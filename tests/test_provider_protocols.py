import json
from pathlib import Path

import pytest
from sdd_providers.protocols import ProtocolError, opencode_response, structured_answer


def captured():
    return [
        json.loads(line)
        for line in Path(__file__)
        .with_name("fixtures")
        .joinpath("opencode-2.0.16.jsonl")
        .read_text()
        .splitlines()
        if line.strip()
    ]


def test_real_opencode_2016_fenced_answer_without_terminal_stop():
    events = captured()
    assert events[-1]["type"] == "text"
    answer, session = opencode_response(events)
    assert session == events[-1]["sessionID"]
    assert structured_answer(answer) == {
        "outcome": "done",
        "reason": "smoke",
        "standards": True,
        "specification": True,
    }


@pytest.mark.parametrize("change", ["error", "tool", "session", "truncated", "no_answer"])
def test_captured_stream_corruption_fails_closed(change):
    events = captured()
    if change == "error":
        events.append({"type": "error"})
    elif change == "tool":
        events.append({"type": "tool_use", "part": {}})
    elif change == "session":
        events[-1]["sessionID"] = "other"
    elif change == "truncated":
        events[-1]["part"]["text"] = events[-1]["part"]["text"][:-4]
    else:
        events.pop()
    with pytest.raises(ProtocolError):
        structured_answer(opencode_response(events)[0])


def test_repeated_part_replaces_snapshot_without_duplicate_answer():
    events = captured()
    events.append(events[-1])
    assert structured_answer(opencode_response(events)[0])["outcome"] == "done"
