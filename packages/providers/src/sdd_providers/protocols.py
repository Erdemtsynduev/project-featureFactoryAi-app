"""Provider-specific response parsers, independent of process management."""

from sdd_core.codec import object_json, text
from sdd_core.models import Json


class ProtocolError(ValueError):
    """A completed execution returned an incompatible response; do not retry a model."""


def structured_answer(response: str) -> dict[str, Json]:
    value = response.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) < 3 or lines[0] not in ("```json", "```") or lines[-1] != "```":
            raise ProtocolError("Expected one complete JSON block")
        value = "\n".join(lines[1:-1])
    try:
        return object_json(value)
    except ValueError as error:
        raise ProtocolError("Invalid structured agent answer") from error


def cursor_response(events: list[dict[str, Json]]) -> tuple[str, str]:
    terminal = [event for event in events if event.get("type") == "result"]
    if (
        len(terminal) != 1
        or terminal[0].get("is_error") is not False
        or terminal[0].get("subtype") != "success"
    ):
        raise ProtocolError("Cursor stream has no unique successful terminal result")
    return text(terminal[0].get("result"), "result"), text(
        terminal[0].get("session_id"), "session_id"
    )


def opencode_response(events: list[dict[str, Json]]) -> tuple[str, str]:
    session: str | None = None
    messages: dict[str, dict[str, str]] = {}
    final_message: str | None = None
    action_after_answer = False
    for index, event in enumerate(events):
        if event.get("type") == "error":
            raise ProtocolError("OpenCode reported an error")
        if event.get("sessionID") is not None:
            observed = text(event["sessionID"], "sessionID")
            if session is not None and session != observed:
                raise ProtocolError("Mixed provider sessions")
            session = observed
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        if event.get("type") in ("tool_use", "step_start"):
            action_after_answer = True
        if event.get("type") == "text":
            message = text(part.get("messageID", "final"), "messageID")
            identity = text(part.get("id", part.get("partID", str(index))), "partID")
            messages.setdefault(message, {})[identity] = text(part.get("text"), "text")
            final_message, action_after_answer = message, False
    if session is None or final_message is None or action_after_answer:
        raise ProtocolError("OpenCode stream has no final assistant answer")
    # Called only after the host has confirmed a zero exit and stopped descendants.
    # OpenCode 2.0.16 need not emit step_finish(reason=stop) after its final text.
    return "".join(messages[final_message].values()), session
