"""Keep the opt-in benchmark's observed receipts and cleanup executable."""

import asyncio
import os
from pathlib import Path
import tempfile

import pytest
from telegram_stability_benchmark import database, paired_text_latency, run_pipeline


@pytest.mark.skipif(os.geteuid() != 0, reason="native bridge credential checks require root")
def test_benchmark_observes_independent_client_results_and_settled_cleanup():
    with tempfile.TemporaryDirectory(prefix="mng-bench-test-") as temporary:
        root = Path(temporary)
        result = run_pipeline(root / "pipeline", clients=2, messages=2, actor_lanes=2)
        assert result["covered_events"] == 4
        assert result["verified_sends"] == 4
        assert result["leaked_tasks"] == 0
        assert all(seconds >= 0 for seconds in result["phase_seconds"].values())
        assert sum(result["phase_seconds"].values()) <= result["elapsed_seconds"]
        for stage in (
            "queue_wait",
            "claim_to_context_ready",
            "accepted_to_result_receipt",
            "accepted_to_terminal_proof",
        ):
            assert result["stages"][stage]["count"] == 2
            assert result["stages"][stage]["max_seconds"] >= 0
        assert result["stage_timings"]["new"]["peer_prepare"]["count"] == 2
        state = database(root / "database", retained=32)
        assert state["retained_peers"] == 32
        assert state["quiet_peers_status"]["count"] == 5
    assert not root.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="native bridge credential checks require root")
def test_paired_text_benchmark_proves_one_reply_per_case_and_reuses_thread():
    with tempfile.TemporaryDirectory(prefix="mng-bench-pair-") as temporary:
        result = asyncio.run(paired_text_latency(Path(temporary) / "pair"))

    assert result["bootstrap_ready_seconds"] >= 0
    assert [case["condition"] for case in result["cases"]] == ["create_thread", "reuse_thread"]
    for case in result["cases"]:
        assert case["verified_replies"] == 1 and case["execution_proof"] == "completed"
        assert all(value >= 0 for value in case["driver_seconds"].values())
        for service in ("bridge", "wake"):
            assert all(cell["count"] == 1 and cell["seconds"] >= 0 for cell in case[service].values())
