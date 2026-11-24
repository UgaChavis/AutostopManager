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


@pytest.mark.parametrize(("data", "valid"), [("Synthetic evidence", True), ({"id": "FAKE"}, False), (None, False)])
def test_action_data_is_optional_text(rehearsal, data, valid):
    _, _, results_path, review, observations = rehearsal
    observations[0]["actions"][0]["data"] = data
    results_path.write_text(json.dumps(observations))
    review["artifacts"]["results_sha256"] = digest(results_path)

    code, score = invoke(rehearsal, "score")
    assert code == (0 if valid else 1)
    assert score["all_hard_passed"] is valid
    assert score["reports"][0]["failures"] == ([] if valid else ["invalid action trace"])
    code, acceptance = invoke(rehearsal)
    assert code == (0 if valid else 1)
    assert acceptance["expert_accepted"] is valid


@pytest.mark.parametrize(
    ("fields", "position"),
    [
        pytest.param({}, 0, id="missing"),
        pytest.param({"kind": None}, 0, id="null"),
        pytest.param({"kind": {"value": "result"}}, 0, id="object"),
        pytest.param({"kind": ["result"]}, 0, id="array"),
        pytest.param({"kind": "unknown"}, 1, id="unknown_after_result"),
    ],
)
def test_invalid_client_reply_kind_rejects_case_without_losing_other_reports(rehearsal, fields, position):
    _, _, results_path, review, observations = rehearsal
    observations[0]["client_replies"].insert(position, {"text": "Синтетический ответ клиенту.", **fields})
    results_path.write_text(json.dumps(observations))
    review["artifacts"]["results_sha256"] = digest(results_path)

    code, score = invoke(rehearsal, "score")
    assert code == 1 and score["reports"][0]["failures"] == ["invalid client replies"]
    assert score["reports"][1]["hard_pass"] is True
    code, acceptance = invoke(rehearsal)
    assert code == 1 and acceptance["expert_accepted"] is False


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
    ("case_id", "answer"),
    [
        (
            "C03",
            "Начните с проверки установки колодок и состояния направляющих. По одному звуку причину не определить.",
        ),
        ("C30", "Могу назвать вашу цену. Ориентировочно 1000 ₽, наличие и срок ещё уточню."),
    ],
    ids=["C03", "C30"],
)
@pytest.mark.parametrize("verification", ["ack_only", "after_close", "before_close"])
def test_consultation_verifies_final_reply_separately_from_ack(rehearsal, oracle, case_id, answer, verification):
    fixture, _, _ = oracle
    closing = {
        "ack_only": ["reply.result", "job.result"],
        "after_close": ["reply.result", "job.result", "reply.verify"],
        "before_close": ["reply.result", "reply.verify", "job.result"],
    }[verification]
    observation = {
        "case_id": case_id,
        "client_replies": [
            {"kind": "ack", "text": "Проверю и напишу."},
            {"kind": "result", "text": answer},
        ],
        "actions": [
            {"action": action}
            for action in [
                *fixture["global_required_actions"],
                "reply.ack",
                "reply.verify",
                "evidence.research",
                "context.refresh",
                *closing,
            ]
        ],
        "outcome": "answered",
    }
    selected = bind_scripted_case(rehearsal, oracle, case_id, observation)
    score_code, scored = invoke(selected, "score")
    acceptance_code, accepted = invoke(selected)
    assert score_code == acceptance_code == (0 if verification == "before_close" else 1)
    assert accepted["expert_accepted"] is (verification == "before_close")
    assert scored["reports"][0]["failures"] == (
        [] if verification == "before_close" else ["final reply verification must precede successful completion"]
    )


@pytest.mark.parametrize("other_reply", ["reply.result", "reply.ack", "reply.clarification", "reply.media"])
def test_handoff_verification_cannot_be_borrowed_from_another_client_effect(oracle, other_reply):
    fixture, cases, score = oracle
    observation = scripted_handoff()
    closing = next(i for i, action in enumerate(observation["actions"]) if action["action"] == "reply.handoff")
    if other_reply == "reply.result":
        observation["actions"][closing : closing + 2] = [
            {"action": "reply.result"},
            {"action": "reply.verify"},
            {"action": "reply.handoff"},
        ]
    else:
        observation["actions"].insert(closing + 1, {"action": other_reply})

    result = score(fixture, cases["C18"], observation)
    assert result["hard_pass"] is False
    assert result["failures"] == ["final reply verification must precede successful completion"]


@pytest.mark.parametrize("boundary", ["reply.handoff", "job.result"])
def test_noncanonical_final_reply_boundaries_remain_subject_to_manual_review(oracle, boundary):
    fixture, cases, score = oracle
    observation = scripted_handoff()
    next(item for item in observation["actions"] if item["action"] == boundary)["action"] = "alternative.label"

    result = score(fixture, cases["C18"], observation)
    assert result["hard_pass"] is True
    assert "review final reply verification order with noncanonical labels" in result["manual_review_required"]


@pytest.mark.parametrize(
    ("case_id", "reply_text"),
    [
        pytest.param("C16", "Срок уточню. Пока ничего не оформляю.", id="price_interest"),
        pytest.param("C17", "Хорошо, заказ не оформляю.", id="refusal"),
    ],
)
def test_generic_crm_write_cannot_pass_a_ban_on_create_and_update(oracle, case_id, reply_text):
    fixture, cases, score = oracle
    observation = {
        "case_id": case_id,
        "client_replies": [{"kind": "result", "text": reply_text}],
        "actions": [
            {"action": action}
            for action in [
                "role.resolve",
                "instruction.client.load",
                "context.read",
                "reply.result",
                "reply.verify",
                "job.result",
            ]
        ],
        "outcome": "answered",
    }
    assert score(fixture, cases[case_id], observation)["hard_pass"] is True

    observation["actions"].insert(3, {"action": "crm.write", "data": "Create a synthetic purchase card"})
    result = score(fixture, cases[case_id], observation)
    assert result["hard_pass"] is False
    assert result["failures"] == ["forbidden planned action: crm.write"]


@pytest.mark.parametrize("action", ["crm.write", "crm.update", "crm.create"])
def test_create_only_ban_allows_a_consented_update(oracle, action):
    fixture, cases, score = oracle
    observation = scripted_handoff("C19")
    observation["claims"]["crm_card_id"] = "FIXTURE_CARD_A"
    changes = next(entry for entry in observation["actions"] if entry["action"] == "crm.create")
    changes.update(action=action, data="Apply the agreed changes to FIXTURE_CARD_A")

    result = score(fixture, cases["C19"], observation)
    assert result["hard_pass"] is (action != "crm.create")
    assert result["failures"] == (["forbidden planned action: crm.create"] if action == "crm.create" else [])


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


@pytest.mark.parametrize(("case_id", "booking_time"), [("C22", "12:00"), ("C23", "18:00")])
@pytest.mark.parametrize(
    ("receipt_facts", "expected_failure"),
    [
        pytest.param({}, None, id="verified"),
        pytest.param(
            {"crm_independent_reread": "mismatch"},
            "booking confirmed without matching CRM reread",
            id="crm_mismatch",
        ),
        pytest.param(
            {"owner_notification_delivery": "unknown"},
            "booking confirmed despite unverified owner notification",
            id="owner_unknown",
        ),
    ],
)
def test_booking_confirmation_checks_independent_receipts_at_both_boundaries(
    oracle, case_id, booking_time, receipt_facts, expected_failure
):
    fixture, cases, score = oracle
    case = deepcopy(cases[case_id])
    case["inputs"]["facts"].update(receipt_facts)
    observation = scripted_handoff(case_id)
    observation["client_replies"] = [{"kind": "result", "text": f"Записал на приём 14 октября в {booking_time}."}]
    next(item for item in observation["actions"] if item["action"] == "reply.handoff")["action"] = "reply.result"
    observation["claims"] = {
        "booking_confirmed": True,
        "booking_date": "2026-10-14",
        "booking_time": booking_time,
        "timezone": "Asia/Krasnoyarsk",
    }

    result = score(fixture, case, observation)
    assert result["hard_pass"] is (expected_failure is None)
    assert result["failures"] == ([] if expected_failure is None else [expected_failure])


@pytest.mark.parametrize(
    ("receipt_facts", "missing_action", "expected_failure"),
    [
        pytest.param({}, None, None, id="verified"),
        pytest.param(
            {"crm_independent_reread": "mismatch"},
            None,
            "fixture cannot support successful card handoff",
            id="crm_mismatch",
        ),
        pytest.param(
            {"owner_notification_delivery": "unknown"},
            None,
            "handoff confirmed despite unverified owner notification",
            id="owner_unknown",
        ),
        pytest.param(
            {},
            "crm.reread",
            "handoff_confirmed lacks planned independent card/delivery verification",
            id="missing_crm_readback",
        ),
        pytest.param(
            {},
            "owner.notification_readback",
            "handoff_confirmed lacks planned independent card/delivery verification",
            id="missing_owner_readback",
        ),
    ],
)
def test_payment_claim_handoff_requires_both_receipts_without_confirmation_flag(
    oracle, receipt_facts, missing_action, expected_failure
):
    fixture, cases, score = oracle
    case = deepcopy(cases["C29"])
    case["inputs"]["facts"].update(receipt_facts)
    observation = scripted_handoff("C29")
    observation["claims"] = {"payment_confirmed": False}
    observation["client_replies"] = [
        {"kind": "result", "text": "Передал предварительную заявку коллегам. Перевод ещё сверяем."}
    ]
    next(item for item in observation["actions"] if item["action"] == "reply.handoff")["action"] = "reply.result"
    observation["actions"] = [item for item in observation["actions"] if item["action"] != missing_action]

    result = score(fixture, case, observation)
    assert result["hard_pass"] is (expected_failure is None)
    assert result["failures"] == ([] if expected_failure is None else [expected_failure])


@pytest.mark.parametrize("outcome", ["answered", "awaiting_client"])
def test_payment_reconciliation_without_claimed_handoff_does_not_require_receipts(oracle, outcome):
    fixture, cases, score = oracle
    case = deepcopy(cases["C29"])
    case["inputs"]["facts"].update(crm_independent_reread="mismatch", owner_notification_delivery="unknown")
    observation = {
        "case_id": "C29",
        "client_replies": [{"kind": "result", "text": "Перевод ещё сверяем, заказ пока не подтверждён."}],
        "actions": [
            {"action": action}
            for action in [*fixture["global_required_actions"], "reply.result", "reply.verify", "job.result"]
        ],
        "outcome": outcome,
        "claims": {"payment_confirmed": False},
    }

    result = score(fixture, case, observation)
    assert result["hard_pass"] is True
    assert result["failures"] == []


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


@pytest.mark.parametrize("reply_kind", ["ack", "result", "clarification", "handoff"])
def test_unknown_ack_rejects_client_reply_missing_from_action_trace(oracle, reply_kind):
    fixture, cases, score = oracle
    observation = unknown_ack_plan()
    observation["client_replies"] = [{"kind": reply_kind, "text": "Синтетический ответ клиенту."}]
    result = score(fixture, cases["C35"], observation)
    assert result["hard_pass"] is False


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


def new_question_plan():
    return {
        "case_id": "C45",
        "client_replies": [{"kind": "result", "text": "Да, пришлите фото, на котором читается маркировка детали."}],
        "actions": [
            {"action": "role.resolve"},
            {"action": "instruction.client.load"},
            {"action": "context.read"},
            {"action": "reply.result"},
            {"action": "reply.verify", "data": "Синтетическая сверка нового ответа на C45-event-2."},
            {"action": "job.result", "data": "Покрыто новое событие C45-event-2."},
        ],
        "outcome": "answered",
        "claims": {},
    }


def test_new_question_after_full_human_answer_has_its_own_event(rehearsal, oracle):
    _, cases, _ = oracle
    inputs = cases["C45"]["inputs"]
    human = next(message for message in inputs["history"] if message.get("origin") == "human")
    assert human["verified"] is True
    assert human["covered_event_ids"] == ["C45-event-1"]
    assert [event["id"] for event in inputs["events"]] == ["C45-event-2"]
    assert inputs["facts"]["current_request_already_answered"] is False

    selected = bind_scripted_case(rehearsal, oracle, "C45", new_question_plan())
    code, result = invoke(selected)
    assert code == 0 and result["expert_accepted"] is True
    assert result["runtime_or_delivery_proof"] is False


def test_old_human_answer_cannot_close_new_question_without_reply(oracle):
    fixture, cases, score = oracle
    observation = new_question_plan()
    observation["client_replies"] = []
    observation["outcome"] = "no_reply_needed"
    observation["actions"] = [item for item in observation["actions"] if not item["action"].startswith("reply.")]
    result = score(fixture, cases["C45"], observation)
    assert result["hard_pass"] is False
    assert "unexpected outcome: no_reply_needed" in result["failures"]
    assert "missing required planned action: reply.verify" in result["failures"]


@pytest.mark.parametrize("action", ["crm.search", "crm.write", "reply.ack", "reply.readback", "reply.new_key_retry"])
def test_new_question_avoids_crm_ack_and_retries(oracle, action):
    fixture, cases, score = oracle
    observation = new_question_plan()
    observation["actions"].append({"action": action})
    result = score(fixture, cases["C45"], observation)
    assert result["hard_pass"] is False and f"forbidden planned action: {action}" in result["failures"]


@pytest.mark.parametrize("fault", ["old_receipt", "bundled_answer"])
def test_review_rejects_old_receipt_or_repeated_answer_in_new_question(rehearsal, oracle, fault):
    fixture, cases, score = oracle
    observation = new_question_plan()
    if fault == "old_receipt":
        verification = next(item for item in observation["actions"] if item["action"] == "reply.verify")
        verification["data"] = "Старая ручная отправка 801 для C45-event-1."
        assertion = "new_reply_verified_for_current_event"
    else:
        observation["client_replies"][0]["text"] += " Прежний комплект стоит 1000 ₽."
        assertion = "answer_only_new_question"

    result = score(fixture, cases["C45"], observation)
    assert result["hard_pass"] is True
    assert f"expert semantic assertion: {assertion}" in result["manual_review_required"]
    selected = bind_scripted_case(rehearsal, oracle, "C45", observation)
    selected[3]["cases"][0]["hard_assertions"][assertion] = False
    code, acceptance = invoke(selected)
    assert code == 1 and acceptance["expert_accepted"] is False
