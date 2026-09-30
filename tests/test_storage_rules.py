"""The storage rules both backends apply, as pure decisions."""

from dataclasses import replace

import pytest
from sdd_core.models import Run
from sdd_core.ports import Conflict
from sdd_core.records import EffectRecord
from sdd_core.storage_rules import (
    check_execution_binding,
    live_process,
    pin_changes,
    runnable,
    started_unfinished,
)


def run(identifier: str, **fields: object) -> Run:
    return replace(Run(identifier, "flow", "work", "rev", paused=False), **fields)


def test_runnable_is_resumed_unsettled_with_accepted_prerequisites_fairest_first():
    candidates = [
        (run("late"), [], 5.0, 1.0),
        (run("early"), ["accepted"], 2.0, 2.0),
        (run("waits"), ["ready"], 0.0, 0.0),
        (run("paused", paused=True), [], 0.0, 0.0),
        (run("blocked", status="blocked"), [], 0.0, 0.0),
        (run("tie"), [], 2.0, 1.0),
    ]
    assert runnable(candidates) == ("tie", "early", "late")


def test_a_pin_follows_the_manifest_only_between_attempts():
    assert pin_changes(None, "v1", live=True) and pin_changes("v1", "v2", live=False)
    assert not pin_changes("v1", "v1", live=True)
    with pytest.raises(Conflict):
        pin_changes("v1", "v2", live=True)
    assert live_process("agent", "running") and not live_process("human", "running")
    assert not live_process("agent", "done")


def test_an_attempt_goes_to_one_execution_backend_once():
    record = EffectRecord("a", "run", "agent", "pending", "/w")
    check_execution_binding(record, "run", None, ("x", "doc"))
    check_execution_binding(record, "run", ("x", "doc"), ("x", "doc"))
    for bad in (
        (record, "other", None),
        (record, "run", ("y", "doc")),
        (replace(record, pid=7), "run", None),
        (replace(record, status="done"), "run", None),
    ):
        with pytest.raises(Conflict):
            check_execution_binding(bad[0], bad[1], bad[2], ("x", "doc"))
    assert started_unfinished(run("r", generation=1)) and not started_unfinished(run("r"))
