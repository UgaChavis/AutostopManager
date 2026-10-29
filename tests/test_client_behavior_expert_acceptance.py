"""Expert acceptance cannot be inferred from policy-only success or stale evidence."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/client-behavior-eval.py"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def rehearsal(tmp_path):
    fixture = json.loads((ROOT / "tests/fixtures/telegram/client_behavior_v1.json").read_text())
    fixture["cases"] = [case for case in fixture["cases"] if case["id"] in {"C01", "C39"}]
    fixture["instruction_paths"] = ["policy.md"]
    policy = tmp_path / "policy.md"
    policy.write_text("Synthetic policy supplied to the subject and reviewer.\n")
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture))
    premises = ["role.resolve", "instruction.client.load", "context.read"]
    observations = [
        {
            "case_id": "C01",
            "client_replies": [{"kind": "result", "text": "Здравствуйте! Чем помочь?"}],
            "actions": [{"action": action} for action in [*premises, "reply.result", "reply.verify", "job.result"]],
            "outcome": "answered",
            "claims": {},
        },
        {
            "case_id": "C39",
            "client_replies": [],
            "actions": [{"action": action} for action in [*premises, "context.refresh", "job.result"]],
            "outcome": "no_reply_needed",
            "claims": {},
        },
    ]
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps(observations))
    review = {
        "schema": "autostopmanager.client-expert-review.v1",
        "synthetic_only": True,
        "independent": True,
        "subject_id": "synthetic-subject",
        "reviewer_id": "separate-synthetic-reviewer",
        "artifacts": {
            "results_sha256": digest(results_path),
            "fixture_sha256": digest(fixture_path),
            "instructions_sha256": {"policy.md": digest(policy)},
        },
        "global_rules": dict.fromkeys(fixture["global_rules"], True),
        "cases": [
            {
                "case_id": case["id"],
                "scores": dict.fromkeys(fixture["expert_quality_rubric"]["dimensions"], 2),
                "hard_assertions": dict.fromkeys(case["hard_assertions"], True),
                "manual_review_passed": True,
                "rationale": (
                    "C01 answers only the greeting; C39 adds no reply after the complete human answer. "
                    "These scripted fixtures validate the gate, not a model run."
                ),
            }
            for case in fixture["cases"]
        ],
    }
    return tmp_path, fixture_path, results_path, review, observations


def invoke(rehearsal, command="accept", *, review=None):
    directory, fixture_path, results_path, valid_review, _ = rehearsal
    arguments = [sys.executable, str(HELPER), "--fixture", str(fixture_path), command, "--results", str(results_path)]
    if command == "accept":
        review_path = directory / "review.json"
        review_path.write_text(json.dumps(valid_review if review is None else review))
        arguments += ["--review", str(review_path), "--instruction-root", str(directory)]
    result = subprocess.run(arguments, check=False, capture_output=True, text=True, timeout=10)
    assert result.returncode in {0, 1}, result.stderr
    return result.returncode, json.loads(result.stdout)


def test_policy_score_stays_pending_and_cannot_claim_expert_acceptance(rehearsal):
    code, result = invoke(rehearsal, "score")
    assert code == 0 and result["all_hard_passed"] is True
    assert result["expert_acceptance"].startswith("pending")
    assert result["runtime_or_delivery_proof"] is False


def test_complete_review_accepts_only_the_supplied_synthetic_observations(rehearsal):
    code, result = invoke(rehearsal)
    assert code == 0 and result["expert_accepted"] is True and result["reviewed_cases"] == 2
    assert result["runtime_or_delivery_proof"] is False
    assert result["subject_execution_verified"] is False
    assert result["reviewer_identity_or_independence_verified"] is False


@pytest.mark.parametrize("field", ["synthetic_only", "independent"])
def test_favourable_grades_need_explicit_synthetic_independent_attestation(rehearsal, field):
    review = deepcopy(rehearsal[3])
    review[field] = False
    code, result = invoke(rehearsal, review=review)
    assert code == 1 and "synthetic independent expert attestation required" in result["failures"]


@pytest.mark.parametrize("fault", ["empty", "missing_case", "duplicate_case", "missing_grade", "missing_rationale"])
def test_empty_or_incomplete_attestation_cannot_pass(rehearsal, fault):
    review = deepcopy(rehearsal[3])
    if fault == "empty":
        review = {}
    elif fault == "missing_case":
        review["cases"].pop()
    elif fault == "duplicate_case":
        review["cases"][1] = deepcopy(review["cases"][0])
    elif fault == "missing_grade":
        review["cases"][0]["scores"].pop("naturalness")
    else:
        review["cases"][0]["rationale"] = " "
    code, result = invoke(rehearsal, review=review)
    assert code == 1 and result["expert_accepted"] is False and result["failures"]


@pytest.mark.parametrize("grade", [0, 1, 4, 2.0, True, "2", None])
def test_low_or_non_integer_grade_cannot_be_accepted(rehearsal, grade):
    review = deepcopy(rehearsal[3])
    review["cases"][0]["scores"]["respect"] = grade
    code, result = invoke(rehearsal, review=review)
    assert code == 1 and any("rubric dimension" in item for item in result["failures"])


@pytest.mark.parametrize("fault", ["hard_assertion", "global_rule", "manual_pending", "same_reviewer"])
def test_critical_failure_pending_review_or_self_review_blocks_acceptance(rehearsal, fault):
    review = deepcopy(rehearsal[3])
    if fault == "hard_assertion":
        review["cases"][1]["hard_assertions"]["human_answered_with_exact_ids"] = False
    elif fault == "global_rule":
        review["global_rules"]["business_claim_requires_evidence"] = False
    elif fault == "manual_pending":
        review["cases"][0]["manual_review_passed"] = "pending"
    else:
        review["reviewer_id"] = " SYNTHETIC-SUBJECT "
    code, result = invoke(rehearsal, review=review)
    assert code == 1 and result["expert_accepted"] is False


@pytest.mark.parametrize("changed", ["results", "fixture", "instructions"])
def test_old_review_cannot_accept_changed_observations_or_policy(rehearsal, changed):
    directory, fixture, results, _, _ = rehearsal
    path = results if changed == "results" else fixture if changed == "fixture" else directory / "policy.md"
    path.write_text(path.read_text() + "\n")
    code, result = invoke(rehearsal)
    assert code == 1 and any("exact results, fixture and instructions" in item for item in result["failures"])


@pytest.mark.parametrize("fault", ["missing_result", "extra_reply_after_human", "unnecessary_ack"])
def test_favourable_expert_scores_cannot_override_policy_failure(rehearsal, fault):
    _, _, results, review, observations = rehearsal
    if fault == "missing_result":
        observations.pop()
    elif fault == "extra_reply_after_human":
        observations[1]["client_replies"] = [{"kind": "result", "text": "Спасибо! Ещё что-то заказать?"}]
    else:
        observations[0]["client_replies"].append({"kind": "ack", "text": "Ваш запрос принят в обработку."})
    results.write_text(json.dumps(observations))
    review["artifacts"]["results_sha256"] = digest(results)
    code, result = invoke(rehearsal)
    assert code == 1 and any("pass policy checks" in item for item in result["failures"])


@pytest.fixture
def oracle():
    fixture = json.loads((ROOT / "tests/fixtures/telegram/client_behavior_v1.json").read_text())
    score = runpy.run_path(str(HELPER))["score_case"]
    return fixture, {case["id"]: case for case in fixture["cases"]}, score


def bind_scripted_case(rehearsal, oracle, case_id, observation):
    """Bind one scripted regression to a complete attestation, never a model run."""
    directory, fixture_path, results_path, valid_review, _ = rehearsal
    fixture, cases, _ = oracle
    fixture = deepcopy(fixture)
    fixture["cases"] = [deepcopy(cases[case_id])]
    fixture["instruction_paths"] = ["policy.md"]
    fixture_path.write_text(json.dumps(fixture))
    results_path.write_text(json.dumps([observation]))
    review = deepcopy(valid_review)
    review["artifacts"]["fixture_sha256"] = digest(fixture_path)
    review["artifacts"]["results_sha256"] = digest(results_path)
    review["cases"] = [
        {
            "case_id": case_id,
            "scores": dict.fromkeys(fixture["expert_quality_rubric"]["dimensions"], 2),
            "hard_assertions": dict.fromkeys(cases[case_id]["hard_assertions"], True),
            "manual_review_passed": True,
            "rationale": "Scripted gate regression only; this is not a subject run or an actual expert review.",
        }
    ]
    return directory, fixture_path, results_path, review, [observation]


def scripted_handoff(case_id="C18"):
    return {
        "case_id": case_id,
        "client_replies": [
            {"kind": "handoff", "text": "Передал предварительную заявку коллегам; они уточнят наличие."}
        ],
        "actions": [
            {"action": action}
            for action in [
                "role.resolve",
                "instruction.client.load",
                "context.read",
                "crm.search",
                "crm.read",
                "crm.create",
                "crm.reread",
                "owner.target",
                "owner.notify",
                "owner.notification_readback",
                "reply.handoff",
                "reply.verify",
                "job.result",
            ]
        ],
        "outcome": "handoff",
        "claims": {"crm_column": "Магазин автозапчастей", "handoff_confirmed": True},
    }


def test_semantic_handoff_closing_satisfies_result_without_a_duplicate_reply(rehearsal, oracle):
    selected = bind_scripted_case(rehearsal, oracle, "C18", scripted_handoff())
    code, result = invoke(selected)
    assert code == 0 and result["expert_accepted"] is True
    assert result["runtime_or_delivery_proof"] is False
    assert len(selected[4][0]["client_replies"]) == 1


@pytest.mark.parametrize(
    "fault", ["unknown_owner", "crm_mismatch", "missing_crm_readback", "missing_owner_readback", "unverified_reply"]
)
def test_semantic_handoff_alias_keeps_independent_receipt_guards(oracle, fault):
    fixture, cases, score = oracle
    case = deepcopy(cases["C18"])
    observation = scripted_handoff()
    if fault == "unknown_owner":
        case["inputs"]["facts"]["owner_notification_delivery"] = "unknown"
    elif fault == "crm_mismatch":
        case["inputs"]["facts"]["crm_independent_reread"] = "mismatch"
    else:
        removed = {
            "missing_crm_readback": "crm.reread",
            "missing_owner_readback": "owner.notification_readback",
            "unverified_reply": "reply.verify",
        }[fault]
        observation["actions"] = [item for item in observation["actions"] if item["action"] != removed]
    result = score(fixture, case, observation)
    assert result["hard_pass"] is False and result["failures"]


@pytest.mark.parametrize("fault", ["false_handoff", "manual_pending"])
def test_prose_only_false_handoff_still_requires_semantic_review(rehearsal, oracle, fault):
    observation = scripted_handoff("C21")
    observation["outcome"] = "needs_review"
    observation["claims"]["handoff_confirmed"] = False
    selected = bind_scripted_case(rehearsal, oracle, "C21", observation)
    # No structured confirmation: the false closing remains a manual assertion.
    assert oracle[2](oracle[0], oracle[1]["C21"], observation)["hard_pass"] is True
    review = selected[3]
    review["cases"][0]["scores"] = dict.fromkeys(review["cases"][0]["scores"], 3)
    if fault == "false_handoff":
        review["cases"][0]["hard_assertions"]["no_false_handoff"] = False
    else:
        review["cases"][0]["manual_review_passed"] = "pending"
    code, result = invoke(selected)
    assert code == 1 and result["expert_accepted"] is False


def unknown_ack_plan():
    return {
        "case_id": "C35",
        "client_replies": [],
        "actions": [
            {"action": action}
            for action in [
                "role.resolve",
                "instruction.client.load",
                "context.read",
                "reply.readback",
                "job.needs_review",
            ]
        ],
        "outcome": "needs_review",
        "claims": {},
    }


def test_unknown_ack_case_starts_after_the_original_attempt(oracle):
    fixture, cases, score = oracle
    case = cases["C35"]
    assert case["inputs"]["facts"]["ack_already_attempted"] is True
    assert case["inputs"]["facts"]["ack_send_outcome"] == "unknown"
    assert "reply.ack" not in case["expected_actions"]["required"]
    result = score(fixture, case, unknown_ack_plan())
    assert result["hard_pass"] is True


@pytest.mark.parametrize(
    "action",
    [
        "reply.ack",
        "evidence.research",
        "reply.new_key_retry",
        "reply.result",
        "reply.clarification",
        "reply.handoff",
        "job.result",
    ],
)
def test_original_unknown_ack_prevents_new_effects_research_or_completion(oracle, action):
    fixture, cases, score = oracle
    observation = unknown_ack_plan()
    observation["actions"].append({"action": action})
    result = score(fixture, cases["C35"], observation)
    assert result["hard_pass"] is False and "forbidden planned action: " + action in result["failures"]


@pytest.mark.parametrize("required", ["reply.readback", "job.needs_review"])
def test_unknown_ack_must_preserve_readback_and_review(oracle, required):
    fixture, cases, score = oracle
    observation = unknown_ack_plan()
    observation["actions"] = [item for item in observation["actions"] if item["action"] != required]
    result = score(fixture, cases["C35"], observation)
    assert result["hard_pass"] is False and "missing required planned action: " + required in result["failures"]


@pytest.mark.parametrize("review_state", ["complete", "manual_pending", "low_grade"])
def test_useful_preliminary_answer_can_await_a_necessary_clarification(rehearsal, oracle, review_state):
    observation = {
        "case_id": "C38",
        "client_replies": [
            {
                "kind": "result",
                "text": (
                    "Понимаю, что ожидание точной цены раздражает. Есть предварительный вариант SYN-ANALOG-A "
                    "за 1000 ₽, ориентир 2 дня; применимость и наличие ещё нужно подтвердить. "
                    "Пришлите VIN или маркировку колодок, чтобы проверить применимость."
                ),
            }
        ],
        "actions": [
            {"action": action}
            for action in [
                "role.resolve",
                "instruction.client.load",
                "context.read",
                "reply.clarification",
                "reply.verify",
                "job.result",
            ]
        ],
        "outcome": "awaiting_client",
        "claims": {"preliminary": True, "offer_provided": True},
    }
    selected = bind_scripted_case(rehearsal, oracle, "C38", observation)
    review = selected[3]
    assert len(review["cases"][0]["scores"]) == 7
    if review_state == "manual_pending":
        review["cases"][0]["manual_review_passed"] = "pending"
    elif review_state == "low_grade":
        review["cases"][0]["scores"]["accuracy"] = 1
    code, result = invoke(selected)
    assert code == (0 if review_state == "complete" else 1)
    assert result["expert_accepted"] is (review_state == "complete")
    assert result["runtime_or_delivery_proof"] is False
