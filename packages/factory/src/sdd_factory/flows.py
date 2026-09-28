"""Workflow templates, validation and publication for the operator UI.

A task is created from an intent ("feature", "main-flow", "ticket") rather
than from a digest the operator had to publish by hand: the template is built
for the project (its checks, lanes and language), validated against the
installed handlers and published. Publication is content-addressed, so the same
template for the same project always yields the same immutable version.
"""

import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from sdd_core.codec import canonical, mapping, sequence, text, workflow_load
from sdd_core.graph import validate
from sdd_core.models import Json, Workflow
from sdd_core.options import StepOptions
from sdd_core.profiles import resolve_profiles
from sdd_core.sdk import Registry, handler_key
from sdd_runtime.engine import Engine
from sdd_runtime.profiles import load_profiles
from sdd_workflows.templates import (
    CheckCommand,
    approved_feature,
    command_demo,
    feature,
    interview,
    localized,
    main_flow,
    ticket,
    with_checks,
)

from sdd_factory.model import INTENTS

TEMPLATES = (*INTENTS, "approved-feature", "interview", "demo")


class FlowLibrary:
    def __init__(
        self,
        engine: Engine,
        config: Path,
        handlers: Callable[[], Registry],
        project: Callable[[str], dict[str, Json]],
    ) -> None:
        self.engine, self.config = engine, config
        self.handlers, self.project = handlers, project

    def template(
        self,
        name: str,
        project_id: str = "",
        language: str = "ru",
        repositories: tuple[str, ...] = (),
    ) -> Workflow:
        """The project's version of a template. A ticket owning `repositories` runs the
        checks configured for them; otherwise the project's checks."""
        if name not in TEMPLATES:
            raise ValueError("Unknown template")
        project = self.project(project_id)
        checks = [text(x, "check") for x in sequence(project.get("checks", []))]
        if name == "demo":
            flow = command_demo(sys.executable)
        elif name == "ticket":
            flow = ticket(
                self._ticket_checks(project, checks, repositories),
                isolated=project.get("isolation", True) is not False,
                auto_resolve=project.get("auto_resolve", True) is not False,
            )
        else:
            flow = {
                "feature": feature,
                "approved-feature": approved_feature,
                "main-flow": main_flow,
                "interview": interview,
            }[name]()
        flow = localized(flow, language)
        return flow if name == "demo" else with_checks(flow, checks)

    def readiness(self) -> dict[str, list[str]]:
        """Agent profiles each task intent needs but the registry lacks."""
        known = {manifest.id for manifest in self.handlers().manifests()}
        missing: dict[str, list[str]] = {}
        for intent in INTENTS:
            needed = {
                handler_key(step)
                for step in self.template(intent).steps
                if step.kind in ("agent", "check", "operation")
            }
            missing[intent] = sorted(needed - known)
        return missing

    def check(self, document: Json, publish: bool) -> dict[str, object]:
        flow = workflow_load(canonical(document))
        return {"workflow": asdict(flow), "digest": self._verified(flow, publish)}

    @staticmethod
    def _ticket_checks(
        project: dict[str, Json], checks: list[str], repositories: tuple[str, ...]
    ) -> tuple[CheckCommand, ...]:
        configured = mapping(project.get("repository_checks", {}))
        owned = [
            CheckCommand(
                tuple(text(x, "check") for x in sequence(configured[repository])),
                repository,
                title=f"checks · {repository}",
            )
            for repository in repositories
            if sequence(configured.get(repository, []))
        ]
        if owned:
            return tuple(owned)
        return (CheckCommand(tuple(checks), title="checks"),) if checks else ()

    def ensure(
        self, name: str, project_id: str, language: str, repositories: tuple[str, ...] = ()
    ) -> str:
        """Digest of the project's version of a template, published on first use."""
        digest = self._verified(self.template(name, project_id, language, repositories), True)
        assert digest is not None
        return digest

    def _verified(self, flow: Workflow, publish: bool) -> str | None:
        if self.config.exists():
            flow = resolve_profiles(flow, load_profiles(self.config).profiles)
        validate(flow)
        handlers = self.handlers()
        for step in flow.steps:
            if step.kind == "check" and step.handler == "command":
                argv = StepOptions.parse(step.config).argv
                if not argv or not Path(text(argv[0], "executable")).is_absolute():
                    raise ValueError(
                        "Configure an absolute check executable in the project or step"
                    )
            if step.kind in ("agent", "check", "operation"):
                try:
                    manifest = handlers.get(handler_key(step)).manifest
                except KeyError:
                    raise ValueError(
                        f"Step {step.id} needs the agent profile '{handler_key(step)}'; "
                        "connect it under Agents"
                    ) from None
                if step.kind not in manifest.capabilities:
                    raise ValueError(f"Incompatible handler on {step.id}")
        return self.engine.store.publish(flow) if publish else None
