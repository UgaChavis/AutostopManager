"""Keep the opt-in benchmark's observed receipts and cleanup executable."""

import asyncio
import os
from pathlib import Path
import tempfile

import pytest
from telegram_stability_benchmark import database, pipeline


@pytest.mark.skipif(os.geteuid() != 0, reason="native bridge credential checks require root")
def test_benchmark_observes_independent_client_results_and_settled_cleanup():
    with tempfile.TemporaryDirectory(prefix="mng-bench-test-") as temporary:
        root = Path(temporary)
        result = asyncio.run(pipeline(root / "pipeline", clients=2, messages=2, actor_lanes=2))
        assert result["covered_events"] == 4
        assert result["verified_sends"] == 4
        assert result["leaked_tasks"] == 0
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
