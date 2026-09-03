import pytest
from pickme.routing.fastpath import match

def test_summarize_with_count():
    intent = match("hey bot, summarize last 50 messages for me")
    assert intent is not None
    assert intent.action == "summarize"
    assert intent.count == 50

def test_summarize_without_count():
    intent = match("summarize please")
    assert intent is not None
    assert intent.action == "summarize"
    assert intent.count is None

def test_evaluate_everyone():
    intent = match("оцени всех")
    assert intent is not None
    assert intent.action == "evaluate"
    assert intent.target is not None
    assert intent.target.lower() in ("everyone", "всех", "все", "all", "всем")

def test_no_match_weather():
    intent = match("what is the weather")
    assert intent is None

def test_evaluate_mention():
    intent = match("evaluate @dan")
    assert intent is not None
    assert intent.action == "evaluate"
    assert intent.target == "@dan"
