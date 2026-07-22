#!/usr/bin/env python3
"""Privacy-preserving Codex lifecycle hook for AutoStopManager learning mode.

The script is deliberately dependency-light because Codex invokes it as a
short-lived process.  It receives a hook JSON object on stdin and emits only
the Codex hook JSON response on stdout.  Prompts, tool inputs, tool responses,
and assistant messages are never logged or persisted here: only opaque hashes
and allowlisted technical metadata reach ``ManagerMemoryStore``.

The durable storage methods are provided by the learning foundation:
``resolve_agent_mode``, ``start_agent_turn``, ``record_agent_tool_event``,
``get_agent_learning_turn_by_external_id_fast``,
``get_agent_turn_by_external_id``, ``has_completed_experience_review_by_external_id``,
and ``post_run_review``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Protocol, cast


PROJECT_ROOT = Path(os.environ.get("AUTOSTOP_MANAGER_ROOT", Path(__file__).resolve().parents[1])).resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_WORK_MODE_RE = re.compile(
    r"(?:\b(?:не|без)\s+(?:в\s+)?(?:режим(?:е|а)?\s+)?(?:само)?обучени[яе]\b|"
    r"\bне\s+(?:включ(?:и|ай|ать|ить)|запускай|используй|переключай)(?:\s+(?:в|на))?\s+(?:режим(?:е|а)?\s+)?(?:само)?обучени[яе]\b|"
    r"\b(?:отключ(?:и|ить)|выключ(?:и|ить))\s+(?:режим(?:е|а)?\s+)?(?:само)?обучени[яе]\b|"
    r"\b(?:рабоч(?:ий|ем|его)?\s+(?:режим(?:е|а)?|mode)|режим(?:е|а)?\s+работы|work\s+mode)\b)",
    re.IGNORECASE,
)
_LEARNING_MODE_RE = re.compile(
    r"\b(?:(?:режим(?:е|а)?|mode)\s+(?:само)?обучени[яе]|(?:само)?обучени[яе]\s+режим(?:е|а)?|learning\s+mode)\b",
    re.IGNORECASE,
)
_PERSISTENT_LEARNING_COMMAND_RE = re.compile(
    r"^(?:(?:пожалуйста|давай)\s+)?(?:включ(?:и|ить)|перевед(?:и|ти)|переключ(?:и|ить)|"
    r"перейд(?:и|ти)|установ(?:и|ить)|активир(?:уй|овать))\s+(?:в\s+|на\s+)?"
    r"(?:режим\s+)?(?:само)?обучени[яе]$",
    re.IGNORECASE,
)
_PERSISTENT_WORK_COMMAND_RE = re.compile(
    r"^(?:(?:пожалуйста|давай)\s+)?(?:(?:выключ(?:и|ить)|отключ(?:и|ить))\s+"
    r"(?:режим\s+)?(?:само)?обучени[яе]|(?:перевед(?:и|ти)|переключ(?:и|ить)|"
    r"перейд(?:и|ти)|установ(?:и|ить)|верн(?:и|уться))\s+(?:в\s+|на\s+)?"
    r"(?:(?:обычн(?:ый|ом)|рабоч(?:ий|ем))\s+режим|work\s+mode))$",
    re.IGNORECASE,
)
_MODES = frozenset({"work", "learning"})


class LearningStore(Protocol):
    """The narrow, safe storage contract used by hook processes."""

    def resolve_agent_mode(self, mode_override: str | None = None) -> Any: ...

    def set_agent_mode(self, mode: str, *, expected_state_version: int | None = None) -> Any: ...

    def start_agent_turn(
        self,
        task_signature: str,
        mode_override: str | None = None,
        workflow_id: str = "",
        source: str = "codex",
        metadata: dict[str, Any] | None = None,
        external_turn_id: str = "",
    ) -> dict[str, Any]: ...

    def record_agent_tool_event(
        self,
        turn_id: str,
        *,
        tool_name: str,
        status: str,
        duration_ms: int | None = None,
        error_code: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def get_agent_turn_by_external_id(self, external_turn_id: str) -> dict[str, Any]: ...

    def get_agent_learning_turn_by_external_id_fast(self, external_turn_id: str) -> dict[str, Any]: ...

    def get_agent_turn(self, turn_id: str) -> dict[str, Any]: ...

    def has_completed_experience_review_by_external_id(self, external_turn_id: str) -> dict[str, Any] | bool: ...

    def has_completed_experience_review(self, turn_id: str) -> dict[str, Any] | bool: ...

    def post_run_review(
        self,
        turn_id: str,
        outcome: str = "confirmed",
        completion_checks: list[str] | None = None,
        tool_assessment: list[dict[str, Any]] | None = None,
        failure_class: str = "",
        improvement_kind: str = "",
        risk: str = "low",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def agent_learning_workflow(self, operation: str, **kwargs: Any) -> dict[str, Any]: ...


def _safe_opaque_id(value: Any) -> str:
    """Return one Codex-owned opaque identifier, never an arbitrary payload."""

    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    return candidate if _OPAQUE_ID_RE.fullmatch(candidate) else ""


def _hash(value: str, *, purpose: str) -> str:
    """Return an opaque scoped hash; the original value is never retained."""

    material = f"autostop-manager-learning-hook-v1\x00{purpose}\x00{value}".encode()
    return hashlib.sha256(material).hexdigest()


def _mode_from_value(value: Any) -> str:
    if isinstance(value, str):
        candidate = value
    elif isinstance(value, dict):
        candidate = value.get("effective_mode") or value.get("mode") or ""
        if not candidate and isinstance(value.get("item"), dict):
            candidate = value["item"].get("effective_mode") or value["item"].get("mode") or ""
    else:
        candidate = ""
    return candidate if candidate in _MODES else "work"


def explicit_mode_override(prompt: Any) -> str | None:
    """Recognize only explicit mode wording without retaining the prompt."""

    if not isinstance(prompt, str):
        return None
    normalized = " ".join(prompt.casefold().split())
    if _WORK_MODE_RE.search(normalized):
        return "work"
    if _LEARNING_MODE_RE.search(normalized):
        return "learning"
    return None


def persistent_mode_command(prompt: Any) -> str | None:
    """Accept only a whole owner command that changes the durable mode.

    Voice transcription frequently adds punctuation, so normalize it while
    deliberately rejecting a mode phrase embedded in an ordinary task.
    """

    if not isinstance(prompt, str):
        return None
    normalized = " ".join(prompt.casefold().split()).strip(" .,!?:;")
    if _PERSISTENT_WORK_COMMAND_RE.fullmatch(normalized):
        return "work"
    if _PERSISTENT_LEARNING_COMMAND_RE.fullmatch(normalized):
        return "learning"
    return None


def _load_store() -> LearningStore | None:
    """Load the Manager's safe storage facade, or fail open if unavailable."""

    try:
        from autostop_manager.storage import ManagerMemoryStore

        # The hook intentionally depends on the small protocol above so it can
        # keep working across a rolling Manager deployment.
        return cast(LearningStore, ManagerMemoryStore())
    except Exception:  # noqa: BLE001 - unavailable storage must never block the owner task.
        return None


def _turn_for_payload(store: LearningStore, payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    external_turn_id = _safe_opaque_id(payload.get("turn_id"))
    if not external_turn_id:
        return "", None
    try:
        turn = store.get_agent_turn_by_external_id(external_turn_id)
    except AttributeError:
        # Foundation guarantees the alias, but retain compatibility with its
        # resolver-capable base method during a rolling deploy.
        try:
            turn = store.get_agent_turn(external_turn_id)
        except Exception:  # noqa: BLE001 - a storage read must fail open in a lifecycle hook.
            return external_turn_id, None
    except Exception:  # noqa: BLE001 - a storage read must fail open in a lifecycle hook.
        return external_turn_id, None
    return external_turn_id, turn if isinstance(turn, dict) else None


def _learning_turn(store: LearningStore, payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    external_turn_id, turn = _turn_for_payload(store, payload)
    if _mode_from_value(turn) != "learning":
        return external_turn_id, None
    return external_turn_id, turn


def _fast_learning_turn_for_tool_event(
    store: LearningStore, payload: dict[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    """Read a compact learning turn without schema setup before Pre/Post work.

    Work-mode turns have no learning row. They therefore return after one
    read-only lookup with neither the legacy reader nor telemetry writes.
    """

    external_turn_id = _safe_opaque_id(payload.get("turn_id"))
    if not external_turn_id:
        return "", None
    try:
        result = store.get_agent_learning_turn_by_external_id_fast(external_turn_id)
    except AttributeError:
        # Compatibility fallback for a briefly mixed hook/Manager rollout.
        return _learning_turn(store, payload)
    except Exception:  # noqa: BLE001 - a fast telemetry lookup must fail open.
        return external_turn_id, None
    if not isinstance(result, dict) or result.get("ok") is not True:
        return external_turn_id, None
    item = result.get("item")
    if not isinstance(item, dict) or _mode_from_value(item) != "learning":
        return external_turn_id, None
    return external_turn_id, item


def _safe_tool_name(value: Any) -> str:
    return _safe_opaque_id(value)


def _tool_response_summary(response: Any) -> tuple[str, str, str]:
    """Classify a result using only shape and generic outcome signals.

    This intentionally never returns content, exception text, command output,
    or any field value from the tool response.
    """

    if response is None:
        return "succeeded", "", "null"
    if isinstance(response, dict):
        # Only test top-level key presence and primitive success markers.  Do
        # not recurse into or copy potentially sensitive payloads.
        if response.get("isError") is True or response.get("is_error") is True:
            return "failed", "tool_reported_error", "object"
        if response.get("ok") is False or response.get("success") is False:
            return "failed", "tool_reported_failure", "object"
        exit_code = response.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
            return "failed", "nonzero_exit", "object"
        if response.get("error") is not None and "error" in response:
            return "failed", "tool_error_field", "object"
        return "succeeded", "", "object"
    if isinstance(response, list):
        return "succeeded", "", "array"
    if isinstance(response, str):
        return "succeeded", "", "string"
    if isinstance(response, bool):
        return "succeeded", "", "boolean"
    if isinstance(response, (int, float)):
        return "succeeded", "", "number"
    return "succeeded", "", "other"


def _tool_metadata(payload: dict[str, Any], *, phase: str, response_shape: str | None = None) -> dict[str, str]:
    tool_use_id = _safe_opaque_id(payload.get("tool_use_id"))
    metadata = {"phase": phase}
    if tool_use_id:
        metadata["tool_use_id_hash"] = _hash(tool_use_id, purpose="tool-use")
    if response_shape is not None:
        metadata["response_shape"] = response_shape
    return metadata


def _mode_context(
    mode: str,
    *,
    external_turn_id: str = "",
    persistent_change: bool = False,
) -> dict[str, Any]:
    if mode == "learning":
        turn_reference = f" Learning turn reference: {external_turn_id}." if external_turn_id else ""
        mode_change = " Global mode was set to learning by an explicit owner command." if persistent_change else ""
        text = (
            "AutoStopManager effective mode: learning. Finish the requested work, then call "
            "post_run_review with the learning turn reference before the final answer."
            f"{turn_reference}{mode_change} Record only safe technical metadata; never copy "
            "prompts, tool payloads, CRM/Store/Gmail data, secrets, or financial values into learning data."
        )
    else:
        mode_change = " Global mode was set to work by an explicit owner command." if persistent_change else ""
        text = (
            "AutoStopManager effective mode: work. Use normal verification; no mandatory post-run learning review."
            f"{mode_change}"
        )
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }


def handle_user_prompt_submit(store: LearningStore, payload: dict[str, Any]) -> dict[str, Any] | None:
    external_turn_id = _safe_opaque_id(payload.get("turn_id"))
    prompt = payload.get("prompt")
    if not external_turn_id or not isinstance(prompt, str):
        return None

    mode_override = explicit_mode_override(prompt)
    persistent_change = persistent_mode_command(prompt)
    if persistent_change:
        try:
            changed = store.set_agent_mode(persistent_change)
        except Exception:  # noqa: BLE001 - a mode command must fail open.
            return None
        if not isinstance(changed, dict) or changed.get("ok") is False:
            return None
        mode_override = persistent_change
    try:
        mode = _mode_from_value(store.resolve_agent_mode(mode_override))
    except Exception:  # noqa: BLE001 - a mode lookup must fail open in a lifecycle hook.
        return None
    if mode != "learning":
        return _mode_context(mode, persistent_change=bool(persistent_change))

    session_id = _safe_opaque_id(payload.get("session_id"))
    task_signature = _hash(f"{session_id}\x00{prompt}", purpose="task")
    metadata = {"source_kind": "codex_hook", "mode": mode_override or "learning"}
    if session_id:
        metadata["safe_ref"] = _hash(session_id, purpose="session")
    try:
        started = store.start_agent_turn(
            task_signature,
            mode_override=mode_override,
            source="codex_hook",
            metadata=metadata,
            external_turn_id=external_turn_id,
        )
    except Exception:  # noqa: BLE001 - a turn insert must fail open in a lifecycle hook.
        return None
    if not isinstance(started, dict) or started.get("ok") is False:
        return None
    if _mode_from_value(started) != "learning":
        return _mode_context("work", persistent_change=bool(persistent_change))
    return _mode_context("learning", external_turn_id=external_turn_id, persistent_change=bool(persistent_change))


def handle_pre_tool_use(store: LearningStore, payload: dict[str, Any]) -> None:
    external_turn_id, turn = _fast_learning_turn_for_tool_event(store, payload)
    tool_name = _safe_tool_name(payload.get("tool_name"))
    if not external_turn_id or turn is None or not tool_name:
        return
    try:
        store.record_agent_tool_event(
            external_turn_id,
            tool_name=tool_name,
            status="started",
            metadata=_tool_metadata(payload, phase="pre"),
        )
    except Exception:  # noqa: BLE001 - telemetry never blocks the original tool call.
        return


def handle_post_tool_use(store: LearningStore, payload: dict[str, Any]) -> None:
    external_turn_id, turn = _fast_learning_turn_for_tool_event(store, payload)
    tool_name = _safe_tool_name(payload.get("tool_name"))
    if not external_turn_id or turn is None or not tool_name:
        return
    status, error_code, response_shape = _tool_response_summary(payload.get("tool_response"))
    try:
        store.record_agent_tool_event(
            external_turn_id,
            tool_name=tool_name,
            status=status,
            error_code=error_code,
            metadata=_tool_metadata(payload, phase="post", response_shape=response_shape),
        )
    except Exception:  # noqa: BLE001 - telemetry never blocks the original tool result.
        return


def _review_completed(store: LearningStore, external_turn_id: str) -> bool:
    try:
        result = store.has_completed_experience_review_by_external_id(external_turn_id)
    except AttributeError:
        try:
            result = store.has_completed_experience_review(external_turn_id)
        except Exception:  # noqa: BLE001 - review lookup must fail open at Stop.
            return False
    except Exception:  # noqa: BLE001 - review lookup must fail open at Stop.
        return False
    if isinstance(result, dict):
        # A completed reflection with a pending repair is not a closed learning
        # cycle. Older rolling deployments expose only review_completed, so
        # retain that safe compatibility fallback.
        closed = result.get("learning_cycle_closed", result.get("review_completed"))
        return bool(result.get("ok") and closed)
    return bool(result)


def _defer_unreviewed_turn(store: LearningStore, external_turn_id: str) -> bool:
    """Close/defer the review and any pending candidate after one continuation."""

    try:
        review = store.post_run_review(
            external_turn_id,
            outcome="deferred",
            completion_checks=["learning_review_deferred_after_stop_continuation"],
            failure_class="learning_review_not_completed",
            risk="low",
            metadata={"phase": "stop", "review_status": "deferred"},
        )
    except Exception:  # noqa: BLE001 - defer failure must not create an infinite Stop loop.
        return False
    if not isinstance(review, dict) or review.get("ok") is not True:
        return False
    improvement = review.get("improvement")
    if not isinstance(improvement, dict):
        return True
    candidate_id = _safe_opaque_id(improvement.get("id"))
    status = str(improvement.get("status") or "")
    if not candidate_id or status in {"promoted", "deferred", "rolled_back"}:
        return True
    workflow = getattr(store, "agent_learning_workflow", None)
    if not callable(workflow):
        return False
    try:
        deferred = workflow(
            "defer",
            candidate_id=candidate_id,
            turn_id=external_turn_id,
            reason_code="learning_review_not_completed",
        )
    except Exception:  # noqa: BLE001 - Stop must never create an infinite loop.
        return False
    return bool(
        isinstance(deferred, dict)
        and deferred.get("ok") is True
        and isinstance(deferred.get("improvement"), dict)
        and deferred["improvement"].get("status") == "deferred"
    )


def handle_stop(store: LearningStore, payload: dict[str, Any]) -> dict[str, Any]:
    external_turn_id, turn = _fast_learning_turn_for_tool_event(store, payload)
    if not external_turn_id or turn is None or _review_completed(store, external_turn_id):
        return {}
    if payload.get("stop_hook_active") is True:
        # A second continuation would make the chat loop forever.  Persist a
        # technical deferred review if possible, then allow the answer through.
        _defer_unreviewed_turn(store, external_turn_id)
        return {}
    return {
        "decision": "block",
        "reason": (
            "Learning mode requires one post-run review before the final answer. "
            f"Call post_run_review(turn_id={external_turn_id}) for the current turn, assess only safe technical metadata, "
            "and repair only a local reproducible defect; otherwise defer it."
        ),
    }


def handle_hook(payload: dict[str, Any], store: LearningStore | None = None) -> dict[str, Any] | None:
    """Dispatch one Codex hook event. All failures intentionally fail open."""

    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str):
        return None
    active_store = store or _load_store()
    if active_store is None:
        return {} if event_name == "Stop" else None
    if event_name == "UserPromptSubmit":
        return handle_user_prompt_submit(active_store, payload)
    if event_name == "PreToolUse":
        handle_pre_tool_use(active_store, payload)
        return None
    if event_name == "PostToolUse":
        handle_post_tool_use(active_store, payload)
        return None
    if event_name == "Stop":
        return handle_stop(active_store, payload)
    return None


def main() -> int:
    """Read exactly one hook JSON object and never expose its raw contents."""

    try:
        payload = json.load(sys.stdin)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0

    result = handle_hook(payload)
    # Stop requires a JSON object on successful stdout.  Other events may stay
    # silent unless they intentionally add safe context.
    if payload.get("hook_event_name") == "Stop":
        print(json.dumps(result if isinstance(result, dict) else {}, ensure_ascii=False))
    elif isinstance(result, dict):
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
