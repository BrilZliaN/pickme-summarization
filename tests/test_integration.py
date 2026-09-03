"""Integration tests with FakeLLM — no network."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from pickme.config import Settings
from pickme.db.connection import Database
from pickme.db import queries as q
from pickme.llm.client import ChatResult
from pickme.pipelines import PipelineError
from pickme.schemas.profile import UserProfile, EvalTarget
from pickme.pipelines.render import build_aliases
from pickme.schemas.summary import SummaryResult


class FakeLLM:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, messages, *, feature="misc", json_mode=False, temperature=None, max_tokens=None):
        self.calls.append({"feature": feature, "messages": messages, "json_mode": json_mode})
        text = self.responses.pop(0) if self.responses else "ok"
        return ChatResult(text=text, provider="fake", model="fake")


def _settings(tmp_path):
    return Settings(telegram_bot_token="t", data_dir=str(tmp_path), memory_batch_size=3, memory_ttl_hours=6)


async def _make_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    return db


# 1
async def test_db_roundtrip(tmp_path):
    db = await _make_db(tmp_path)
    try:
        now = int(time.time() * 1000)
        await q.upsert_chat(db.conn, 1, "Chat", now)
        await q.upsert_user(db.conn, 100, "alice", "Alice", now)
        # insert 3 messages
        ids = []
        for i, txt in enumerate(["hello", "world", "again"]):
            mid = await q.insert_message(db.conn, 1, 100, txt, None, None, None, now + i, False, False)
            ids.append(mid)
        # ordering ascending
        rows = await q.get_last_messages(db.conn, 1, 10)
        assert len(rows) == 3
        assert [r["text"] for r in rows] == ["hello", "world", "again"]
        # count_user_messages_since watermark 0
        cnt = await q.count_user_messages_since(db.conn, 1, 100, 0)
        assert cnt == 3
        # since first id
        cnt2 = await q.count_user_messages_since(db.conn, 1, 100, ids[0])
        assert cnt2 == 2
    finally:
        await db.close()


# 2
async def test_ingest_message_memory_due(tmp_path):
    db = await _make_db(tmp_path)
    try:
        from pickme.pipelines.ingest import ingest_message

        settings = _settings(tmp_path)
        now = int(time.time() * 1000)
        # 3 messages for same user should trigger memory_due on third
        for i in range(3):
            mid, due = await ingest_message(
                db,
                settings,
                chat_id=1,
                chat_title="Chat",
                user_id=42,
                username="bob",
                display_name="Bob",
                text=f"msg {i}",
                reply_to_message_id=None,
                media_type=None,
                media_meta=None,
                created_at=now + i * 1000,
                is_bot=False,
                is_edited=False,
            )
            if i < 2:
                assert not due, f"i={i} unexpected due"
            else:
                assert due is True
        # pending job exists
        pending = await q.pending_memory_jobs(db.conn)
        assert len(pending) == 1
        assert pending[0]["chat_id"] == 1 and pending[0]["user_id"] == 42
    finally:
        await db.close()


# 3
async def test_run_summarize_alias_and_sanitize(tmp_path):
    db = await _make_db(tmp_path)
    try:
        from pickme.pipelines.summarize import run_summarize

        settings = Settings(telegram_bot_token="t", data_dir=str(tmp_path))
        now = int(time.time() * 1000)
        await q.upsert_chat(db.conn, 1, "G", now)
        # seed 10 messages with distinct display names, ensure aliases needed
        for uid, name, txt in [
            (10, "Alice Wonderland", "hello world from Alice"),
            (20, "Bob Builder", "bob message 1"),
            (10, "Alice Wonderland", "alice again"),
            (20, "Bob Builder", "bob again 2"),
            (30, "Charlie", "charlie hi"),
            (10, "Alice Wonderland", "more alice"),
            (20, "Bob Builder", "more bob"),
            (30, "Charlie", "charlie 2"),
            (10, "Alice Wonderland", "alice 3"),
            (20, "Bob Builder", "bob 3"),
        ]:
            await q.upsert_user(db.conn, uid, None, name, now)
            await q.insert_message(db.conn, 1, uid, txt, None, None, None, now, False, False)
            await q.increment_chat_message_count(db.conn, 1)

        fake = FakeLLM(['<b>Summary</b> of user_1 and user_2 <script>alert(1)</script>'])
        result = await run_summarize(db, fake, settings, 1, 10)
        assert isinstance(result, SummaryResult)
        assert result.message_count == 10
        assert result.truncated is False
        # sanitized keeps <b> but escapes script
        assert "<b>Summary</b>" in result.html
        assert "<script>" not in result.html
        assert "&lt;script&gt;" in result.html
        # dealias: final reply restores real display names, no alias tokens left
        assert "Alice Wonderland" in result.html
        assert "Bob Builder" in result.html
        assert "user_1" not in result.html
        assert "user_2" not in result.html
        # LLM prompt must contain aliases, not real names
        assert len(fake.calls) == 1
        prompt_text = " ".join(
            str(m.get("content", "")) for m in fake.calls[0]["messages"]
        )
        assert "Alice Wonderland" not in prompt_text
        assert "Bob Builder" not in prompt_text
        assert "Charlie" not in prompt_text
        assert "user_1" in prompt_text
        assert "user_2" in prompt_text
    finally:
        await db.close()


async def test_summarize_markdown_conversion(tmp_path):
    db = await _make_db(tmp_path)
    try:
        from pickme.pipelines.summarize import run_summarize

        settings = Settings(telegram_bot_token="t", data_dir=str(tmp_path))
        now = int(time.time() * 1000)
        await q.upsert_chat(db.conn, 1, "G", now)
        await q.upsert_user(db.conn, 10, None, "Alice", now)
        await q.upsert_user(db.conn, 20, None, "Bob", now)
        for i in range(5):
            uid = 10 if i % 2 == 0 else 20
            await q.insert_message(db.conn, 1, uid, f"msg {i}", None, None, None, now + i, False, False)
            await q.increment_chat_message_count(db.conn, 1)
        fake = FakeLLM(["**bold** md"])
        result = await run_summarize(db, fake, settings, 1, 5)
        assert "<b>bold</b> md" in result.html
        assert "**" not in result.html
    finally:
        await db.close()


# 4a valid
async def test_run_memory_merge_valid(tmp_path):
    db = await _make_db(tmp_path)
    try:
        from pickme.pipelines.memory import run_memory_merge

        settings = _settings(tmp_path)
        now = int(time.time() * 1000)
        await q.upsert_chat(db.conn, 1, "G", now)
        await q.upsert_user(db.conn, 50, "eve", "Eve", now)
        # messages
        for i in range(3):
            await q.insert_message(db.conn, 1, 50, f"eve msg {i}", None, None, None, now + i, False, False)
            await q.increment_chat_message_count(db.conn, 1)
        # pending job watermark 0
        await q.upsert_memory_job(db.conn, 1, 50, 0, now)
        profile_json = json.dumps({
            "topics": ["testing"],
            "stance": "curious",
            "activity_level": 3,
            "notable_facts": ["likes testing"],
            "narrative": "Eve is curious."
        })
        fake = FakeLLM([profile_json])
        # need to get last id for since
        rows = await q.get_last_messages(db.conn, 1, 10)
        last_id = rows[-1]["id"]
        await run_memory_merge(db, fake, settings, 1, 50)
        prof = await q.get_profile(db.conn, 1, 50)
        assert prof is not None
        assert float(prof["activity_score"]) > 0
        assert int(prof["msg_count"]) == 3
        assert int(prof["since_message_id"]) == last_id
        pending = await q.pending_memory_jobs(db.conn)
        assert pending == []
        # second call malformed-then-valid
        # add 3 more messages
        for i in range(3):
            await q.insert_message(db.conn, 1, 50, f"eve2 msg {i}", None, None, None, now + 100 + i, False, False)
            await q.increment_chat_message_count(db.conn, 1)
        await q.upsert_memory_job(db.conn, 1, 50, last_id, now + 200)
        fake2 = FakeLLM(["not json", profile_json])
        await run_memory_merge(db, fake2, settings, 1, 50)
        prof2 = await q.get_profile(db.conn, 1, 50)
        assert prof2 is not None
        assert len(fake2.calls) == 2  # retry path used
        pending2 = await q.pending_memory_jobs(db.conn)
        assert pending2 == []
    finally:
        await db.close()


# 5 single me
async def test_run_evaluate_single_me(tmp_path):
    db = await _make_db(tmp_path)
    try:
        from pickme.pipelines.evaluate import run_evaluate

        settings = _settings(tmp_path)
        now = int(time.time() * 1000)
        await q.upsert_chat(db.conn, 1, "G", now)
        await q.upsert_user(db.conn, 100, "alice", "Alice Wonderland", now)
        for i in range(5):
            await q.insert_message(db.conn, 1, 100, f"alice msg {i}", None, None, None, now + i, False, False)
            await q.increment_chat_message_count(db.conn, 1)
        # profile
        up = UserProfile(topics=["python"], stance="supportive", activity_level=4, notable_facts=["writes tests"], narrative="Alice is active.")
        await q.upsert_profile(db.conn, 1, 100, 3.5, 5, 0, up.model_dump_json(), now)
        # fake evaluation JSON
        ev_json = json.dumps({
            "tone": "supportive",
            "constructiveness": 5,
            "participation": 4,
            "dominant_topics": ["python"],
            "notable_contributions": ["helpful answers"],
            "red_flags": []
        })
        fake = FakeLLM([ev_json])
        target = EvalTarget(kind="me")
        html = await run_evaluate(db, fake, settings, 1, 100, target)
        assert "Alice Wonderland" in html  # dealiased real name
        assert "Конструктивность" in html
        # prompt must not contain real name
        assert len(fake.calls) == 1
        prompt = " ".join(str(m.get("content","")) for m in fake.calls[0]["messages"])
        assert "Alice Wonderland" not in prompt
    finally:
        await db.close()


# 6 everyone
async def test_run_evaluate_everyone(tmp_path):
    db = await _make_db(tmp_path)
    try:
        from pickme.pipelines.evaluate import run_evaluate

        settings = _settings(tmp_path)
        now = int(time.time() * 1000)
        await q.upsert_chat(db.conn, 1, "G", now)
        for uid, name in [(10, "Alice"), (20, "Bob"), (30, "Charlie")]:
            await q.upsert_user(db.conn, uid, None, name, now)
            up = UserProfile(topics=[name.lower()], stance=None, activity_level=3, notable_facts=[], narrative=f"{name} narrative")
            await q.upsert_profile(db.conn, 1, uid, 2.0 + uid % 3, 5, 1, up.model_dump_json(), now)

        batch_json = json.dumps([
            {"alias": "user_1", "evaluation": {"tone": "supportive", "constructiveness": 4, "participation": 3, "dominant_topics": ["a"], "notable_contributions": ["c1"], "red_flags": []}},
            {"alias": "user_2", "evaluation": {"tone": "neutral", "constructiveness": 3, "participation": 3, "dominant_topics": ["b"], "notable_contributions": ["c2"], "red_flags": []}},
            {"alias": "user_3", "evaluation": {"tone": "confrontational", "constructiveness": 2, "participation": 2, "dominant_topics": ["c"], "notable_contributions": ["c3"], "red_flags": ["flag"]}},
        ])
        fake = FakeLLM([batch_json])
        target = EvalTarget(kind="everyone")
        html = await run_evaluate(db, fake, settings, 1, 10, target)
        # one card per user -> each name appears?
        assert html.count("Alice") + html.count("Bob") + html.count("Charlie") >= 3
        # at least 3 cards (heuristic: <b> occurs)
        assert html.count("<b>") >= 3
    finally:
        await db.close()


# 7 QA
async def test_run_qa_alias_and_rolling(tmp_path):
    db = await _make_db(tmp_path)
    try:
        from pickme.pipelines.qa import run_qa

        settings = _settings(tmp_path)
        now = int(time.time() * 1000)
        await q.upsert_chat(db.conn, 1, "G", now)
        # rolling summary
        await q.set_rolling_summary(db.conn, 1, "Rolling context about project X", 0)
        await q.upsert_user(db.conn, 10, None, "Alice", now)
        await q.upsert_user(db.conn, 20, None, "Bob", now)
        for i in range(5):
            uid = 10 if i % 2 == 0 else 20
            await q.insert_message(db.conn, 1, uid, f"msg {i} from {uid}", None, None, None, now + i, False, False)
            await q.increment_chat_message_count(db.conn, 1)
        fake = FakeLLM(["Answer by user_1 about topic"])
        html = await run_qa(db, fake, settings, 1, "what is X?", asker_id=10, quote="hello from Alice")
        # final answer has real name substituted
        assert "Alice" in html or "Bob" in html
        assert "user_1" not in html  # dealiased
        # prompt had rolling summary
        prompt = " ".join(str(m.get("content","")) for m in fake.calls[0]["messages"])
        assert "Rolling context about project X" in prompt
        # asker identity + replied-to quote passed via alias (privacy-safe)
        assert "Question (from user_1)" in prompt
        assert "hello from Alice" in prompt
    finally:
        await db.close()


# 8 JobQueue
async def test_jobqueue_coalescing_and_metrics(tmp_path):
    from pickme.worker.queue import JobQueue, JobAlreadyRunning
    from pickme.pipelines import PipelineError
    from pickme.pipelines.qa import run_qa

    db = await _make_db(tmp_path)
    try:
        settings = _settings(tmp_path)
        now = int(time.time() * 1000)
        await q.upsert_chat(db.conn, 1, "G", now)
        await q.upsert_user(db.conn, 10, None, "Alice", now)
        await q.insert_message(db.conn, 1, 10, "hello", None, None, None, now, False, False)
        await q.upsert_chat(db.conn, 2, "G2", now)
        fake = FakeLLM(["qa answer html <b>hi</b>"])

        async def qa_wrapper(payload):
            # small delay to keep job in-flight for coalescing test
            await asyncio.sleep(0.25)
            return await run_qa(db, fake, settings, payload["chat_id"], payload["question"])

        async def slow_wrapper(payload):
            await asyncio.sleep(0.3)
            return "slow done"

        queue = JobQueue()
        queue.register("qa", qa_wrapper)
        queue.register("slow", slow_wrapper)
        # also register other kinds as no-ops for completeness
        queue.register("summarize", qa_wrapper)
        queue.register("evaluate", qa_wrapper)
        queue.register("memory_merge", qa_wrapper)
        await queue.start()
        try:
            # submit qa returns html — use create_task for concurrency test
            task = asyncio.create_task(queue.submit("qa", 1, {"chat_id": 1, "question": "q?"}))
            # coalescing: second submit same (kind,chat_id) while first pending raises
            # need to attempt before first completes
            await asyncio.sleep(0.05)
            with pytest.raises(JobAlreadyRunning):
                await queue.submit("qa", 1, {"chat_id": 1, "question": "second"})
            res = await task
            assert isinstance(res, str)
            # submit_background returns future; metrics
            queue2 = JobQueue()
            queue2.register("slow", slow_wrapper)
            await queue2.start()
            try:
                # start slow job in background
                f1 = queue2.submit_background("slow", 1, {"chat_id": 1})
                assert f1 is not None
                # coalesced
                f2 = queue2.submit_background("slow", 1, {"chat_id": 1})
                assert f2 is None
                await asyncio.sleep(0.6)
                m = queue2.metrics()
                assert m["queued"] >= 1
                assert m["done"] >= 1 or m["failed"] == 0
            finally:
                await queue2.stop()
            m = queue.metrics()
            assert m["queued"] >= 1
            assert m["done"] >= 1
        finally:
            await queue.stop()
    finally:
        await db.close()


# 9 maybe_update_rolling
async def test_maybe_update_rolling(tmp_path):
    db = await _make_db(tmp_path)
    try:
        from pickme.pipelines.summarize import maybe_update_rolling

        settings = _settings(tmp_path)
        now = int(time.time() * 1000)
        await q.upsert_chat(db.conn, 1, "G", now)
        # ensure rolling_summary_at =0 then insert 25 messages with users pre-created
        for uid in [10, 11, 12]:
            await q.upsert_user(db.conn, uid, None, f"User{uid}", now)
        for i in range(25):
            uid = 10 + (i % 3)
            await q.insert_message(db.conn, 1, uid, f"msg {i}", None, None, None, now + i, False, False)
            await q.increment_chat_message_count(db.conn, 1)
        fake = FakeLLM(["new rolling summary"])
        ok = await maybe_update_rolling(db, fake, settings, 1)
        assert ok is True
        chat = await q.get_chat(db.conn, 1)
        assert chat["rolling_summary"] is not None
        # second call with <25 new should be False (no new messages)
        ok2 = await maybe_update_rolling(db, fake, settings, 1)
        assert ok2 is False
    finally:
        await db.close()


# 10 extract_json and split_html
def test_extract_json_and_split_html():
    from pickme.llm.client import extract_json
    from pickme.telegram.bot import split_html

    # fenced
    assert extract_json("```json\n{\"a\": 1}\n```") == {"a": 1}
    # leading prose + balanced object
    assert extract_json("here is json {\"x\": 2, \"y\": \"hi\"} end") == {"x": 2, "y": "hi"}
    # garbage -> None
    assert extract_json("no json here") is None
    assert extract_json("```\nnot json\n```") is None

    # split_html long text >4096 splits into <=4096 chunks, rejoins losslessly
    long = ("a" * 4000 + "\n") * 3  # ~12000 chars with newlines
    chunks = split_html(long, 4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == long
    short = "hi <b>there</b>"
    assert split_html(short) == [short]


# 11 Settings missing token
def test_settings_requires_token(tmp_path, monkeypatch):
    # Ensure env var and .env do not provide a token — test should be hermetic
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    # Point env_file to a non-existent file so .env with a real token is ignored
    nonexistent = tmp_path / "no.env"
    with pytest.raises(ValidationError):
        Settings(_env_file=str(nonexistent))
    with pytest.raises(ValidationError):
        Settings(_env_file=str(nonexistent), telegram_bot_token="")


# Regression: default-constructed Database must find and apply migrations
async def test_database_default_discovers_migrations(tmp_path):
    # No explicit migrations_dir — must auto-discover via package/repo candidates
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    try:
        import sqlite3

        # Verify all 7 tables exist via sqlite_master query through our connection,
        # and also directly via sqlite3 for double-check
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        names = sorted(r["name"] for r in rows)  # type: ignore[index]
        # sqlite_sequence is auto-created for AUTOINCREMENT — ignore it
        names_filtered = [n for n in names if n != "sqlite_sequence"]
        expected = sorted(
            ["chats", "users", "messages", "user_profiles", "memory_jobs", "llm_log", "schema_migrations"]
        )
        assert names_filtered == expected, f"got {names} filtered {names_filtered}"
        # Also exercise an unmigrated-db path: existing empty file should get migrated
        # Re-open same file with new Database instance — should not raise and should still have tables
        await db.close()
        db2 = Database(str(tmp_path / "t.db"))
        await db2.connect()
        try:
            cursor2 = await db2.conn.execute("SELECT count(*) AS cnt FROM schema_migrations")
            row2 = await cursor2.fetchone()
            await cursor2.close()
            assert row2 is not None
            assert int(row2["cnt"]) >= 1  # at least 0001_init.sql applied
            # pending_memory_jobs should work (evidence failure would have been here)
            pending = await q.pending_memory_jobs(db2.conn)
            assert isinstance(pending, list)
        finally:
            await db2.close()
        return
    finally:
        # Ensure db closed if still open
        try:
            await db.close()
        except Exception:
            pass
