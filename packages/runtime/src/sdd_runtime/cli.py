"""Headless API client and local runtime entry point."""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from sdd_core.codec import (
    canonical,
    integer,
    mapping,
    object_json,
    sequence,
    text,
    workflow_json,
    workflow_load,
)
from sdd_core.editor import compare, insert_step, simulate
from sdd_core.graph import validate
from sdd_core.models import Step
from sdd_core.sdk import Handler, Registry
from sdd_storage.store import Store

from sdd_runtime.coordinator import Coordinator
from sdd_runtime.engine import Engine
from sdd_runtime.files import atomic_write, revision
from sdd_runtime.lock import Lease
from sdd_runtime.platform import NO_WINDOW, Job
from sdd_runtime.plugins import load_extensions
from sdd_runtime.watchdog import watch_parent


def registry(config: Path | None) -> Registry:
    from sdd_providers.catalog import adapters
    from sdd_providers.handlers import CommandHandler
    from sdd_providers.structured_cli import Invocation

    from sdd_runtime.profiles import RunnerInstallation, load_profiles, register_profiles

    result = Registry()
    result.register(CommandHandler())
    if config is None:
        return result
    doc = object_json(config.read_text(encoding="utf-8"))
    catalog = adapters(
        tuple(text(x, "extension") for x in sequence(doc.get("agent_extensions", [])))
    )

    def factory(installation: RunnerInstallation, model: str) -> Handler:
        if installation.adapter not in catalog:
            raise ValueError(f"Unknown agent adapter: {installation.adapter}")
        return catalog[installation.adapter].factory(
            Invocation(installation.executable, installation.arguments), model
        )

    if "profiles" in doc:
        profiles = load_profiles(config)
        for profile in profiles.profiles:
            adapter_id = profiles.runners[profile.runner].adapter
            if adapter_id not in catalog:
                raise ValueError(f"Unknown agent adapter: {adapter_id}")
            profile_adapter = catalog[adapter_id]
            if profile.permissions == "read-only" and not profile_adapter.read_only:
                raise ValueError(f"{profile_adapter.id} read-only policy is not qualified")
        register_profiles(result, profiles, factory)
    else:
        for raw in sequence(doc.get("providers", [])):
            item = mapping(raw)
            adapter = text(item.get("id"), "id")
            if adapter not in catalog:
                raise ValueError(f"Unknown agent adapter: {adapter}")
            model = item.get("model")
            handler = catalog[adapter].factory(
                Invocation(
                    text(item.get("executable"), "executable"),
                    tuple(text(arg, "argument") for arg in sequence(item.get("arguments", []))),
                ),
                None if model is None else text(model, "model"),
            )
            result.register(handler, name=text(item.get("name", adapter), "name"))
        load_extensions(
            result, tuple(text(x, "extension") for x in sequence(doc.get("extensions", [])))
        )
    return result


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
                child = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    creationflags=NO_WINDOW,
                    start_new_session=os.name != "nt",
                )
                job.assign(child.pid)
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
        prog="ffai", description="Feature Factory AI — deterministic workflow orchestration"
    )
    root.add_argument("--database", type=Path, default=Path(".state/engine.sqlite3"))
    root.add_argument("--version", action="version", version="Feature Factory AI 0.1.0")
    commands = root.add_subparsers(dest="action")
    demo = commands.add_parser("demo", help="Run an isolated example without model calls")
    demo.add_argument("--directory", type=Path, default=Path(".state/demo"))
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
    template.add_argument("name", choices=["main-flow", "interview"])
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
    for name in ("resume", "pause", "retry", "stop"):
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


def main() -> int:
    argument_parser = parser()
    args = argument_parser.parse_args()
    if args.action is None:
        argument_parser.print_help()
        return 0
    if args.action == "demo":
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
    if args.action in ("agents", "models", "profiles"):
        from sdd_core.profiles import resolve_profiles
        from sdd_providers.catalog import adapters

        from sdd_runtime.discovery import discover, list_models
        from sdd_runtime.profiles import load_profiles

        if args.action == "agents":
            catalog = adapters(tuple(args.extension))
            selected = [catalog[args.adapter]] if args.adapter else list(catalog.values())
            print(canonical([asdict(discover(adapter)) for adapter in selected]))
        elif args.action == "models":
            config = load_profiles(args.config)
            runner = config.runners[args.runner]
            document = object_json(args.config.read_text(encoding="utf-8"))
            catalog = adapters(
                tuple(text(x, "extension") for x in sequence(document.get("agent_extensions", [])))
            )
            print(
                canonical(
                    list_models(
                        (runner.executable, *runner.arguments),
                        catalog[runner.adapter].model_arguments,
                        catalog[runner.adapter].parse_models,
                    )
                )
            )
        else:
            config = load_profiles(args.config)
            resolved = registry(args.config)
            if args.workflow:
                flow = resolve_profiles(
                    workflow_load(args.workflow.read_text(encoding="utf-8")), config.profiles
                )
                validate(flow)
                print(workflow_json(flow))
            else:
                print(canonical([asdict(item) for item in resolved.manifests()]))
        return 0

    if args.action == "schema":
        from sdd_core.schema import workflow_schema

        print(json.dumps(workflow_schema(), indent=2))
        return 0
    if args.action == "diff":
        print(
            compare(
                workflow_load(args.left.read_text(encoding="utf-8")),
                workflow_load(args.right.read_text(encoding="utf-8")),
            )
        )
        return 0
    if args.action == "simulate":
        print(
            canonical(
                simulate(workflow_load(args.file.read_text(encoding="utf-8")), tuple(args.outcomes))
            )
        )
        return 0
    if args.action == "insert":
        workflow = workflow_load(args.file.read_text(encoding="utf-8"))
        step = Step(
            args.id,
            "agent",
            args.handler,
            args.prompt_file.read_text(encoding="utf-8"),
            required=args.required,
            mutates=args.mutates,
        )
        atomic_write(
            args.output, workflow_json(insert_step(workflow, args.after, args.outcome, step))
        )
        return 0
    if args.action == "template":
        from sdd_workflows.templates import interview, main_flow

        atomic_write(
            args.output, workflow_json(main_flow() if args.name == "main-flow" else interview())
        )
        return 0
    if args.action == "validate":
        flow = workflow_load(args.file.read_text(encoding="utf-8"))
        if args.config:
            from sdd_core.profiles import resolve_profiles

            from sdd_runtime.profiles import load_profiles

            registry(args.config)
            flow = resolve_profiles(flow, load_profiles(args.config).profiles)
        validate(flow, tuple(args.mandatory))
        print("valid")
        return 0
    if args.action == "catalog":
        print(canonical([asdict(item) for item in registry(args.config).manifests()]))
        return 0
    if args.action in ("supervise", "start"):
        return supervise(args)
    if args.action == "run" and args.gated:
        watch_parent(args.parent_pid)
        if sys.stdin.readline().strip() != "GO":
            return 2
    engine = Engine(Store(args.database))
    if args.action == "policy":
        with engine.store.unit() as db:
            db.set_policy(str(args.workspace.resolve(strict=True)), tuple(args.mandatory))
    elif args.action == "export":
        atomic_write(args.output, workflow_json(engine.store.workflow(args.definition)))
    elif args.action == "publish":
        flow = workflow_load(args.file.read_text(encoding="utf-8"))
        if args.config:
            from sdd_core.profiles import resolve_profiles

            from sdd_runtime.profiles import load_profiles

            registry(args.config)
            flow = resolve_profiles(flow, load_profiles(args.config).profiles)
        print(engine.store.publish(flow, tuple(args.mandatory)))
    elif args.action == "create":
        if args.config:
            from sdd_core.profiles import resolve_profiles

            from sdd_runtime.profiles import load_profiles

            args.definition = engine.store.publish(
                resolve_profiles(
                    engine.store.workflow(args.definition), load_profiles(args.config).profiles
                )
            )
        state = engine.create(
            args.id or uuid.uuid4().hex,
            args.definition,
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
        print(canonical(asdict(state)))
    elif args.action in ("pause", "resume", "retry", "stop"):
        print(
            canonical(
                asdict(
                    engine.command(
                        args.id,
                        args.action,
                        args.request_id or uuid.uuid4().hex,
                        args.version,
                        time.time(),
                    )
                )
            )
        )
    elif args.action == "status":
        print(json.dumps(asdict(engine.store.get(args.id)), indent=2))
    elif args.action == "events":
        print(canonical(engine.store.history(args.id)))
    elif args.action == "replay":
        print(canonical(asdict(engine.store.replay(args.id))))
    elif args.action == "backup":
        engine.store.backup(args.target)
    elif args.action == "answer":
        with Lease(args.database.with_suffix(".coordinator.lock")):
            Coordinator(engine, Registry()).answer(args.id, args.outcome, args.text, time.time())
    elif args.action == "run":
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
