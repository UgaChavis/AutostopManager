from __future__ import annotations

import json

from autostop_manager.vin_lookup import decode_vin_vpic


class _FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_vpic_decode_request_uses_model_year_and_json(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=10.0):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        payload = {
            "Results": [
                {
                    "VIN": "1HGCM82633A004352",
                    "Make": "Honda",
                    "Model": "Accord",
                    "ModelYear": "2003",
                }
            ]
        }
        return _FakeResponse(payload)

    monkeypatch.setattr("autostop_manager.vin_lookup.urlopen", fake_urlopen)

    result = decode_vin_vpic("1HGCM82633A004352", model_year=2003)

    assert result["ok"] is True
    assert result["source"] == "NHTSA vPIC"
    assert "modelyear=2003" in captured["url"]
    assert result["vehicle"]["make"] == "Honda"
    assert result["vehicle"]["modelyear"] == 2003
