"""Minimal pytest for render helpers — frozen contract verification."""

from pickme.pipelines.render import build_aliases, dealias, estimate_tokens, render_transcript, sanitize_html


def test_build_aliases_numbering_by_first_appearance():
    rows = [
        {"user_id": 42, "text": "hi"},
        {"user_id": 99, "text": "hello"},
        {"user_id": 42, "text": "again"},
        {"user_id": 7, "text": "yo"},
        {"user_id": None, "text": "system msg"},
    ]
    aliases = build_aliases(rows)
    assert aliases == {42: "user_1", 99: "user_2", 7: "user_3"}
    # Order preserved even if reordered input
    rows2 = [{"user_id": 7}, {"user_id": 42}]
    assert build_aliases(rows2) == {7: "user_1", 42: "user_2"}


def test_render_transcript_media_and_time():
    # Use known epoch ms for HH:MM determinism — just check format contains alias and media placeholder
    rows = [
        {"user_id": 1, "text": "hello", "media_type": None, "created_at": 0, "is_edited": 0},
        {"user_id": 2, "text": None, "media_type": "photo", "created_at": 0, "is_edited": 0},
        {"user_id": 3, "text": "edited text", "media_type": None, "created_at": 0, "is_edited": 1},
        {"user_id": None, "text": "system note", "media_type": None, "created_at": 0, "is_edited": 0},
    ]
    aliases = build_aliases(rows)
    out = render_transcript(rows, aliases, with_time=True)
    # Check aliases present
    assert "user_1: hello" in out
    # Media-only renders as [photo]
    assert "user_2: [photo]" in out
    # Edited suffix
    assert "user_3: edited text (edited)" in out
    # System alias
    assert "system: system note" in out
    # With time disabled, no brackets prefix
    out_no_time = render_transcript(rows[:1], aliases, with_time=False)
    assert out_no_time.startswith("user_1:")


def test_render_transcript_text_precedence_over_media():
    rows = [{"user_id": 1, "text": "caption here", "media_type": "photo", "created_at": 0, "is_edited": 0}]
    aliases = build_aliases(rows)
    out = render_transcript(rows, aliases)
    assert "caption here" in out
    assert "[photo]" not in out


def test_dealias_replaces_alias_with_display_name():
    aliases = {10: "user_1", 20: "user_2", 30: "user_3"}
    names = {10: "Alice", 20: "Bob"}  # 30 missing -> keep alias
    text = "user_1 and user_2 met user_3 and user_10"
    result = dealias(text, aliases, names)
    assert "Alice" in result
    assert "Bob" in result
    # user_3 kept as alias (no name)
    assert "user_3" in result
    # user_10 not in alias map should not be replaced partially
    assert "user_10" in result
    # Word boundary: user_1 inside user_10 not mangled
    assert result.count("Alice") == 1


def test_sanitize_html_keeps_only_allowed_tags():
    raw = '<b>bold</b> <i>italic</i> <code>code</code> <script>alert(1)</script> 5 < 6 & 7 > 3'
    sanitized = sanitize_html(raw)
    assert "<b>bold</b>" in sanitized
    assert "<i>italic</i>" in sanitized
    assert "<code>code</code>" in sanitized
    # Disallowed tag escaped
    assert "&lt;script&gt;" in sanitized
    assert "&lt;/script&gt;" in sanitized
    # & escaped
    assert "&amp;" in sanitized
    # stray < > escaped (the "5 < 6" part)
    assert "&lt;" in sanitized
    assert "&gt;" in sanitized


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 8) == 2
    assert estimate_tokens("hello world") == len("hello world") // 4


def test_sanitize_html_markdown_bold_italic_code():
    raw = "**bold** and *it* and `code`"
    out = sanitize_html(raw)
    assert "<b>bold</b>" in out
    assert "<i>it</i>" in out
    assert "<code>code</code>" in out
    # no stray markdown left
    assert "**" not in out
    assert out.count("<b>") == 1


def test_sanitize_html_bullets_and_headings():
    assert sanitize_html("- item one") == "• item one"
    assert sanitize_html("  * item two") == "  • item two"
    assert sanitize_html("### Title") == "<b>Title</b>"
    assert sanitize_html("# Заголовок") == "<b>Заголовок</b>"


def test_sanitize_html_already_html_and_snake():
    assert sanitize_html("<b>x</b>") == "<b>x</b>"
    assert sanitize_html("some_var_name") == "some_var_name"
    assert sanitize_html("some_var_name and _italic_") == "some_var_name and <i>italic</i>"
    # snake_case not italicized
    assert "some_var_name" in sanitize_html("some_var_name")


def test_sanitize_html_hr_and_combined():
    assert sanitize_html("---") == ""
    assert sanitize_html("***") == ""
    assert sanitize_html("___") == ""
    # combined markdown
    raw = "**Сводка:**\n* item _x_ and `y`\n### З"
    out = sanitize_html(raw)
    assert "<b>Сводка:</b>" in out
    assert "<i>" in out
    assert "<code>y</code>" in out
    assert "###" not in out
    assert "**" not in out


def test_render_transcript_bot_label():
    rows = [
        {"user_id": 1, "text": "hi", "media_type": None, "created_at": 0, "is_edited": 0, "is_bot": 0},
        {"user_id": None, "text": "my earlier reply", "media_type": None, "created_at": 0, "is_edited": 0, "is_bot": 1},
        {"user_id": None, "text": "anon note", "media_type": None, "created_at": 0, "is_edited": 0, "is_bot": 0},
        {"user_id": 5, "text": "other bot msg", "media_type": None, "created_at": 0, "is_edited": 0, "is_bot": 1},
    ]
    aliases = build_aliases(rows)
    out = render_transcript(rows, aliases)
    assert "user_1: hi" in out
    # own stored reply (user_id None + is_bot) renders as "bot"
    assert "bot: my earlier reply" in out
    # anon non-bot row stays "system"
    assert "system: anon note" in out
    # another bot with a real user_id aliases as a regular participant
    assert "user_2: other bot msg" in out
    # build_aliases ignores None user_ids
    assert None not in aliases and 5 in aliases
