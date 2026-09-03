# pickme-summarization — Architecture Plan

Telegram bot that tracks group-chat messages and provides LLM-powered summarization,
per-user memory, user evaluation, and grounded Q&A — including **natural-language
commands** ("hey bot, summarize last messages for me") — via the free Hetzner
Experiments Inference API.

**Stack (locked):** Python 3.12+ · aiogram 3.x (async) · SQLite (WAL) ·
Docker Compose on a Hetzner VPS · OpenAI-compatible LLM clients with 3-tier
provider failover: Hetzner (free) → OpenCode Zen free models → OpenCode Go.

**Key external constraint:** Hetzner's free tier enforces a request-level limit of
~10 requests / 60s per key (in addition to token caps). The binding constraint is
*request count*, not tokens — LLM calls are serialized and batched throughout the design.

---

## 1. Architecture Overview

**Core principle:** one Python process, one writer, one serialized LLM worker.
SQLite + WAL gives single-writer safety for free; the API's tight request-level
limit forces serialization anyway, so an external queue (Redis/RabbitMQ) is YAGNI
at this scale.

```
                         Telegram (group chats)
                                │  updates (long polling)
                                ▼
        ┌──────────────────────────────────────────────┐
        │                 BOT PROCESS                    │
        │                                                │
        │  aiogram Dispatcher                            │
        │    ├─ Ingest Middleware (outer)                │
        │    │     └─► store raw message → SQLite (WAL)  │
        │    ├─ Command Handlers (/summarize /evaluate…) │
        │    └─ Address Observer (mention / reply / /ask)│
        │           └─► Intent Router                    │
        │                 ├─ keyword fast-path (0 LLM)   │
        │                 └─ LLM router (JSON intent)    │
        │                       └─► dispatch to pipeline │
        │                                                │
        │  LLM Job Queue (asyncio.Queue, single worker)  │
        │    ├─ token bucket ~8 req/60s ← THE rate limit│
        │    ├─ global Semaphore(2)    ← concurrency    │
        │    ├─ per-chat asyncio.Lock (job serialization)│
        │    └─ retry/backoff + circuit breaker          │
        │                │                               │
        │                ▼                               │
        │   LLMClient (OpenAI-compatible, 3 tiers)      │
        │     ├─ T1 Hetzner (free, primary)             │
        │     ├─ T2 OpenCode Zen free (mimo-v2.5-free)  │
        │     ├─ T3 OpenCode Go sub (mimo-v2.5, paid)   │
        │     └─ per-tier health poll + breaker         │
        └──────────────────────────────────────────────┘
                                │  HTTPS
                                ▼
          Hetzner → OpenCode Zen (free) → OpenCode Go
              (failover chain; see §5 for URLs/models)
```

**Data flow:**
1. Every group message → ingest middleware → `messages` table (fire-and-forget, fast).
2. Ingest also updates lightweight counters and enqueues a *memory-update trigger*
   check (not an LLM call).
3. User commands (`/summarize`, `/evaluate`, `/ask`) **or natural-language requests**
   ("hey bot, summarize last messages for me") enqueue a job; the single worker pulls
   it, calls the LLM under the semaphore, writes results back to SQLite, and replies.
4. A background timer periodically flushes chat rolling summaries and due
   user-profile merges.

**Topology decision (opinionated):** Single process. In-process `asyncio.Queue` +
one consumer coroutine. All SQLite writes go through **one** `aiosqlite` connection
(safe under asyncio's single thread; WAL allows concurrent reads). External queue
explicitly rejected for v1–v2.

---

## 2. Project Layout

```
pickme-summarization/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── pyproject.toml
├── bot-plan.md
├── migrations/
│   └── 0001_init.sql
└── src/pickme/
    ├── main.py                 # entrypoint: wire config, db, bot, worker; run_polling
    ├── config.py               # pydantic-settings (env)
    ├── db/
    │   ├── connection.py       # single aiosqlite conn, WAL, migrations apply
    │   └── queries.py          # parameterized SQL helpers (no SQLite-only funcs)
    ├── telegram/
    │   ├── bot.py              # Bot + Dispatcher + routers
    │   ├── middlewares.py      # IngestMiddleware (outer)
    │   ├── handlers.py         # command handlers
    │   └── filters.py          # is_mention / is_reply_to_bot
    ├── llm/
    │   ├── client.py           # LLMClient abstraction (AsyncOpenAI)
    │   ├── registry.py         # per-tier model discovery, failover chain, health
    │   └── prompts.py          # prompt templates (kept as code, versioned)
    ├── routing/
    │   ├── fastpath.py         # regex keyword fast-path (0 LLM calls)
    │   ├── intent.py           # LLM intent router + Intent schema
    │   └── dispatch.py         # intent → pipeline dispatch
    ├── pipelines/
    │   ├── ingest.py
    │   ├── summarize.py        # single-shot + map-reduce
    │   ├── memory.py           # batch trigger, merge, decay
    │   ├── evaluate.py         # rubric → Markdown card
    │   └── qa.py               # context assembly + answer
    ├── worker/
    │   └── queue.py            # queue + worker loop + semaphore + locks + breaker
    └── schemas/
        ├── profile.py          # pydantic: UserProfile, Evaluation
        ├── summary.py          # pydantic: SummaryResult
        └── intent.py           # pydantic: Intent
```

**Postgres-readiness rule:** all queries in `db/queries.py` use standard SQL and
bind params; no `json1`/`GROUP_CONCAT`/date functions in app logic (do date math in
Python). Swap the connection layer only when migrating.

---

## 3. SQLite Schema (DDL sketch)

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE chats (
  chat_id                 INTEGER PRIMARY KEY,
  title                   TEXT,
  created_at              INTEGER NOT NULL,          -- epoch ms
  rolling_summary         TEXT,                      -- compact chat summary
  rolling_summary_at      INTEGER,
  message_count           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE users (
  user_id        INTEGER PRIMARY KEY,
  username       TEXT,
  display_name   TEXT,                               -- cached, for local mapping only
  first_seen_at  INTEGER NOT NULL,
  last_seen_at   INTEGER NOT NULL
);

CREATE TABLE messages (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id             INTEGER NOT NULL REFERENCES chats(chat_id),
  user_id             INTEGER REFERENCES users(user_id),  -- NULL = system/anon
  text                TEXT,
  reply_to_message_id INTEGER,
  media_type          TEXT,                          -- photo/video/doc/sticker/null
  media_meta          TEXT,                          -- JSON: file_id, w/h, etc.
  created_at          INTEGER NOT NULL,
  is_bot              INTEGER NOT NULL DEFAULT 0,
  is_edited           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_messages_chat_time ON messages(chat_id, created_at DESC);
CREATE INDEX idx_messages_user      ON messages(chat_id, user_id);

CREATE TABLE user_profiles (
  chat_id          INTEGER NOT NULL,
  user_id          INTEGER NOT NULL,
  version          INTEGER NOT NULL DEFAULT 1,
  activity_score   REAL    NOT NULL DEFAULT 0,       -- queryable for /evaluate sort
  msg_count        INTEGER NOT NULL DEFAULT 0,
  since_message_id INTEGER NOT NULL DEFAULT 0,        -- watermark: msgs ≤ this are merged
  last_updated_at  INTEGER NOT NULL,
  profile_json     TEXT    NOT NULL,                 -- structured narrative (topics/stance/facts)
  PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX idx_profiles_activity ON user_profiles(chat_id, activity_score DESC);

CREATE TABLE memory_jobs (            -- durability for pending merges
  chat_id          INTEGER NOT NULL,
  user_id          INTEGER NOT NULL,
  since_message_id INTEGER NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending',  -- pending|done|failed
  created_at       INTEGER NOT NULL,
  PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE llm_log (                -- observability/audit
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  feature          TEXT,                              -- summarize|evaluate|qa|route|memory
  provider         TEXT,                              -- hetzner|zen|go
  model            TEXT,
  prompt_tokens    INTEGER,
  completion_tokens INTEGER,
  created_at       INTEGER NOT NULL,
  ok               INTEGER,
  error            TEXT
);
```

**Design note:** profiles use a **hybrid** — a few indexed scalar columns
(`activity_score`, `msg_count`, `last_updated_at`) for querying/sorting, plus a
`profile_json` blob for the flexible narrative. This avoids the "JSON-only =
unqueryable" and "fully structured = rigid" extremes.

---

## 4. Pipelines per Feature

> **Shared rendering rule (applies to EVERY LLM-facing pipeline):** messages are
> rendered through one shared helper that replaces real display names with stable
> aliases (`user_123`); the `user_id→display_name` map stays local and is applied
> only to the final Telegram reply. Summarize, memory merge, evaluate, Q&A, and
> router member-hints all use this renderer — no pipeline sends real names to any
> provider (including Zen free, whose models may train on data).

### 4.1 Ingest (synchronous, no LLM)
- `IngestMiddleware` (outer) runs on **every** update. Extracts `chat_id`,
  `user_id`, `text`, `reply_to`, media metadata, timestamps.
- Writes to `messages`, upserts `users`, increments `chats.message_count`.
- **No LLM call here.** Instead, it checks the `memory_jobs` trigger — counted
  **per `(chat,user)`**, not per chat: `SELECT COUNT(*) FROM messages WHERE
  chat_id=? AND user_id=? AND id > since_message_id` (covered by
  `idx_messages_user`; before a profile exists, the watermark is 0). A merge is
  due when that count ≥ N=50, OR a profile exists and TTL=6h elapsed since
  `last_updated_at`. When due and no job is pending, insert/replace a `pending`
  `memory_jobs` row; the worker later picks it up.
- Cost: one INSERT + one indexed COUNT per message. Safe under WAL.

### 4.2 Summarize (`/summarize [X]` — or natural language)
- Default X=100, cap X=500. Pull last X messages (indexed `idx_messages_chat_time`).
- **Token budget:** estimate tokens ≈ `len(text)//4` (safe over-estimate; exact
  counting needs the Qwen tokenizer — acceptable approximation; API rejects if over).
- **Single-shot path** if estimated input ≤ ~60k tokens: one `chat/completions` call,
  system prompt = "concise neutral summary of this group chat segment",
  user = rendered messages.
- **Map-reduce path** if > 60k: split into ~40k-token chunks → summarize each (map)
  → combine chunk summaries into final (reduce). Cap total input at ~150k tokens;
  if X still exceeds, summarize the most recent 150k-worth and note truncation.
- Output: Markdown, split into ≤4096-char Telegram messages. Send
  `chat_action="typing"` during generation.

### 4.3 Memory Update (batched, no per-message LLM)
- **Trigger:** every N=50 new messages per `(chat,user)` OR TTL=6h since
  `last_updated_at`.
- Gather new messages since `user_profiles.since_message_id` (0 if no profile
  yet). Build merge prompt:
  `[OLD profile_json] + [NEW messages]`. Instruct LLM to return **merged**
  `UserProfile` JSON (topics, stance, activity, notable_facts), dropping stale facts.
- **Decay/forgetting:** on each merge, `activity_score` decays exponentially toward 0
  if the user has been quiet; very old `notable_facts` are pruned by the LLM per
  instruction. A periodic reaper lowers `activity_score` for inactive users so they
  fade from `/evaluate` rankings.
- `enable_thinking: False` here (deterministic merge, saves tokens).
- On success: write `user_profiles` (including `since_message_id` = id of the
  last merged message), clear the `memory_jobs` row.

### 4.4 Evaluate (`/evaluate [@user | me | everyone]` — or natural language)
- **Rubric (structured output):** tone (supportive/neutral/confrontational),
  constructiveness (1–5), dominant topics, participation level (1–5), notable
  contributions, red-flags (optional). Rendered as a Markdown "card."
- For a single `@user`: pull their `profile_json` + recent sample messages → LLM
  assessment.
- For `everyone`: take top-K `activity_score` users per chat (K capped, e.g. 20)
  and **batch 5–10 profiles per LLM call** (one card per user, parsed from the
  batched response) — NOT one call per user, which at ~10 req/60s would occupy
  the queue for minutes. Runs as a background job with a progress reply
  ("Evaluating 18 members…").
- Output schema validated by `schemas/profile.py` (pydantic); malformed JSON →
  retry once, else error card.

### 4.5 Direct Q&A (`/ask`, mention, reply-to-bot)
- **Context assembly (budgeted, well within 262k):**
  - `rolling_summary` (≤2k tokens) — chat-level grounding.
  - Relevant `user_profiles` (top-N by activity or mentioned users; ≤N×0.5k).
  - Recent window: last ~200 messages or ~30k tokens (whichever first), rendered
    with author alias + text.
  - The question.
- **Privacy alias:** via the shared §4 rendering rule — aliased authors, local
  `user_id→display_name` map for the final reply only. (See Risks.)
- Truncation order: drop oldest messages → compress media to metadata → if still
  over, summarize older portion into the rolling summary and retry.
- Answer in Markdown, split if needed.

### 4.6 Natural-Language Intent Router ("hey bot, summarize last messages for me")

Any explicitly addressed message (mention, reply-to-bot, `/ask`) is first routed;
the bot **summarizes, evaluates, or answers** based on what the user *said*, not
just slash commands.

**Two-stage routing, cheap-first:**

1. **Keyword fast-path (0 LLM calls):** high-confidence regex patterns, any language:
   - `summar|tldr|tl;dr|резюме|перекаж|opowi|zusammenfass` (+ optional
     `last (\d+)` / `последние (\d+)` count extraction) → `summarize(count?)`
   - `evaluat|assess|оцен|rank|рейтинг` (+ `@user` / `everyone` / `me`) →
     `evaluate(target?)`
   Fast-path only fires on a clear match; otherwise fall through to stage 2.
2. **LLM router (1 small call, structured JSON):** system prompt describes the
   bot's actions as a menu — `summarize{count?}`, `evaluate{target?}`, `qa`,
   `forget{target?}`, `help` — and instructs: classify the user's utterance into
   exactly one action, extract params, respond in JSON only, handle any language.
   Output validated by pydantic `Intent`; malformed JSON → **default to `qa`**
   (graceful degradation: worst case the bot just answers the message).

**Dispatch:**
- `summarize` / `evaluate` → enqueue the **same jobs** as the slash commands
  (pipelines reused verbatim; per-chat lock prevents races). The bot immediately
  acknowledges ("Summarizing the last 100 messages…") and the result arrives as the
  job output.
- `qa` → normal Q&A pipeline (§4.5) with full context assembly.
- `forget` → no LLM; direct deletion pipeline.
- Parameter extraction: count ("last 50"), target (`@user`, "everyone", "me", or a
  display-name lookup against chat members). Time-window phrasing ("since morning")
  maps to the nearest count in v1; timestamp-filtered windows are a v2 refinement.
- Router calls carry only the utterance + a member/mention hint (aliases only,
  per the shared §4 rendering rule) — **no chat corpus** — so they stay tiny.
  Tagged `feature=route` in `llm_log`.
- Rate-limit note: routing adds at most one small call per addressed message; it
  runs inline (outside the worker queue) but goes through the same `LLMClient`
  token bucket, so it can never burst past the request budget. The fast-path
  exists precisely to skip this call for the most common asks.
- Rejected alternative (documented): a single "mega-call" that both classifies and
  answers with full context — unpredictable and wasteful for summarize
  jobs that need the whole corpus anyway. Two small deterministic stages win.

### 4.7 Chat Rolling Summary (background, feeds Q&A)
- **Trigger:** background timer per active chat — every 30 min when the chat has
  ≥ 25 new messages since `chats.rolling_summary_at`; runs as a worker job under
  the same per-chat lock, so it never races a `/summarize` or memory merge.
- **Input:** previous `chats.rolling_summary` + all messages since
  `rolling_summary_at`, rendered through the shared §4 aliasing rule. Budget
  ~40k tokens for the new-message window; if exceeded, map-reduce the window
  first (same chunking as §4.2), then merge.
- **Merge prompt:** `[OLD summary] + [NEW segment summaries]` → one compact
  ≤2k-token summary: ongoing topics, decisions, open threads, who-is-doing-what
  (aliased). Stale facts are dropped by instruction, mirroring the profile merge.
- **On failure:** keep the old summary untouched; retry on the next timer tick.
  The summary is an optimization (Q&A grounding), never a hard dependency.

---

## 5. LLM Client Design

```python
class LLMClient(Protocol):
    async def chat(self, messages, *, model=None, json=False,
                   enable_thinking=False, **kw) -> ChatResult: ...
```

- **Implementation:** one thin `Provider` wrapper per tier around
  `openai.AsyncOpenAI(base_url=..., api_key=...)` — every tier we use exposes
  OpenAI-compatible `/chat/completions` for the selected models. `chat()` adds
  `response_format={"type":"json_object"}` when `json=True` (support on the
  opencode tiers is unconfirmed → always keep a robust JSON-from-text parse
  fallback), and `extra_body={"chat_template_kwargs":{"enable_thinking": False}}`
  on the Hetzner tier only (undocumented — try/except, non-fatal if ignored).
- **Provider chain (`registry.py`) — cost-ordered, failover down, recover up:**

  | Tier | Provider | Base URL | Model (config) | Cost |
  |---|---|---|---|---|
  | 1 | Hetzner Inference | `https://inference.hetzner.com/api/v1` | `LLM_PRIMARY_MODEL` (in-tier fallback: `LLM_FALLBACK_MODEL`) | free |
  | 2 | OpenCode Zen | `https://opencode.ai/zen/v1` | `ZEN_FREE_MODEL` (def `mimo-v2.5-free`) | free (limited-time) |
  | 3 | OpenCode Go | `https://opencode.ai/zen/go/v1` | `GO_MODEL` (def `mimo-v2.5`) | $10/mo sub quotas |

  - **Auth:** `HETZNER_API_KEY` + one long-lived `OPENCODE_API_KEY` (the same key
    serves Zen and Go; created at opencode.ai/auth). Descriptive
    `User-Agent: pickme-bot/1.0` on opencode requests (abuse-filter hygiene).
  - **Failover:** a circuit breaker **per tier**; tier N opening (5xx storm,
    auth error, or model missing from its `/models` listing — **not** 429s, see
    rate limiting below) shifts traffic to
    tier N+1. When a cheaper tier's health poll succeeds again, traffic fails
    back automatically. The user-facing "degraded" state happens only when ALL
    tiers are down.
  - **Discovery per tier:** startup + periodic `GET {base_url}/models`; use the
    configured model if listed, else the first known-compatible one. Hot-swap
    via env / `set_primary()` without restart.
  - **Endpoint-style caveat:** the chain is restricted to `/chat/completions`
    models (the MiMo family qualifies on both Zen-free and Go). Qwen models on
    Go use the Anthropic-style `/messages` path and are excluded unless a second
    client style is added later.
- **Rate limiting & retries (inside client, shared by ALL LLM traffic):**
  - **Token bucket first:** ~8 requests / 60s (configurable), enforced inside
    `LLMClient` in front of the semaphore — a semaphore caps *concurrency*, not
    *rate*. Every LLM request buys a token: worker jobs, inline router calls
    (§4.6), and per-tier health polls all share this one budget.
  - Global `asyncio.Semaphore(2)` behind the bucket bounds concurrent requests.
  - **429 → backoff only, never failover.** A 429 is the *expected* state of a
    free tier at its limit: exponential backoff + jitter, honor `Retry-After`.
    The **circuit breaker opens only on 5xx storms, auth errors, or a model
    missing from its `/models` listing** — never on plain 429s — so a burst at
    the rate limit cannot spill traffic onto paid tiers. Breaker open: that
    tier pauses, traffic fails over (chain above); auto-recovers when the
    tier's health poll succeeds.
  - **Per-chat `asyncio.Lock`** keyed by `chat_id` so two jobs for the same chat
    never race (e.g., memory merge + summarize).
  - **Request coalescing:** if a `/summarize` for a chat is already queued/running,
    second request gets "already running, wait" rather than a duplicate job.
- **Health check:** per-tier `GET {base_url}/models` poll every ≥60s (infrequent
  and 429-tolerant — a 429 from a poll means "alive but busy", not "tier down");
  drives failover, fail-back, and the all-tiers-down degraded state. Polls draw
  from the same token bucket.

---

## 6. Command Surface & UX

| Trigger | Args | Behavior |
|---|---|---|
| `/start`, `/help` | — | Capabilities + privacy note |
| `/summarize` | `[X]` (def 100, cap 500) | Summarize last X; map-reduce if large |
| `/evaluate` | `[@user \| me \| everyone]` | Markdown assessment card |
| `/ask` | `<question>` | Q&A grounded in context+memories |
| **NL address** (mention / reply-to-bot) | free text | Intent-routed: "summarize last messages for me", "what do you think of @dan?", "оцени всех" → matching pipeline |
| `/forget` | `[me \| @user \| chat]` | Delete that user's data / whole chat |
| `/status` | (admin) | Active provider tier, model, health, queue depth, 429 count |

**UX details:**
- `chat_action="typing"` during generation; "⏳ thinking…" placeholder for long jobs.
- Replies split at 4096 chars (Telegram limit); **HTML parse mode** (avoids
  MarkdownV2 escaping pain — opinionated).
- Address detection: mention (`@bot`), reply-to-bot, or `/ask` prefix all route to
  the intent router (§4.6).
- On LLM failure: friendly error + suggest retry; never crash the handler.
- **Privacy mode (CRITICAL):** bot must have **privacy mode disabled** in BotFather
  *or* be a chat admin to read all messages. Documented in README and `/start`.
  Without it, tracking is silently incomplete.

---

## 7. Deployment

**Long polling (recommended)** over webhook: no public TLS endpoint, no reverse
proxy, robust to restarts, trivial on a single VPS. *Trade-off flagged:* webhook
scales better and gives lower latency, but requires HTTPS termination
(nginx/Caddy) and is over-engineering for one bot. Revisit only if multiple bots or
high throughput.

`docker-compose.yml` (single service):
```yaml
services:
  bot:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/app/data      # SQLite WAL files persist here
    deploy:
      resources:
        limits: { memory: 512M }
```

`.env` (via `config.py` / pydantic-settings):
```
TELEGRAM_BOT_TOKEN=
HETZNER_API_KEY=
HETZNER_BASE_URL=https://inference.hetzner.com/api/v1
LLM_PRIMARY_MODEL=Qwen/Qwen3.6-35B-A3B-FP8
LLM_FALLBACK_MODEL=Qwen3.8-27B
OPENCODE_API_KEY=
ZEN_BASE_URL=https://opencode.ai/zen/v1
ZEN_FREE_MODEL=mimo-v2.5-free
GO_BASE_URL=https://opencode.ai/zen/go/v1
GO_MODEL=mimo-v2.5
GO_ENABLED=1
MEMORY_BATCH_SIZE=50
MEMORY_TTL_HOURS=6
SUMMARIZE_DEFAULT=100
SUMMARIZE_CAP=500
ROUTER_FASTPATH=1
LOG_LEVEL=INFO
DATA_DIR=/app/data
```
SQLite path: `${DATA_DIR}/pickme.db` with WAL files on the same volume.

---

## 8. Observability

- Structured JSON logs (stdlib `logging` + `json` formatter, or `structlog`).
  Fields: `feature`, `model`, `tokens`, `latency_ms`, `status`.
- Lightweight metrics via an in-process counter set (jobs queued/done/failed, 429
  count, LLM latency, queue depth, router fast-path hit rate) exposed through the
  `/status` command and optionally a tiny `aiohttp` `/metrics` endpoint (optional,
  not required for v1).
- `llm_log` table for per-call audit/debug.

---

## 9. Milestones

**M1 — MVP (tracking + /summarize)**
- Bot ingests all group messages to SQLite (WAL).
- `/summarize [X]` works for default and custom X; map-reduce for large inputs.
- LLM client with token-bucket rate limiter, semaphore, 429 backoff (never
  breaker-opening), and circuit breaker on 5xx/auth; graceful "degraded"
  reply.
- Privacy-mode note in README/`/start`.
- *Acceptance:* in a test group, send 200 messages, `/summarize` returns coherent
  Markdown; kill network → bot retries then reports degraded, recovers on restore.

**M2 — v1 (memory + /evaluate)**
- Batched incremental `user_profiles` (no per-message calls); decay/reaper.
- `/evaluate @user|me|everyone` renders Markdown cards from `schemas/profile.py`.
- `/forget` deletes user/chat data; retention policy enforced.
- *Acceptance:* after N messages, profiles populate; `/evaluate everyone` ranks by
  `activity_score`; `/forget @user` removes rows and they stop appearing.

**M3 — v2 (NL routing + Q&A + polish)**
- Address observer → intent router: keyword fast-path + LLM router with `qa`
  fallback; "hey bot, summarize last messages for me" routes to the summarize job.
- `/ask`, mention, reply-to-bot Q&A grounded in rolling summary + profiles + recent
  window, with author aliasing.
- Chat-level rolling summary incremental merge.
- Full degradation: 3-tier provider failover (Hetzner → Zen free → Go), feature
  disable when all tiers are down, and observability.
- *Acceptance:* ask "what did we decide about X?" → answer cites chat context;
  say "summarize the last 50 for me" via mention → routed summary with correct X;
  break Hetzner (invalid key) → bot serves via Zen free, then Go; all tiers down →
  clean degraded reply; a recovered cheaper tier regains traffic; model/tier swap
  via env takes effect without code change; `/status` shows tier + health.

Each milestone is independently demoable and shippable.

---

## 10. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **Experimental API, model deprecation** | Features break overnight | `/v1/models` discovery + primary/fallback + hot-swap + health loop; degrade gracefully |
| **Request-level limit ≈10/60s (binding)** | Throughput ceiling, 429 storms | Token bucket ~8 req/60s in `LLMClient` covering ALL traffic (worker, router, health polls); batching; coalescing |
| **429 / 5xx storms** | Latency, failures | 429 → backoff only, never fails over to paid tiers; breaker opens on 5xx/auth/model-missing only |
| **Zen/Go rate limits unpublished (429s reported)** | Fallback tiers unstable | Per-tier breakers + failover; Zen free treated as best-effort; shared backoff |
| **Zen free models may train on data** | Privacy gap widens | Keep author aliasing everywhere; `/status` shows active tier; tier 2 disableable via env |
| **Go $-quota windows can exhaust** | Tier 3 dark mid-window | Quota/429 errors treated as tier-down; window roll restores it |
| **Mixed endpoint styles on opencode** | Wrong-path 404s | Chain restricted to `/chat/completions` models (MiMo); Qwen-on-Go excluded |
| **Zen free may need 1-time deposit activation** | Tier 2 unusable | Verify `GET /zen/v1/models` with our key before relying on it; else skip to Go |
| **Router adds +1 call per NL interaction** | Tighter rate-limit budget | Keyword fast-path skips the call for common asks; router prompt is tiny; coalescing |
| **SQLite write contention** | Corruption/locked errors | WAL + single writer connection; all writes via one `aiosqlite` conn; reads share it |
| **Postgres migration later** | Rework | Standard SQL only, bind params, no SQLite-only functions; swap connection layer |
| **Telegram rate limits (send)** | 429 on replies | Split long messages; `chat_action` throttling; per-chat send guard |
| **Privacy mode ON** | Silent data loss | BotFather: disable privacy or require admin; documented in `/start` |
| **GDPR-lite / no DPA (Hetzner)** | Compliance gap for personal data | Treat as prototype/internal tool; `/forget` + export + retention limits; **alias display names** in LLM prompts (keep `user_id→name` map local); document that message text is sent to a third-party processor with no DPA |
| **`enable_thinking` undocumented** | Token waste / behavior change | Use it only as a non-fatal hint; design works without it |
| **Token over-estimation** | Unnecessary truncation | Char/4 heuristic + safety margin; API rejects over-limit → catch and re-chunk |
| **Job loss on restart** | Stale profiles | `memory_jobs` table gives durability; worker drains `pending` on startup (optional) |

**Trade-offs explicitly flagged:**
1. **Single-process + in-memory queue** is simplest and correct here, but a process
   crash loses in-flight LLM jobs. The `memory_jobs` table mitigates the important
   ones (profile merges). If guaranteed delivery is ever needed, *then* add Redis —
   not before.
2. **Author aliasing vs memory quality:** aliasing display names protects privacy
   but slightly weakens cross-session memory coherence. Aliased by default, local
   name map kept; revisit if memory quality suffers.
3. **Two-stage routing vs single mega-call:** the extra (tiny) routing call is the
   price for deterministic intent handling that reuses the slash-command pipelines
   verbatim; the fast-path recovers the cost for common phrasings.
