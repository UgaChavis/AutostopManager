"""Preserve partial supplier-read evidence while accepting the prior producer."""

import pytest

import autostop_manager.store_api as store_api_module

from test_store_api import _Response, _client, _envelope


@pytest.mark.parametrize(
    "warnings",
    [
        [],
        ["sourcing_provider_deadline_exceeded"],
        ["sourcing_provider_capacity_exhausted"],
        ["sourcing_provider_unavailable"],
    ],
)
def test_sourcing_search_preserves_partial_warnings_and_old_producer(monkeypatch, warnings):
    payload = _envelope(
        summary={"entity": "store_sourcing_offer", "matches": 1},
        items=[
            {
                "entity": "store_sourcing_offer",
                "id": "synthetic-offer",
                "provider": "synthetic",
                "article": "TEST-PART",
                "fitment_confidence": "UNVERIFIED",
            }
        ],
        next_cursor=None,
    )
    payload["warnings"] = warnings
    monkeypatch.setattr(store_api_module, "urlopen", lambda *a, **kw: _Response(payload))
    result = _client(quote_token="synthetic-quote-secret").search(entity="store_sourcing_offer", query_text="TEST-PART")
    assert result["ok"] is True
    assert result["warnings"] == warnings
    assert result["items"][0]["fitment_confidence"] == "UNVERIFIED"
    assert "synthetic-quote-secret" not in str(result)


@pytest.mark.parametrize("warning", sorted(store_api_module._SOURCING_WARNING_VALUES))
def test_empty_sourcing_partial_warning_is_not_dropped(monkeypatch, warning):
    payload = _envelope(summary={"entity": "store_sourcing_offer", "matches": 0}, next_cursor=None)
    payload["warnings"] = [warning]
    monkeypatch.setattr(store_api_module, "urlopen", lambda *a, **kw: _Response(payload))
    result = _client(quote_token="synthetic-quote-secret").search(entity="store_sourcing_offer", query_text="TEST-PART")
    assert result["ok"] is True
    assert result["items"] == []
    assert result["warnings"] == [warning]


@pytest.mark.parametrize("warning", sorted(store_api_module._SOURCING_WARNING_VALUES))
@pytest.mark.parametrize("contract", ["runtime", "digest", "entity", "action", "order_search"])
def test_sourcing_warning_rejected_on_other_contracts(warning, contract):
    payload = _envelope()
    payload["warnings"] = [warning]
    with pytest.raises(ValueError, match="only for sourcing searches"):
        store_api_module._validate_contract_payload(
            payload,
            response_contract="search" if contract == "order_search" else contract,
            expected_entity="store_order",
            expected_detail=None,
            expected_operation="set_order_payment_status" if contract == "action" else None,
        )


def test_sourcing_unknown_warning_remains_fail_closed(monkeypatch):
    payload = _envelope(summary={"entity": "store_sourcing_offer", "matches": 0})
    payload["warnings"] = ["arbitrary_provider_message"]
    monkeypatch.setattr(store_api_module, "urlopen", lambda *a, **kw: _Response(payload))
    result = _client(quote_token="synthetic-quote-secret").search(entity="store_sourcing_offer", query_text="TEST-PART")
    assert result["ok"] is False
    assert result["summary"]["error_code"] == "store_response_schema_invalid"
