"""Headless API client and local runtime entry point."""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path

from sdd_core.codec import (
    canonical,
    encode,
    integer,
    object_json,
    sequence,
    text,
    workflow_json,
    workflow_load,
)
from sdd_core.editor import compare, insert_step, simulate
from sdd_core.graph import validate
from sdd_core.models import Step, Workflow
from sdd_storage.store import Store

from sdd_runtime.composition import registry
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.files import atomic_write, revision
from sdd_runtime.lock import Lease
from sdd_runtime.platform import Job, start_contained
from sdd_runtime.versions import engine
from sdd_runtime.watchdog import watch_parent


def supervise(args: argparse.Namespace) -> int:
    with Lease(args.database.with_suffix(".supervisor.lock")):
        record = args.database.with_suffix(".supervisor.json")
        failures = (
            0
            if args.reset_retries or not record.exists()
            else integer(
                object_json(record.read_text(encoding="utf-8")).get("failures", 0), "failures"
            )
        )
        while failures <= 3:
            command = [
                sys.executable,
                "-m",
                "sdd_runtime.cli",
                "--database",
                str(args.database),
                "run",
                "--gated",
                "--parent-pid",
                str(os.getpid()),
            ]
            if args.config:
                command += ["--config", str(args.config.resolve())]
            job = Job()
            child: subprocess.Popen[bytes] | None = None
            try:
                child = start_contained(command, [job], stdin=subprocess.PIPE)
                atomic_write(
                    record,
                    canonical(
                        {"failures": failures, "coordinator_pid": child.pid, "started": time.time()}
                    ),
                )
                if child.stdin is None:
                    raise RuntimeError("No coordinator launch gate")
                child.stdin.write(b"GO\n")
                child.stdin.close()
                code = child.wait()
            finally:
                job.close()
                if child and child.poll() is None:
                    child.wait(timeout=10)
            if code == 0:
                return 0
            failures += 1
            atomic_write(
                args.database.with_suffix(".supervisor.json"),
                canonical(
                    {"failures": failures, "exit": code, "next_retry": time.time() + 2**failures}
                ),
            )
            if failures <= 3:
                time.sleep(2**failures)
        return 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="ffai", description="Feature Factory AI - deterministic workflow orchestration"
    )
    root.add_argument("--database", type=Path, default=Path(".state/engine.sqlite3"))
    root.add_argument("--version", action="version", version=f"Feature Factory AI {engine()}")
    commands = root.add_subparsers(dest="action")
    demo = commands.add_parser("demo", help="Run an isolated example without model calls")
    demo.add_argument("--directory", type=Path, default=Path(".state/demo"))
    ui = commands.add_parser("ui", help="Open the local visual workspace")
    ui.add_argument("--port", type=int, default=8787)
    ui.add_argument("--config", type=Path)
    ui.add_argument("--no-browser", action="store_true")
    commands.add_parser("schema")
    export = commands.add_parser("export")
    export.add_argument("definition")
    export.add_argument("output", type=Path)
    diff = commands.add_parser("diff")
    diff.add_argument("left", type=Path)
    diff.add_argument("right", type=Path)
    simulation = commands.add_parser("simulate")
    simulation.add_argument("file", type=Path)
    simulation.add_argument("outcomes", nargs="*")
    insert = commands.add_parser("insert")
    insert.add_argument("file", type=Path)
    insert.add_argument("--after", required=True)
    insert.add_argument("--outcome", default="done")
    insert.add_argument("--id", required=True)
    insert.add_argument("--handler", required=True)
    insert.add_argument("--prompt-file", type=Path, required=True)
    insert.add_argument("--required", action="store_true")
    insert.add_argument("--mutates", action="store_true")
    insert.add_argument("--output", type=Path, required=True)
    policy = commands.add_parser("policy")
    policy.add_argument("workspace", type=Path)
    policy.add_argument("--mandatory", nargs="+", required=True)
    for name in ("validate", "publish"):
        command = commands.add_parser(name)
        command.add_argument("file", type=Path)
        command.add_argument("--config", type=Path)
        command.add_argument("--mandatory", nargs="*", default=[])
    template = commands.add_parser("template")
    template.add_argument("name", choices=["feature", "approved-feature", "main-flow", "interview"])
    template.add_argument("--output", type=Path, required=True)
    create = commands.add_parser("create")
    create.add_argument("definition")
    create.add_argument("--config", type=Path)
    create.add_argument("workspace", type=Path)
    create.add_argument("--context", default="")
    create.add_argument("--id", default=None)
    create.add_argument("--depends-on", nargs="*", default=[])
    for name in ("status", "events", "replay"):
        commands.add_parser(name).add_argument("id")
    for name in ("resume", "pause", "retry", "stop", "auto", "manual"):
        command = commands.add_parser(name)
        command.add_argument("id")
        command.add_argument("--version", type=int, required=True)
        command.add_argument("--request-id", default=None)
    run = commands.add_parser("run")
    run.add_argument("--config", type=Path)
    run.add_argument("--once", action="store_true")
    run.add_argument("--seconds", type=float)
    run.add_argument("--gated", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    supervisor = commands.add_parser(
        "supervise", aliases=["start"], help="Start the supervised queue in the foreground"
    )
    supervisor.add_argument("--config", type=Path)
    supervisor.add_argument("--reset-retries", action="store_true")
    answer = commands.add_parser("answer")
    answer.add_argument("id")
    answer.add_argument("outcome")
    answer.add_argument("text")
    answer.add_argument(
        "--version", type=int, help="Task version the answer refers to (default: current)"
    )
    commands.add_parser("backup").add_argument("target", type=Path)
    commands.add_parser("catalog").add_argument("--config", type=Path)
    agents = commands.add_parser("agents")
    agents.add_argument("operation", choices=["discover"])
    agents.add_argument("--adapter")
    agents.add_argument("--extension", action="append", default=[])
    models = commands.add_parser("models")
    models.add_argument("operation", choices=["list"])
    models.add_argument("--runner", required=True)
    models.add_argument("--config", type=Path, required=True)
    profiles = commands.add_parser("profiles")
    profiles.add_argument("operation", choices=["validate"])
    profiles.add_argument("--config", type=Path, required=True)
    profiles.add_argument("--workflow", type=Path)
    return root


def _resolved(flow: Workflow, config: Path | None) -> Workflow:
    """`flow` with its named agent profiles resolved from `config`, when one is given."""
    if config is None:
        return flow
    from sdd_core.profiles import resolve_profiles

    from sdd_runtime.profiles import load_profiles

    registry(config)  # the profiles must load as handlers too
    return resolve_profiles(flow, load_profiles(config).profiles)


def _read_flow(path: Path) -> Workflow:
    return workflow_load(path.read_text(encoding="utf-8"))


# Commands without an engine database ---------------------------------------------------


def _ui(args: Namespace) -> int:
    from collections.abc import Callable
    from importlib.metadata import entry_points
    from typing import cast

    installed = entry_points(group="ffai.commands", name="ui")
    if len(installed) != 1:
        raise ValueError("Install the feature-factory-ai product to use its UI")
    launch = cast(Callable[[Path, Path | None, int, bool], int], next(iter(installed)).load())
    return launch(args.database, args.config, args.port, not args.no_browser)


def _demo(args: Namespace) -> int:
    from sdd_runtime.demo import demonstrate

    directory = args.directory.resolve() / uuid.uuid4().hex[:12]
    directory.mkdir(parents=True, exist_ok=False)
    engine = Engine(Store(directory / "engine.db"))
    identifier = demonstrate(engine, registry(None), directory)
    print(
        canonical(
            {
                "status": "accepted",
                "run": identifier,
                "model_calls": 0,
                "database": str(engine.store.path),
            }
        )
    )
    return 0


def _agents(args: Namespace) -> int:
    from sdd_providers.catalog import adapters

    from sdd_runtime.discovery import discover

    catalog = adapters(tuple(args.extension))
    selected = [catalog[args.adapter]] if args.adapter else list(catalog.values())
    print(canonical([encode(discover(adapter)) for adapter in selected]))
    return 0


def _models(args: Namespace) -> int:
    from sdd_providers.catalog import adapters

    from sdd_runtime.discovery import list_models
    from sdd_runtime.profiles import load_profiles

    runner = load_profiles(args.config).runners[args.runner]
    document = object_json(args.config.read_text(encoding="utf-8"))
    catalog = adapters(
        tuple(text(x, "extension") for x in sequence(document.get("agent_extensions", [])))
    )
    adapter = catalog[runner.adapter]
    command = (runner.executable, *runner.arguments)
    print(canonical(list_models(command, adapter.model_arguments, adapter.parse_models)))
    return 0


def _profiles(args: Namespace) -> int:
    if args.workflow:
        flow = _resolved(_read_flow(args.workflow), args.config)
        validate(flow)
        print(workflow_json(flow))
    else:
        print(canonical([encode(item) for item in registry(args.config).manifests()]))
    return 0


def _schema(args: Namespace) -> int:
    from sdd_core.schema import workflow_schema

    print(json.dumps(workflow_schema(), indent=2))
    return 0


def _diff(args: Namespace) -> int:
    print(compare(_read_flow(args.left), _read_flow(args.right)))
    return 0


def _simulate(args: Namespace) -> int:
    print(canonical(simulate(_read_flow(args.file), tuple(args.outcomes))))
    return 0


def _insert(args: Namespace) -> int:
    step = Step(
        args.id,
        "agent",
        args.handler,
        args.prompt_file.read_text(encoding="utf-8"),
        required=args.required,
        mutates=args.mutates,
    )
    edited = insert_step(_read_flow(args.file), args.after, args.outcome, step)
    atomic_write(args.output, workflow_json(edited))
    return 0


def _template(args: Namespace) -> int:
    from sdd_workflows.templates import approved_feature, feature, interview, main_flow

    builders: dict[str, Callable[[], Workflow]] = {
        "feature": feature,
        "approved-feature": approved_feature,
        "main-flow": main_flow,
        "interview": interview,
    }
    atomic_write(args.output, workflow_json(builders[args.name]()))
    return 0


def _validate(args: Namespace) -> int:
    validate(_resolved(_read_flow(args.file), args.config), tuple(args.mandatory))
    print("valid")
    return 0


def _catalog(args: Namespace) -> int:
    print(canonical([encode(item) for item in registry(args.config).manifests()]))
    return 0


STANDALONE: dict[str, Callable[[Namespace], int]] = {
    "ui": _ui,
    "demo": _demo,
    "agents": _agents,
    "models": _models,
    "profiles": _profiles,
    "schema": _schema,
    "diff": _diff,
    "simulate": _simulate,
    "insert": _insert,
    "template": _template,
    "validate": _validate,
    "catalog": _catalog,
    "supervise": supervise,
    "start": supervise,
}


# Commands on an engine database -----------------------------------------------------


def _policy(engine: Engine, args: Namespace) -> None:
    with engine.store.unit() as db:
        db.set_policy(str(args.workspace.resolve(strict=True)), tuple(args.mandatory))


def _export(engine: Engine, args: Namespace) -> None:
    atomic_write(args.output, workflow_json(engine.store.workflow(args.definition)))


def _publish(engine: Engine, args: Namespace) -> None:
    flow = _resolved(_read_flow(args.file), args.config)
    print(engine.store.publish(flow, tuple(args.mandatory)))


def _create(engine: Engine, args: Namespace) -> None:
    definition = args.definition
    if args.config:
        definition = engine.store.publish(_resolved(engine.store.workflow(definition), args.config))
    state = engine.create(
        args.id or uuid.uuid4().hex,
        definition,
        args.workspace,
        args.context,
        revision(args.workspace),
        time.time(),
        tuple(args.depends_on),
    )
    if args.config:
        coordinator = Coordinator(
            engine, registry(args.config), args.database.with_suffix(".health.json")
        )
        try:
            coordinator.bind(state.id)
        finally:
            coordinator.close()
    print(canonical(encode(state)))


def _command(engine: Engine, args: Namespace) -> None:
    request = args.request_id or uuid.uuid4().hex
    state = engine.command(args.id, args.action, request, args.version, time.time())
    print(canonical(encode(state)))


def _status(engine: Engine, args: Namespace) -> None:
    print(json.dumps(encode(engine.store.get(args.id)), indent=2))


def _events(engine: Engine, args: Namespace) -> None:
    print(canonical(engine.store.history(args.id)))


def _replay(engine: Engine, args: Namespace) -> None:
    print(canonical(encode(engine.store.replay(args.id))))


def _backup(engine: Engine, args: Namespace) -> None:
    engine.store.backup(args.target)


def _answer(engine: Engine, args: Namespace) -> None:
    expected = engine.store.get(args.id).version if args.version is None else args.version
    engine.answer(args.id, args.outcome, args.text, {}, expected, time.time())


def _run(engine: Engine, args: Namespace) -> None:
    with Lease(args.database.with_suffix(".coordinator.lock")):
        coordinator = Coordinator(
            engine, registry(args.config), args.database.with_suffix(".health.json")
        )
        coordinator.restore(time.time())
        deadline = None if args.seconds is None else time.monotonic() + args.seconds
        try:
            while True:
                coordinator.tick()
                if args.once or (deadline is not None and time.monotonic() >= deadline):
                    break
                time.sleep(0.25)
        finally:
            coordinator.close()


ON_ENGINE: dict[str, Callable[[Engine, Namespace], None]] = {
    "policy": _policy,
    "export": _export,
    "publish": _publish,
    "create": _create,
    **{command: _command for command in ("pause", "resume", "retry", "stop", "auto", "manual")},
    "status": _status,
    "events": _events,
    "replay": _replay,
    "backup": _backup,
    "answer": _answer,
    "run": _run,
}


def main() -> int:
    argument_parser = parser()
    args = argument_parser.parse_args()
    if args.action is None:
        argument_parser.print_help()
        return 0
    if args.action in STANDALONE:
        return STANDALONE[args.action](args)
    if args.action == "run" and args.gated:
        watch_parent(args.parent_pid)
        if sys.stdin.readline().strip() != "GO":
            return 2
    ON_ENGINE[args.action](Engine(Store(args.database)), args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
