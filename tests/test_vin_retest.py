import asyncio
import json
from types import SimpleNamespace

from autostop_manager.vin_retest import run_retest, save_receipt, technical_summary


def test_retest_short_route_keeps_diagnostics_without_persisting_payload(tmp_path):
    identifier = "WVWZZZ1KZAW000001"
    calls = []

    class Session:
        async def call_tool(self, tool, arguments, **kwargs):
            calls.append((tool, arguments))
            outcome = "empty_result" if arguments.get("operation") == "decodeVINus" else "partial"
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "outcome": outcome,
                    "ok": True,
                    "payload": {"VIN": identifier, "private": "DO_NOT_SAVE"},
                    "input_binding": {"identifier_sha256": "private_digest"},
                    "provider_diagnostics": {"error_codes": ["1", "7", "400"]},
                    "execution": {"network_calls": 1 if tool == "partsapi_catalog_lookup" else 0},
                    "field_statuses": {"make": {"status": "candidate", "alternatives": [{"value": "PRIVATE"}]}},
                },
            )

    schemas = {
        tool: {"properties": {"detail": {"enum": ["summary", "full"]}}}
        for tool in (
            "decode_vehicle_identity",
            "partsapi_catalog_lookup",
            "reconcile_vehicle_identity",
        )
    }
    receipt = asyncio.run(run_retest(Session(), identifier, schemas, full_control=True))
    assert len(calls) == 8
    assert all(arguments.get("detail") != "full" for _, arguments in calls)
    assert calls[-1][1]["detail"] == "summary"
    assert next(row for row in receipt["calls"] if row["key"] == "vinus_summary")["diagnostic_codes"] == [
        "1",
        "7",
        "400",
    ]
    save_receipt(tmp_path, receipt)
    saved = (tmp_path / "live-trace.json").read_text() + (tmp_path / "REPORT.md").read_text()
    assert all(secret not in saved for secret in (identifier, "private_digest", "DO_NOT_SAVE", "PRIVATE"))


def test_retest_client_failure_is_not_retried_or_logged_as_raw_text():
    calls = []

    class Session:
        async def call_tool(self, tool, arguments, **kwargs):
            calls.append(tool)
            raise RuntimeError("PRIVATE_TOKEN in provider URL")

    receipt = asyncio.run(run_retest(Session(), "WVWZZZ1KZAW000001", {}))
    assert len(calls) == 7
    assert "PRIVATE_TOKEN" not in json.dumps(receipt)
    assert all(row["outcome"] == "client_error" for row in receipt["calls"])
    assert receipt["network_calls"] is None
    assert receipt["automatic_retries"] is None


def test_technical_summary_keeps_actual_network_and_retry_counters():
    summary = technical_summary({"status": "partial", "processing": {"http_attempts": 2, "retry_count": 1}})
    assert summary["network_calls"] == 2
    assert summary["automatic_retries"] == 1


def test_malformed_diagnostics_and_metrics_never_escape_into_trace():
    summary = technical_summary(
        {
            "outcome": ["PRIVATE"],
            "field_statuses": {"engine": ["PRIVATE"]},
            "provider_diagnostics": {"error_codes": 7},
            "provider_errors": 400,
            "execution": {"network_calls": True, "attempts": "PRIVATE"},
            "tool_execution": {"call_id": "PRIVATE", "started_at": "PRIVATE", "wall_ms": {"private": "PRIVATE"}},
        }
    )
    assert "PRIVATE" not in json.dumps(summary)
    assert summary["network_calls"] is None
    assert summary["automatic_retries"] is None
    assert summary["tool_execution"] == {}


def test_distinct_source_attempts_do_not_imply_automatic_retry():
    summary = technical_summary(
        {
            "execution": {
                "network_calls": 2,
                "attempts": [
                    {"method": "vin"},
                    {"method": "wmi"},
                ],
            }
        }
    )
    assert summary["network_calls"] == 2
    assert summary["automatic_retries"] is None
    assert summary["reused"] is None
