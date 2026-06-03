from services.memory_api.main import (
    choose_verification_query,
    is_identifier_query,
    redact_secrets,
    should_capture_memory,
)


def test_identifier_query_detects_long_numeric_ids():
    assert is_identifier_query("454302771395070")
    assert is_identifier_query("TEST_EXACT_ID_99887766")


def test_choose_verification_query_prefers_exact_number():
    note = "Jellop Meta Pixel ID is 454302771395070. BYB pixel is 1537689776542289."

    assert choose_verification_query(note) == "454302771395070"


def test_should_capture_memory_rejects_short_empty_tool_output():
    ok, reason = should_capture_memory("Nothing found.", source="test")

    assert not ok
    assert reason == "junk/empty tool output"


def test_should_capture_memory_accepts_durable_fact():
    ok, reason = should_capture_memory(
        "Decision: Greg confirmed BYB Meta Pixel ID is 1537689776542289.",
        source="test",
    )

    assert ok
    assert reason == "ok"


def test_redact_secrets_removes_bearer_token_material():
    redacted = redact_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456")

    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "[REDACTED_SECRET:kind=bearer_token" in redacted
