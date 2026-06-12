from fastapi import HTTPException

from services.memory_api import main as memory_main
from services.memory_api.main import (
    choose_verification_query,
    is_identifier_query,
    is_inactive_memory_status,
    normalize_memory_status,
    split_historical_import_results,
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


def test_memory_status_normalization_allows_revision_statuses():
    assert normalize_memory_status(" Superseded ") == "superseded"
    assert is_inactive_memory_status("deleted")
    assert not is_inactive_memory_status("active")


def test_memory_status_normalization_rejects_unknown_status():
    try:
        normalize_memory_status("maybe-later")
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("Expected invalid memory status to raise")


def test_split_historical_import_results_keeps_current_first():
    current, historical = split_historical_import_results(
        [
            {"capture_id": 1, "source": "watson"},
            {"capture_id": 2, "source": "historical-import:markdown"},
            {"capture_id": 3, "historical_until_verified": True},
            {"capture_id": 4, "historical_status": "stale"},
        ]
    )

    assert [item["capture_id"] for item in current] == [1]
    assert [item["capture_id"] for item in historical] == [2, 3, 4]


def test_identifier_search_prefers_fresh_exact_capture_over_card(monkeypatch):
    monkeypatch.setattr(
        memory_main,
        "exact_capture_search",
        lambda *args, **kwargs: [{"capture_id": 11533, "chunk_id": None, "match_type": "exact", "source": "addy"}],
    )
    monkeypatch.setattr(
        memory_main,
        "exact_card_alias_search",
        lambda *args, **kwargs: [{"capture_id": 900, "chunk_id": None, "match_type": "card_exact", "source": "historical-import:markdown"}],
    )
    monkeypatch.setattr(memory_main, "lexical_card_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(memory_main, "lexical_capture_search", lambda *args, **kwargs: [])

    results = memory_main.search_memory_records(
        q="6984856859858",
        limit=10,
        project_slug=None,
        session=None,
        user_id="greg",
        include_semantic=False,
    )

    assert [item["capture_id"] for item in results[:2]] == [11533, 900]


def test_identifier_search_keeps_historical_exact_behind_card_alias(monkeypatch):
    monkeypatch.setattr(
        memory_main,
        "exact_capture_search",
        lambda *args, **kwargs: [
            {"capture_id": 901, "chunk_id": None, "match_type": "exact", "source": "historical-import:markdown"}
        ],
    )
    monkeypatch.setattr(
        memory_main,
        "exact_card_alias_search",
        lambda *args, **kwargs: [{"capture_id": 902, "chunk_id": None, "match_type": "card_exact", "source": "watson"}],
    )
    monkeypatch.setattr(memory_main, "lexical_card_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(memory_main, "lexical_capture_search", lambda *args, **kwargs: [])

    results = memory_main.search_memory_records(
        q="6986130790658",
        limit=10,
        project_slug=None,
        session=None,
        user_id="greg",
        include_semantic=False,
    )

    assert [item["capture_id"] for item in results[:2]] == [902, 901]
