from __future__ import annotations

import json

import pytest

from autostop_manager.store_telegram_adapter import (
    ClientReplyCategory,
    ClarificationTopic,
    RecommendationReason,
    StoreTelegramAdapterError,
    StoreTelegramContext,
    build_addition_clarification_message,
    build_clarification_message,
    build_identity_prompt,
    build_offer_message,
    build_payment_instruction,
    build_selection_confirmation_message,
    classify_client_reply,
    is_explicit_order_consent,
    validate_incoming_reply,
)


def _context(*, quote_id: str = "quote-65", revision: int = 4, context_hash: str = "a" * 64) -> StoreTelegramContext:
    return StoreTelegramContext(
        quote_id=quote_id,
        estimate_revision=revision,
        context_hash=context_hash,
    )


@pytest.mark.parametrize(
    ("message", "expected_kind"),
    [
        (lambda context: build_identity_prompt(context), "identity_prompt"),
        (lambda context: build_clarification_message(context, ClarificationTopic.VEHICLE), "clarification"),
        (
            lambda context: build_offer_message(
                context,
                option_label="Bosch",
                reason=RecommendationReason.QUALITY_AND_DELIVERY,
            ),
            "offer",
        ),
        (lambda context: build_selection_confirmation_message(context, option_label="Bosch"), "selection_confirmation"),
        (lambda context: build_addition_clarification_message(context), "addition_clarification"),
        (lambda context: build_payment_instruction(context), "payment_instruction"),
    ],
)
def test_outbound_messages_are_short_casual_and_have_one_next_question(message, expected_kind: str) -> None:
    outbound = message(_context())

    sentence_count = sum(outbound.text.count(mark) for mark in ".!?")
    assert 1 <= sentence_count <= 3
    assert outbound.text.count("?") == 1
    assert outbound.text.endswith("?")
    assert outbound.durable_ref()["message_kind"] == expected_kind


def test_identity_prompt_does_not_disclose_the_quote() -> None:
    outbound = build_identity_prompt(_context())

    assert "заявк" in outbound.text.casefold()
    assert all(value not in outbound.text for value in ("VIN", "Bosch", "цена", "руб"))


def test_durable_message_reference_never_contains_text_or_peer_data() -> None:
    outbound = build_offer_message(_context(), option_label="Bosch 0 986 479 000")
    durable = outbound.durable_ref()

    assert "text" not in durable
    assert "peer" not in durable
    assert "phone" not in durable
    assert "Bosch" not in json.dumps(durable, ensure_ascii=False)
    assert outbound.text not in repr(outbound)


def test_payment_instruction_has_no_requisites_and_never_claims_payment() -> None:
    outbound = build_payment_instruction(_context())
    text = outbound.text.casefold()

    assert "ресепшен" in text
    assert "инструкции сотрудника" in text
    assert "оплачен" not in text
    assert "карт" not in text
    assert "реквизит" not in text
    assert "\n" not in outbound.text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("А по сроку сколько?", ClientReplyCategory.CLARIFICATION),
        ("И ещё нужен фильтр", ClientReplyCategory.ADDITION),
        ("Bosch", ClientReplyCategory.SELECTION),
        ("Оформляем", ClientReplyCategory.CONSENT),
        ("Не надо, спасибо", ClientReplyCategory.DECLINE),
        ("Да", ClientReplyCategory.AMBIGUOUS),
    ],
)
def test_reply_classifier_is_conservative_and_keeps_only_hashes(text: str, expected: ClientReplyCategory) -> None:
    reply = classify_client_reply(_context(), text, offered_option_labels=("Bosch", "оригинал"))
    durable = reply.durable_ref()

    assert reply.category is expected
    assert "text" not in durable
    assert "peer" not in durable
    assert text not in json.dumps(durable, ensure_ascii=False)
    assert text not in repr(reply)


def test_question_form_of_apparent_consent_is_not_orderable() -> None:
    reply = classify_client_reply(_context(), "Оформляем?")

    assert reply.category is ClientReplyCategory.CLARIFICATION
    assert not is_explicit_order_consent(reply)


def test_explicit_consent_is_orderable_only_after_context_validation() -> None:
    context = _context()
    reply = classify_client_reply(context, "Да, оформляем")

    validated = validate_incoming_reply(context, reply)

    assert is_explicit_order_consent(validated)


@pytest.mark.parametrize(
    ("expected", "received", "error"),
    [
        (_context(), _context(quote_id="quote-66"), "incoming_quote_mismatch"),
        (_context(), _context(revision=5), "incoming_revision_stale"),
        (_context(), _context(context_hash="b" * 64), "incoming_context_stale"),
    ],
)
def test_validate_incoming_reply_rejects_stale_or_wrong_context(
    expected: StoreTelegramContext,
    received: StoreTelegramContext,
    error: str,
) -> None:
    reply = classify_client_reply(received, "Оформляем")

    with pytest.raises(StoreTelegramAdapterError, match=error):
        validate_incoming_reply(expected, reply)


def test_context_and_outbound_inputs_reject_values_that_could_break_binding_or_style() -> None:
    with pytest.raises(StoreTelegramAdapterError, match="context_hash_invalid"):
        StoreTelegramContext(quote_id="quote-65", estimate_revision=4, context_hash="not-a-hash")
    with pytest.raises(StoreTelegramAdapterError, match="option_label_invalid"):
        build_offer_message(_context(), option_label="Bosch\nОформляем?")
