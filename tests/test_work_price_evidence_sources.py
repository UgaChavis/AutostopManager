"""Synthetic validation at the public labor-evidence boundary."""

import socket

import pytest

from autostop_manager.automotive_labor import collect_work_price_evidence


@pytest.mark.parametrize("sources", [[{}], [[]], ["public_web", {}], [None], [True], [1], ["unsupported"]])
def test_invalid_sources_are_rejected_before_any_network(monkeypatch, sources):
    def denied(*_args, **_kwargs):
        raise AssertionError("invalid sources must not start network access")

    monkeypatch.setattr(socket, "socket", denied)
    outcome = collect_work_price_evidence(["demo work"], sources=sources)
    assert outcome["outcome"] == "invalid_input"
    assert outcome["execution"]["network_calls"] == 0


def test_supported_aggregate_source_remains_usable(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("aggregate-only acquisition must not use network")

    monkeypatch.setattr(socket, "socket", denied)
    aggregate = {"count": 3}
    outcome = collect_work_price_evidence(["demo work"], sources=["provided_aggregate"], aggregate_evidence=aggregate)
    assert outcome["outcome"] == "success"
    assert outcome["data"]["provided_aggregate"] == aggregate
    assert outcome["execution"]["network_calls"] == 0
