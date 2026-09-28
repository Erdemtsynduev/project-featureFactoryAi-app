import json
import sys
from dataclasses import replace

import pytest
from sdd_core.codec import object_json
from sdd_core.models import Attempt, Step, Workflow
from sdd_core.profiles import AgentProfile, resolve_profiles
from sdd_core.sdk import Packet, handler_key
from sdd_runtime.cli import registry
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.profiles import load_profiles
from sdd_storage.store import Store


def configuration(tmp_path, model="test/model-a"):
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "runners": {"local": {"adapter": "opencode", "executable": sys.executable}},
                "profiles": {
                    "implement": {
                        "runner": "local",
                        "model": model,
                        "permissions": "workspace-write",
                        "timeout_seconds": 42,
                    },
                    "review": {
                        "runner": "local",
                        "model": "test/model-b",
                        "permissions": "workspace-write",
                    },
                },
            }
        )
    )
    return path


def workflow():
    return Workflow(
        "profiles",
        "work",
        (
            Step(
                "work",
                "agent",
                profile="implement",
                mutates=True,
                transitions=(("done", "finish"),),
            ),
            Step("finish", "finish"),
        ),
    )


def test_profiles_select_models_on_same_installation(tmp_path):
    config = configuration(tmp_path)
    profiles = load_profiles(config)
    handlers = registry(config)
    flow = resolve_profiles(workflow(), profiles.profiles)
    step = flow.step("work")
    assert step.timeout == 42 and handler_key(step) == "implement"
    assert object_json(step.config)["profile_snapshot"]["model"] == "test/model-a"
    packet = Packet(
        "one", Attempt("a", "work", 1, 0, 42, "rev"), step, str(tmp_path), str(tmp_path), ""
    )
    for name, model in (("implement", "test/model-a"), ("review", "test/model-b")):
        argv = handlers.get(name).prepare(packet).argv
        assert argv[argv.index("--model") + 1] == model
        assert argv[0] == sys.executable


def test_existing_run_rejects_changed_model(tmp_path):
    config = configuration(tmp_path)
    engine = Engine(Store(tmp_path / "engine.db"))
    flow = resolve_profiles(workflow(), load_profiles(config).profiles)
    engine.create("one", engine.store.publish(flow), tmp_path, "task", "rev", 0)
    original = Coordinator(engine, registry(config))
    try:
        original.bind("one")
    finally:
        original.close()
    configuration(tmp_path, "test/other")
    changed = Coordinator(engine, registry(config))
    try:
        with pytest.raises(ValueError, match="creation snapshot"):
            changed.bind("one")
    finally:
        changed.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", ""),
        ("model", "<exact-id>"),
        ("runner", "missing"),
        ("permissions", "admin"),
        ("timeout_seconds", 0),
        ("timeout_seconds", True),
        ("secret", "no"),
    ],
)
def test_invalid_profile_rejected(tmp_path, field, value):
    path = configuration(tmp_path)
    document = json.loads(path.read_text())
    document["profiles"]["implement"][field] = value
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        load_profiles(path)


def test_profile_permissions_and_missing_profile():
    profile = AgentProfile("implement", "local", "test/model")
    with pytest.raises(ValueError, match="Read-only"):
        resolve_profiles(workflow(), (profile,))
    with pytest.raises(ValueError, match="Unknown profile"):
        resolve_profiles(workflow(), ())
    with pytest.raises(ValueError, match="Duplicate"):
        resolve_profiles(workflow(), (profile, profile))


def test_unqualified_readonly_is_not_advertised_as_safe(tmp_path):
    path = configuration(tmp_path)
    document = json.loads(path.read_text())
    document["profiles"]["implement"]["permissions"] = "read-only"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="not qualified"):
        registry(path)


def test_profile_resolution_keeps_original_definition():
    original = workflow()
    resolved = resolve_profiles(
        original, (AgentProfile("implement", "local", "test/model", "workspace-write"),)
    )
    assert original.step("work").config == "{}"
    assert resolved != original
    assert replace(resolved, steps=original.steps) == original
