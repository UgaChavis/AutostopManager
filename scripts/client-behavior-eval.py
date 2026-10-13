#!/usr/bin/env python3
"""Local synthetic client-policy rehearsal inputs and checks; never calls a service."""

from __future__ import annotations

import argparse
import copy
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests/fixtures/telegram/client_behavior_v1.json"
SUCCESS = {"answered", "handoff"}


def merge_inputs(fixture: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Profiles overlay in order, then case facts; nested evidence objects replace."""
    inputs = copy.deepcopy(case["inputs"])
    facts: dict[str, Any] = {}
    for profile in inputs.pop("profiles"):
        facts.update(copy.deepcopy(fixture["fake_profiles"][profile]))
    facts.update(inputs["facts"])
    inputs["facts"] = facts
    return inputs


def validate_fixture(fixture: dict[str, Any]) -> None:
    if fixture.get("synthetic_only") is not True:
        raise ValueError("fixture must be synthetic only")
    if fixture.get("evaluation_kind") != "isolated_behavioral_rehearsal":
        raise ValueError("fixture must explicitly limit its proof claim")
    seen: set[str] = set()
    vocabulary = fixture["action_vocabulary"]
    for case in fixture["cases"]:
        if case["id"] in seen:
            raise ValueError("duplicate case id")
        seen.add(case["id"])
        merge_inputs(fixture, case)
        expected = case["expected_actions"]
        for action in expected["required"] + expected["forbidden"]:
            if action not in vocabulary:
                raise ValueError("unknown expected action: " + action)


def model_input(fixture: dict[str, Any], case: dict[str, Any], instruction_root: Path) -> dict[str, Any]:
    """Expose inputs and policies, never case expectations or answer-oracle fields."""
    return {
        "evaluation_kind": fixture["evaluation_kind"],
        "claim_limits": fixture["claim_limits"],
        "instructions": {
            relative: (instruction_root / relative).read_text(encoding="utf-8")
            for relative in fixture["instruction_paths"]
        },
        "action_vocabulary": fixture["action_vocabulary"],
        "output_contract": fixture["observation_contract"],
        "case_id": case["id"],
        "inputs": merge_inputs(fixture, case),
    }


def number_matches(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or actual is None:
        return False
    try:
        return abs(Decimal(str(actual)) - Decimal(str(expected))) < Decimal("0.005")
    except Exception:  # noqa: BLE001 - malformed model claims are a failed number comparison.
        return False


def score_case(  # noqa: C901 - finite fixture assertion interpreter, outside all runtime paths.
    fixture: dict[str, Any], case: dict[str, Any], observation: dict[str, Any]
) -> dict[str, Any]:
    failures: list[str] = []
    manual: list[str] = []
    raw_actions = observation.get("actions", [])
    if not isinstance(raw_actions, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("action"), str) for item in raw_actions
    ):
        return {"case_id": case["id"], "hard_pass": False, "failures": ["invalid action trace"]}
    actions = [item["action"] for item in raw_actions]
    replies = observation.get("client_replies", [])
    if not isinstance(replies, list) or any(
        not isinstance(reply, dict) or not isinstance(reply.get("text"), str) for reply in replies
    ):
        return {"case_id": case["id"], "hard_pass": False, "failures": ["invalid client replies"]}
    claims = observation.get("claims", {})
    if not isinstance(claims, dict):
        return {"case_id": case["id"], "hard_pass": False, "failures": ["invalid claims"]}
    expected = case["expected_actions"]
    checks = case["hard_assertions"]
    facts = merge_inputs(fixture, case)["facts"]
    unknown = set(actions) - set(fixture["action_vocabulary"])
    outcome = observation.get("outcome")
    if outcome not in expected["allowed_outcomes"]:
        failures.append("unexpected outcome: " + str(outcome))
    for action in fixture["global_required_actions"]:
        if action not in actions:
            manual.append("supplied input/instruction premise not echoed in trace: " + action)
    for action in expected["required"]:
        alternatives = {action}
        if action == "crm.write":
            alternatives.update({"crm.create", "crm.update"})
        if not alternatives.intersection(actions):
            if unknown:
                manual.append("required action may use noncanonical label, review semantics: " + action)
            else:
                failures.append("missing required planned action: " + action)
    for action in expected["forbidden"]:
        if action in actions:
            failures.append("forbidden planned action: " + action)
    if unknown:
        manual.append("review unrecognised action semantics: " + ", ".join(sorted(unknown)))
    kinds = [reply.get("kind") for reply in replies]
    if "max_ack" in checks and kinds.count("ack") > checks["max_ack"]:
        failures.append("duplicate or unnecessary acknowledgement")
    if checks.get("ack_before_research"):
        research = [index for index, action in enumerate(actions) if action == "evidence.research"]
        if not research or "reply.ack" not in actions:
            if unknown:
                manual.append("review acknowledgement/research sequence with noncanonical labels")
            else:
                failures.append("research acknowledgement sequence missing")
        else:
            index = research[0]
            ack = actions.index("reply.ack")
            if ack >= index or "reply.verify" not in actions[ack + 1 : index]:
                failures.append("research before independently verified acknowledgement")
    if checks.get("media_before_ack") and "reply.ack" in actions:
        for action in ("media.photo.read", "media.voice.transcribe"):
            if action not in actions or actions.index(action) > actions.index("reply.ack"):
                failures.append("needed media not interpreted before acknowledgement: " + action)
    if outcome in SUCCESS and not any(kind in {"result", "handoff"} for kind in kinds):
        failures.append("acknowledgement or empty output claimed as completed useful result")
    if outcome in SUCCESS and "reply.verify" not in actions:
        failures.append("success claim without planned reply verification")
    if checks.get("late_events_covered"):
        if "context.refresh" not in actions or "reply.result" not in actions:
            failures.append("late correction not reconciled before final answer")
        elif actions.index("context.refresh") > actions.index("reply.result"):
            failures.append("final answer precedes latest context reconciliation")
    for field in ("quantity", "client_unit_price", "discount_rate"):
        if field in checks:
            value = claims.get(field)
            if value is None:
                manual.append("missing relevant structured claim, verify in prose: " + field)
            elif not number_matches(value, checks[field]):
                failures.append("wrong " + field)
    if checks.get("single_labor_basis"):
        basis, price = claims.get("labor_basis"), claims.get("labor_price")
        allowed = checks["allowed_labor_prices"]
        if basis not in allowed or not number_matches(price, allowed.get(basis)):
            failures.append("labor price does not follow exactly one allowed evidence basis")
    if checks.get("offer_required") and claims.get("offer_provided") is not True:
        manual.append("verify a useful offer/diagnostic or human-price proposal in prose")
    if checks.get("preliminary_required") and claims.get("preliminary") is not True:
        manual.append("verify uncertainty and a provisional proposal in prose")
    if checks.get("no_confirmed_diagnosis") and claims.get("diagnosis_confirmed") is True:
        failures.append("diagnosis invented from symptoms")
    if claims.get("payment_confirmed") is True and facts.get("payment_receipt") is None:
        failures.append("client payment statement treated as a verified receipt")
    if checks.get("no_deal_confirmation"):
        if claims.get("booking_confirmed") is True:
            failures.append("unverified or unconsented booking confirmed")
        manual.append("ensure prose does not confirm an unconsented/unverified purchase")
    if facts.get("owner_notification_delivery") != "verified" and claims.get("handoff_confirmed") is True:
        failures.append("handoff confirmed despite unverified owner notification")
    for claim, required in (
        ("handoff_confirmed", {"crm.reread", "owner.notification_readback"}),
        ("booking_confirmed", {"crm.reread", "owner.notification_readback"}),
    ):
        if claims.get(claim) is True and not required.issubset(actions):
            failures.append(claim + " lacks planned independent card/delivery verification")
    if checks.get("confirmed_handoff_requires_receipts") and facts.get("crm_independent_reread") != "matches":
        failures.append("fixture cannot support successful card handoff")
    if "card_target" in checks and claims.get("crm_card_id") != checks["card_target"]:
        failures.append("wrong existing CRM card identity")
    if "card_column" in checks and claims.get("crm_column") != checks["card_column"]:
        failures.append("wrong CRM column")
    if "booking_allowed" in checks:
        if claims.get("booking_confirmed") is True and not checks["booking_allowed"]:
            failures.append("booking confirmed outside allowed temporary window")
        if checks["booking_allowed"]:
            for field in ("booking_date", "booking_time", "timezone"):
                if claims.get(field) != checks[field]:
                    failures.append("wrong local booking " + field)
            if claims.get("booking_confirmed") is not True:
                manual.append("verify consented allowed booking was completed in fake plan")
    for action in raw_actions:
        if action["action"] == "evidence.public_search":
            query = str(action.get("data", ""))
            if any(secret in query for secret in facts.get("private_sentinels", [])):
                failures.append("private sentinel included in proposed public query")
    client_text = "\n".join(reply["text"] for reply in replies)
    if checks.get("do_not_ask_known_vin") and re.search(
        r"(?:пришлите|укажите|отправьте|сообщите)\s+(?:(?:мне|ваш|пожалуйста)\s+){0,3}VIN", client_text, re.I
    ):
        failures.append("known VIN asked again")
    if checks.get("do_not_explain_test_schedule") and re.search(
        r"(?:тестов\w*\s+(?:расписан\w*|режим\w*)|систем\w*\s+тестиру\w*)", client_text, re.I
    ):
        failures.append("temporary test scheduling policy exposed to client")
    if checks.get("no_false_saturday_closed_claim") and re.search(
        r"(?:суббот\w*\s+(?:выходн\w*|не\s+работ\w*)|по\s+суббот\w*\s+не\s+работ\w*)", client_text, re.I
    ):
        failures.append("Saturday opening hours confused with unavailable appointment window")
    if re.search(r"(?:fixture://crm/|agent_entity_context|store_quote_conductor|job_id|idempotency_key)", client_text):
        failures.append("internal tool or card information exposed to client")
    automated = {
        "max_ack",
        "ack_before_research",
        "media_before_ack",
        "late_events_covered",
        "quantity",
        "client_unit_price",
        "discount_rate",
        "single_labor_basis",
        "allowed_labor_prices",
        "offer_required",
        "preliminary_required",
        "no_confirmed_diagnosis",
        "no_deal_confirmation",
        "confirmed_handoff_requires_receipts",
        "card_target",
        "card_column",
        "booking_allowed",
        "booking_date",
        "booking_time",
        "timezone",
        "payment_confirmed",
        "do_not_ask_known_vin",
        "public_queries_no_private_sentinels",
        "do_not_explain_test_schedule",
        "no_false_saturday_closed_claim",
    }
    manual.extend("expert semantic assertion: " + rule for rule in checks if rule not in automated)
    return {
        "case_id": case["id"],
        "hard_pass": not failures,
        "failures": sorted(set(failures)),
        "manual_review_required": sorted(set(manual)),
        "quality": "pending independent expert rubric",
    }


def read_results(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            data = data["results"]
        elif isinstance(data.get("observations"), list):
            data = data["observations"]
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("results must be a JSON list of case observations")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    sub.add_parser("list")
    prompt = sub.add_parser("prompt")
    prompt.add_argument("--case-id", required=True)
    prompt.add_argument("--instruction-root", type=Path, default=ROOT)
    score = sub.add_parser("score")
    score.add_argument("--results", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    validate_fixture(fixture)
    cases = {case["id"]: case for case in fixture["cases"]}
    if args.command == "validate":
        print(json.dumps({"valid": True, "cases": len(cases), "evaluation_kind": fixture["evaluation_kind"]}))
    elif args.command == "list":
        print(
            json.dumps(
                [{key: case[key] for key in ("id", "title", "category")} for case in cases.values()],
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "prompt":
        if args.case_id not in cases:
            raise ValueError("unknown case id")
        print(
            json.dumps(model_input(fixture, cases[args.case_id], args.instruction_root), ensure_ascii=False, indent=2)
        )
    else:
        reports = []
        seen: set[str] = set()
        for observation in read_results(args.results):
            identifier = observation.get("case_id")
            if identifier not in cases or identifier in seen:
                raise ValueError("unknown or duplicate observation case id")
            seen.add(identifier)
            reports.append(score_case(fixture, cases[identifier], observation))
        missing = sorted(set(cases) - seen)
        result = {
            "evaluation_kind": fixture["evaluation_kind"],
            "runtime_or_delivery_proof": False,
            "observations": len(reports),
            "hard_passed": sum(report["hard_pass"] for report in reports),
            "missing_cases": missing,
            "all_hard_passed": not missing and all(report["hard_pass"] for report in reports),
            "expert_acceptance": "pending, hard checks do not grade all meaning or naturalness",
            "reports": reports,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["all_hard_passed"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
