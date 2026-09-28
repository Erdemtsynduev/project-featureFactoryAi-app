"""Synthetic protocol fixtures, deliberately not labelled captured live output."""

import json
import sys
from dataclasses import replace

import pytest
from sdd_core.models import Attempt, Step
from sdd_core.sdk import Packet
from sdd_providers.structured_cli import Invocation, StructuredCliHandler


@pytest.fixture
def packet(tmp_path):
    folder = tmp_path / ".sdd-engine" / "attempt"
    folder.mkdir(parents=True)
    return Packet(
        "run",
        Attempt("a", "work", 1, 0, 100, "rev"),
        Step("work", "agent", "test", transitions=(("done", "finish"),), mutates=True),
        str(tmp_path),
        str(folder),
        "",
    )


@pytest.mark.parametrize("provider", ["cursor", "opencode"])
@pytest.mark.parametrize("variant", ["success", "truncated", "invalid", "failure"])
def test_provider_protocol_matrix(packet, provider, variant):
    from pathlib import Path

    response = json.dumps(
        {"outcome": "done", "reason": "ok", "standards": True, "specification": True}
    )
    if variant == "invalid":
        response = "not JSON"
    events = (
        [
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": response,
                "session_id": "session",
            }
        ]
        if provider == "cursor"
        else [
            {"type": "text", "sessionID": "session", "part": {"text": response}},
            {"type": "step_finish", "sessionID": "session", "part": {"reason": "stop"}},
        ]
    )
    if variant == "truncated":
        events = (
            events[:-1]
            if provider == "cursor"
            else [{"type": "text", "sessionID": "session", "part": {"text": response[:-2]}}]
        )
    Path(packet.directory, "stdout.log").write_text("\n".join(json.dumps(e) for e in events))
    handler = StructuredCliHandler(provider, Invocation(sys.executable))
    if variant in ("invalid", "truncated"):
        with pytest.raises(ValueError):
            handler.collect(packet, 0, "rev")
    else:
        result = handler.collect(packet, 2 if variant == "failure" else 0, "rev")
        assert result.outcome == ("blocked" if variant == "failure" else "done")
        assert result.usage.input_tokens is None


def test_launch_is_explicit_fresh_and_pins_entrypoint(packet, tmp_path):
    entry = tmp_path / "entry.js"
    entry.write_text("initial")
    handler = StructuredCliHandler("cursor", Invocation(sys.executable, (str(entry),)))
    launch = handler.prepare(replace(packet, step=replace(packet.step, mutates=False)))
    assert launch.argv[:2] == (sys.executable, str(entry))
    assert "ask" in launch.argv and "--continue" not in launch.argv
    entry.write_text("updated")
    with pytest.raises(ValueError, match="Pinned"):
        handler.prepare(packet)


def test_opencode_never_attaches_to_shared_service(packet):
    handler = StructuredCliHandler("opencode", Invocation(sys.executable))
    assert "--standalone" in handler.prepare(packet).argv
    with pytest.raises(ValueError, match="read-only"):
        handler.prepare(replace(packet, step=replace(packet.step, mutates=False)))
