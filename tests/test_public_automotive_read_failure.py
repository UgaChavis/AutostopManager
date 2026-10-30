"""A failed NHTSA body read must not discard independent reference routes."""

import pytest

from autostop_manager import public_automotive_evidence as evidence


@pytest.mark.parametrize("error", [ConnectionResetError("connection reset"), OSError("read failed")])
@pytest.mark.parametrize("composite", [False, True])
def test_nhtsa_response_read_failure_is_a_source_failure(monkeypatch, error, composite):
    calls = []
    closed = []

    class BrokenResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)
            return False

        def read(self, size):
            raise error

    def fake_urlopen(request, *, timeout):
        calls.append((request.full_url, timeout))
        return BrokenResponse()

    monkeypatch.setattr(evidence, "urlopen", fake_urlopen)
    context = {"make": "Mercedes-Benz", "model": "Synthetic", "model_year": 2020}
    if composite:
        result = evidence.lookup_public_automotive_evidence(**context, topics=["recalls", "fluids"])
        source = result["evidence"][0]
        assert result["confidence"] == "low"
        assert result["evidence"][1]["records"][0]["source_id"] == "mercedes_operating_fluids"
    else:
        result = source = evidence.lookup_nhtsa_recalls(**context)

    assert result["ok"] is False
    assert source["ok"] is False
    assert source["source_id"] == "nhtsa_datasets_apis"
    assert source["records"] == []
    assert "record_count" not in source
    assert source["checked_at"]
    assert len(calls) == 1
    assert closed == [True]

    class HealthyResponse(BrokenResponse):
        chunks = iter([b'{"Count":1,"results":[{"NHTSACampaignNumber":"synthetic"}]}', b""])

        def read(self, size):
            return next(self.chunks)

    def healthy_urlopen(request, *, timeout):
        calls.append((request.full_url, timeout))
        return HealthyResponse()

    monkeypatch.setattr(evidence, "urlopen", healthy_urlopen)
    recovered = evidence.lookup_nhtsa_recalls(**context)
    assert recovered["ok"] is True
    assert recovered["records"][0]["campaign_number"] == "synthetic"
    assert len(calls) == 2
    assert closed == [True, True]
