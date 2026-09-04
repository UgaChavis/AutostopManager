"""Privacy-safe Store quote dialogue primitives for the work Telegram account.

The module deliberately has no Telegram transport dependency.  It prepares
short, human-facing text and produces durable references that contain only a
Store quote identifier, published estimate revision, opaque context hash and
content hashes.  The caller must keep the returned message text and incoming
reply text transient and persist only :meth:`durable_ref` output.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


MAX_CLIENT_REPLY_CHARS: Final = 4096
MAX_OPTION_LABEL_CHARS: Final = 160
_QUOTE_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
_SPACE_PATTERN: Final = re.compile(r"\s+")
_SENTENCE_END_PATTERN: Final = re.compile(r"[.!?]+")
_UNSAFE_LABEL_PATTERN: Final = re.compile(r"[\r\n\x00]|(?:https?://|tg://)", re.IGNORECASE)
_DECLINE_PATTERN: Final = re.compile(
    r"\b(?:не\s+(?:надо|нужен|нужна|нужно|буду|беру)|отмен(?:а|яем|яй(?:те)?|ить)|отказ(?:ываюсь|ался|алась)?)\b",
    re.IGNORECASE,
)
_ADDITION_PATTERN: Final = re.compile(
    r"\b(?:ещ[её]\s+(?:нуж(?:ен|на|но)|добав)|добав(?:ь|ьте|ить)|заодно|плюс)\b",
    re.IGNORECASE,
)
_QUESTION_START_PATTERN: Final = re.compile(
    r"^(?:а\s+)?(?:какой|какая|какие|когда|сколько|почему|зачем|где|как|можно|точно|подойд[её]т|"
    r"есть\s+ли|будет\s+ли|а\s+если)\b",
    re.IGNORECASE,
)
_CONSENT_PATTERN: Final = re.compile(
    r"^(?:да[,.! ]+)?(?:бер(?:у|ем)|оформ(?:ляем|ляй|ляйте)|заказ(?:ываем|ывай|ывайте)|"
    r"подтверждаю|соглас(?:ен|на))(?:\s+(?:этот|его|её|вариант))?[.! ]*$",
    re.IGNORECASE,
)
_ORDINAL_SELECTION_PATTERN: Final = re.compile(
    r"\b(?:перв(?:ый|ая|ое)|втор(?:ой|ая|ое)|трет(?:ий|ья|ье)|оригинал|аналог|вариант)\b",
    re.IGNORECASE,
)


class StoreTelegramAdapterError(ValueError):
    """A validation error that is safe to retain as an error code only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OutboundMessageKind(StrEnum):
    IDENTITY_PROMPT = "identity_prompt"
    CLARIFICATION = "clarification"
    OFFER = "offer"
    SELECTION_CONFIRMATION = "selection_confirmation"
    ADDITION_CLARIFICATION = "addition_clarification"
    PAYMENT_INSTRUCTION = "payment_instruction"


class ClarificationTopic(StrEnum):
    VEHICLE = "vehicle"
    PART = "part"
    PRIORITY = "priority"


class RecommendationReason(StrEnum):
    QUALITY_AND_DELIVERY = "по качеству и сроку"
    QUALITY = "по качеству"
    DELIVERY = "по сроку"
    VALUE = "по цене"


class ClientReplyCategory(StrEnum):
    CLARIFICATION = "clarification"
    ADDITION = "addition"
    SELECTION = "selection"
    CONSENT = "consent"
    DECLINE = "decline"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class StoreTelegramContext:
    """Opaque binding for one published Store estimate and its Telegram turn."""

    quote_id: str
    estimate_revision: int
    context_hash: str

    def __post_init__(self) -> None:
        quote_id = str(self.quote_id or "").strip()
        if not _QUOTE_ID_PATTERN.fullmatch(quote_id):
            raise StoreTelegramAdapterError("quote_id_invalid")
        if isinstance(self.estimate_revision, bool) or not isinstance(self.estimate_revision, int):
            raise StoreTelegramAdapterError("estimate_revision_invalid")
        if not 0 <= self.estimate_revision <= 2**63 - 1:
            raise StoreTelegramAdapterError("estimate_revision_invalid")
        context_hash = str(self.context_hash or "").strip().casefold()
        if not _SHA256_PATTERN.fullmatch(context_hash):
            raise StoreTelegramAdapterError("context_hash_invalid")
        object.__setattr__(self, "quote_id", quote_id)
        object.__setattr__(self, "context_hash", context_hash)

    def durable_ref(self) -> dict[str, str | int]:
        """Return the only context representation suitable for a workflow ledger."""

        return {
            "schema": "StoreTelegramContextRefV1",
            "quote_id": self.quote_id,
            "estimate_revision": self.estimate_revision,
            "context_hash": self.context_hash,
        }


@dataclass(frozen=True)
class StoreTelegramMessage:
    """A transient outbound message; persist ``durable_ref()``, never this object."""

    context: StoreTelegramContext
    kind: OutboundMessageKind
    text: str = field(repr=False)
    text_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.context, StoreTelegramContext):
            raise StoreTelegramAdapterError("context_invalid")
        if not isinstance(self.kind, OutboundMessageKind):
            raise StoreTelegramAdapterError("outbound_kind_invalid")
        if not isinstance(self.text, str):
            raise StoreTelegramAdapterError("outbound_text_invalid")
        _validate_casual_message_style(self.text)
        object.__setattr__(self, "text_sha256", _sha256(self.text))

    def durable_ref(self) -> dict[str, str | int]:
        """Return safe metadata without message text, peer IDs or contacts."""

        return {
            **self.context.durable_ref(),
            "message_kind": self.kind.value,
            "text_sha256": self.text_sha256,
        }


@dataclass(frozen=True)
class ClientReplyClassification:
    """A reply result containing no raw Telegram content or peer identifier."""

    context: StoreTelegramContext
    category: ClientReplyCategory
    text_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.context, StoreTelegramContext):
            raise StoreTelegramAdapterError("context_invalid")
        if not isinstance(self.category, ClientReplyCategory):
            raise StoreTelegramAdapterError("reply_category_invalid")
        text_sha256 = str(self.text_sha256 or "").strip().casefold()
        if not _SHA256_PATTERN.fullmatch(text_sha256):
            raise StoreTelegramAdapterError("reply_hash_invalid")
        object.__setattr__(self, "text_sha256", text_sha256)

    def durable_ref(self) -> dict[str, str | int]:
        """Return a hash-only reply record suitable for persistent workflow state."""

        return {
            **self.context.durable_ref(),
            "reply_category": self.category.value,
            "reply_text_sha256": self.text_sha256,
        }


def build_identity_prompt(context: StoreTelegramContext) -> StoreTelegramMessage:
    """Ask for identity confirmation without disclosing request information."""

    return _message(
        context,
        OutboundMessageKind.IDENTITY_PROMPT,
        "Привет! Вы оставляли заявку на запчасти в AutoStop?",
    )


def build_clarification_message(
    context: StoreTelegramContext,
    topic: ClarificationTopic,
) -> StoreTelegramMessage:
    """Build one neutral, single-question clarification prompt."""

    prompts = {
        ClarificationTopic.VEHICLE: "Подскажи, пожалуйста, год и мотор машины?",
        ClarificationTopic.PART: "Подскажи, пожалуйста, что именно нужно поменять?",
        ClarificationTopic.PRIORITY: "Тебе важнее побыстрее или подешевле?",
    }
    if not isinstance(topic, ClarificationTopic):
        raise StoreTelegramAdapterError("clarification_topic_invalid")
    return _message(context, OutboundMessageKind.CLARIFICATION, prompts[topic])


def build_offer_message(
    context: StoreTelegramContext,
    *,
    option_label: str,
    reason: RecommendationReason = RecommendationReason.QUALITY_AND_DELIVERY,
) -> StoreTelegramMessage:
    """Build a concise recommendation; the option label stays transient."""

    label = _clean_option_label(option_label)
    if not isinstance(reason, RecommendationReason):
        raise StoreTelegramAdapterError("recommendation_reason_invalid")
    return _message(
        context,
        OutboundMessageKind.OFFER,
        f"Глянул: {label} — нормальный вариант {reason.value}. Его оформляем?",
    )


def build_selection_confirmation_message(
    context: StoreTelegramContext,
    *,
    option_label: str,
) -> StoreTelegramMessage:
    """Confirm a chosen option before treating a reply as order consent."""

    label = _clean_option_label(option_label)
    return _message(
        context,
        OutboundMessageKind.SELECTION_CONFIRMATION,
        f"Понял, {label} берём. Оформляем?",
    )


def build_addition_clarification_message(context: StoreTelegramContext) -> StoreTelegramMessage:
    """Ask for enough data to process a client-added position safely."""

    return _message(
        context,
        OutboundMessageKind.ADDITION_CLARIFICATION,
        "Понял, добавлю и это. Подскажи, пожалуйста, артикул или фото?",
    )


def build_payment_instruction(context: StoreTelegramContext) -> StoreTelegramMessage:
    """State approved payment paths without requisites or any paid claim."""

    return _message(
        context,
        OutboundMessageKind.PAYMENT_INSTRUCTION,
        "Заказ оформлен и ждёт оплаты. Оплатить можно на ресепшене или по инструкции сотрудника. Как удобнее?",
    )


def classify_client_reply(
    context: StoreTelegramContext,
    reply_text: str,
    *,
    offered_option_labels: Iterable[str] = (),
) -> ClientReplyClassification:
    """Classify one transient incoming reply without retaining its raw content.

    ``offered_option_labels`` is transient caller input from the exact active
    quote.  It improves selection recognition but is not copied into the
    returned classification or durable reference.
    """

    normalized = _normalise_reply(reply_text)
    offered_labels = tuple(_normalise_option_label(label) for label in offered_option_labels)
    category = _classify_normalized_reply(normalized, offered_labels)
    return ClientReplyClassification(
        context=context,
        category=category,
        text_sha256=_sha256(reply_text),
    )


def validate_incoming_reply(
    expected_context: StoreTelegramContext,
    reply: ClientReplyClassification,
) -> ClientReplyClassification:
    """Reject a reply that was classified for another quote, revision or turn."""

    if not isinstance(expected_context, StoreTelegramContext):
        raise StoreTelegramAdapterError("context_invalid")
    if not isinstance(reply, ClientReplyClassification):
        raise StoreTelegramAdapterError("reply_invalid")
    received = reply.context
    if received.quote_id != expected_context.quote_id:
        raise StoreTelegramAdapterError("incoming_quote_mismatch")
    if received.estimate_revision != expected_context.estimate_revision:
        raise StoreTelegramAdapterError("incoming_revision_stale")
    if received.context_hash != expected_context.context_hash:
        raise StoreTelegramAdapterError("incoming_context_stale")
    return reply


def is_explicit_order_consent(reply: ClientReplyClassification) -> bool:
    """Return true only for a conservatively classified explicit consent."""

    if not isinstance(reply, ClientReplyClassification):
        raise StoreTelegramAdapterError("reply_invalid")
    return reply.category is ClientReplyCategory.CONSENT


def _message(
    context: StoreTelegramContext,
    kind: OutboundMessageKind,
    text: str,
) -> StoreTelegramMessage:
    return StoreTelegramMessage(context=context, kind=kind, text=text)


def _clean_option_label(value: str) -> str:
    if not isinstance(value, str):
        raise StoreTelegramAdapterError("option_label_invalid")
    if _UNSAFE_LABEL_PATTERN.search(value):
        raise StoreTelegramAdapterError("option_label_invalid")
    label = _SPACE_PATTERN.sub(" ", value).strip()
    if not label or len(label) > MAX_OPTION_LABEL_CHARS:
        raise StoreTelegramAdapterError("option_label_invalid")
    return label


def _normalise_option_label(value: str) -> str:
    return _clean_option_label(value).casefold()


def _normalise_reply(value: str) -> str:
    if not isinstance(value, str):
        raise StoreTelegramAdapterError("reply_text_invalid")
    if not value or len(value) > MAX_CLIENT_REPLY_CHARS or "\x00" in value:
        raise StoreTelegramAdapterError("reply_text_invalid")
    normalized = _SPACE_PATTERN.sub(" ", value).strip().casefold()
    if not normalized:
        raise StoreTelegramAdapterError("reply_text_invalid")
    return normalized


def _classify_normalized_reply(normalized: str, offered_labels: tuple[str, ...]) -> ClientReplyCategory:
    # Prioritise outcomes that must stop automatic ordering over a possible yes.
    if _DECLINE_PATTERN.search(normalized):
        return ClientReplyCategory.DECLINE
    if _ADDITION_PATTERN.search(normalized):
        return ClientReplyCategory.ADDITION
    if "?" in normalized or _QUESTION_START_PATTERN.search(normalized):
        return ClientReplyCategory.CLARIFICATION
    if _CONSENT_PATTERN.fullmatch(normalized):
        return ClientReplyCategory.CONSENT
    if _ORDINAL_SELECTION_PATTERN.search(normalized) or any(label in normalized for label in offered_labels):
        return ClientReplyCategory.SELECTION
    return ClientReplyCategory.AMBIGUOUS


def _validate_casual_message_style(text: str) -> None:
    if not isinstance(text, str) or not text or len(text) > MAX_CLIENT_REPLY_CHARS:
        raise StoreTelegramAdapterError("outbound_text_invalid")
    if "\n" in text or "\r" in text or "\x00" in text:
        raise StoreTelegramAdapterError("outbound_text_invalid")
    sentence_count = len(_SENTENCE_END_PATTERN.findall(text))
    question_count = text.count("?")
    if not 1 <= sentence_count <= 3 or question_count != 1 or not text.endswith("?"):
        raise StoreTelegramAdapterError("outbound_style_invalid")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
