from __future__ import annotations

from copy import deepcopy

import pytest

from autostop_manager import work_pricing as pricing, work_pricing_research as research

OPERATION = "замена масла"
ERROR = {"ok": False, "error": "invalid_ready_price_evidence", "execution": {"network_calls": 0}}


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("implicit private or network read forbidden")

    monkeypatch.setattr(pricing, "_load_labor_experience", denied)
    monkeypatch.setattr(research, "urlopen", denied)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))


def rows(count, kind="unique", prefix="a"):
    if kind == "invalid":
        return [{} for _ in range(count)]
    return [
        {
            "source": f"{prefix}{index if kind == 'unique' else 0}.example",
            "operation_name": OPERATION,
            "hours": 1,
            "public_source": True,
            "confidence": "high",
        }
        for index in range(count)
    ]


def payload(labor):
    return {"work_items": [OPERATION], "vehicle_context": {"make": "DEMO"}, "observations": [], "labor": labor}


def wrap(data, envelope):
    return data if envelope == "direct" else {"ok": True, "outcome": envelope, "data": data}


def estimate(ready, manual=()):
    return pricing.estimate_repair_work_cost(
        make="DEMO",
        work_items=[OPERATION],
        price_evidence=ready,
        quotes_json={"labor_time_sample": list(manual)},
        auto_research=False,
        use_internal_experience=False,
    )


@pytest.mark.parametrize("envelope", ["direct", "success", "partial"])
@pytest.mark.parametrize("kind", ["unique", "duplicate", "invalid"])
@pytest.mark.parametrize("count", [0, 100, 101])
def test_ready_labor_budget_counts_raw_rows_before_dedupe(envelope, kind, count):
    ready = wrap(payload(rows(count, kind)), envelope)
    before = deepcopy(ready)
    actual = estimate(ready)
    assert ready == before
    if count > 100:
        assert actual == ERROR
    else:
        assert actual["ok"] is True
        sample = actual["labor_time_sample"]
        assert sample["valid_count"] == (0 if kind == "invalid" else min(count, 1) if kind == "duplicate" else count)
        assert sample["invalid_count"] == (count if kind == "invalid" else 0)


class OversizedWithoutIteration(list):
    def __len__(self):
        return 101

    def __iter__(self):
        raise AssertionError("oversized labor must be rejected before iteration")


@pytest.mark.parametrize("envelope", ["direct", "success", "partial"])
def test_oversized_labor_is_rejected_without_visiting_its_rows(envelope):
    assert estimate(wrap(payload(OversizedWithoutIteration()), envelope)) == ERROR


@pytest.mark.parametrize("envelope", ["direct", "success", "partial"])
def test_oversized_labor_never_reaches_normalization_analysis_or_acquisition(monkeypatch, envelope):
    def denied(*_args, **_kwargs):
        raise AssertionError("oversized labor reached expensive processing")

    for name in (
        "_normalize_operations",
        "_normalize_quote_sample",
        "_normalize_labor_time_sample",
        "_operation_labor_time_analysis",
        "collect_public_work_pricing_research",
        "_load_labor_experience",
    ):
        monkeypatch.setattr(pricing, name, denied)
    assert estimate(wrap(payload(rows(101)), envelope)) == ERROR


@pytest.mark.parametrize("envelope", ["direct", "success", "partial"])
def test_each_input_retains_its_own_hundred_row_budget(envelope):
    actual = estimate(wrap(payload(rows(100)), envelope), manual=rows(100, prefix="b"))
    assert actual["ok"] is True
    assert actual["labor_time_sample"]["valid_count"] == 200
    assert actual["operation_estimates"][0]["labor_time_analysis"]["source_count"] == 200


@pytest.mark.parametrize("envelope", ["direct", "success", "partial"])
def test_manual_labor_budget_remains_independent(envelope):
    actual = estimate(wrap(payload([]), envelope), manual=rows(101))
    assert actual["error"] == "invalid_input"
    assert actual["limits"]["labor_time_rows"] == 100


@pytest.mark.parametrize("envelope", ["direct", "success", "partial"])
def test_ready_labor_uses_its_own_existing_limit_constant(monkeypatch, envelope):
    monkeypatch.setattr(pricing, "MAX_LABOR_TIME_ROWS", 2)
    monkeypatch.setattr(pricing, "MAX_QUOTE_ROWS", 5)
    data = payload(rows(2))
    data["observations"] = [{}] * 5
    assert estimate(wrap(data, envelope))["ok"] is True
    data["labor"] = rows(3)
    assert estimate(wrap(data, envelope)) == ERROR


@pytest.mark.parametrize("envelope", ["direct", "success", "partial"])
@pytest.mark.parametrize("labor", [None, {}, "rows", 1, False, (), [None], [{}, None]])
def test_malformed_labor_still_returns_the_existing_structural_error(envelope, labor):
    assert estimate(wrap(payload(labor), envelope)) == ERROR


@pytest.mark.parametrize("envelope", ["direct", "success", "partial"])
def test_omitted_labor_remains_optional_and_observation_budget_is_preserved(envelope):
    data = payload([])
    data.pop("labor")
    assert estimate(wrap(data, envelope))["ok"] is True
    data["observations"] = [{}] * 101
    assert estimate(wrap(data, envelope)) == ERROR


@pytest.mark.parametrize("envelope", ["direct", "success", "partial"])
@pytest.mark.parametrize("count", [1, 101])
def test_structural_budget_precedes_context_matching_for_oversized_input(envelope, count):
    data = payload(rows(count))
    data["vehicle_context"] = {"make": "OTHER"}
    actual = estimate(wrap(data, envelope))
    assert actual["error"] == ("invalid_ready_price_evidence" if count > 100 else "ready_price_context_mismatch")
    assert actual["execution"]["network_calls"] == 0
