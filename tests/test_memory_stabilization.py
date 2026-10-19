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
            {"capture_id": 5, "historical_status": "historical_verified"},
            {"capture_id": 6, "memory_visibility": "historical_evidence"},
        ]
    )

    assert [item["capture_id"] for item in current] == [1]
    assert [item["capture_id"] for item in historical] == [2, 3, 4, 5, 6]



def test_current_semantic_search_result_beats_historical_cards():
    from packages.memory_retrieval.ranking import fuse
    old = dict(capture_id=900,raw_content="SpikerBot robot connection layer",source="historical-import:markdown")
    new = dict(capture_id=104266,raw_content="SpikerBot robot connection layer",source="watson")
    result=fuse([('card_lexical',1,[old]),('semantic',1,[old,new])],2,'SpikerBot robot connection layer')
    assert [r['capture_id'] for r in result] == [104266,900]


def test_identifier_search_prefers_fresh_exact_capture_over_card():
    from packages.memory_retrieval.ranking import fuse
    new=dict(capture_id=11533,raw_content="6984856859858",source="addy")
    old=dict(capture_id=900,raw_content="6984856859858",source="historical-import:markdown")
    result=fuse([('exact',3,[new]),('alias',3,[old])],10,'6984856859858')
    assert [r['capture_id'] for r in result] == [11533,900]


def test_identifier_search_keeps_historical_exact_behind_card_alias():
    from packages.memory_retrieval.ranking import fuse
    old=dict(capture_id=901,raw_content="6986130790658",source="historical-import:markdown")
    new=dict(capture_id=902,raw_content="Current product alias",source="watson")
    result=fuse([('exact',3,[old]),('alias',3,[new])],10,'6986130790658')
    assert [r['capture_id'] for r in result] == [902,901]
