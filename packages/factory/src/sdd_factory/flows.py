"""Workflow templates, validation and publication for the operator UI.

A task is created from an intent ("feature", "main-flow", "ticket") rather
than from a digest the operator had to publish by hand: the template is built
for the project (its checks, lanes and language), validated against the
installed handlers and published. Publication is content-addressed, so the same
template for the same project always yields the same immutable version.
"""

import sys
from collections.abc import Callable
from pathlib import Path

from sdd_core.codec import canonical, encode, text, workflow_load
from sdd_core.graph import validate
from sdd_core.models import PROCESS_KINDS, Json, Workflow
from sdd_core.profiles import resolve_profiles
from sdd_core.sdk import Registry, handler_key
from sdd_runtime.engine import Engine
from sdd_runtime.profiles import load_profiles
from sdd_workflows.templates import (
    approved_feature,
    capabilities,
    command_demo,
    draft,
    feature,
    interview,
    localized,
    main_flow,
    plan_review,
    ticket,
    with_checks,
    with_tools,
)

from sdd_factory.model import INTENTS
from sdd_factory.settings import ProjectSettings

TEMPLATES = (*INTENTS, "approved-feature", "interview", "demo", "plan-review")


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
        # One builder per template: a new template is a new row.
        self.builders: dict[str, Callable[[ProjectSettings, tuple[str, ...]], Workflow]] = {
            "draft": lambda settings, repositories: draft(settings.roles),
            "feature": self._feature,
            "main-flow": lambda settings, repositories: main_flow(settings.roles),
            "ticket": self._ticket,
            "approved-feature": lambda settings, repositories: approved_feature(settings.roles),
            "interview": lambda settings, repositories: interview(),
            "demo": lambda settings, repositories: command_demo(sys.executable),
            "plan-review": lambda settings, repositories: plan_review(settings.roles),
        }

    def template(
        self,
        name: str,
        project_id: str = "",
        language: str = "ru",
        repositories: tuple[str, ...] = (),
    ) -> Workflow:
        """The project's version of a template. A ticket owning `repositories` runs the
        checks configured for them; otherwise the project's checks."""
        if name not in self.builders:
            raise ValueError("Unknown template")
        settings = ProjectSettings.of(self.project(project_id))
        flow = localized(self.builders[name](settings, repositories), language)
        return flow if name == "demo" else with_checks(flow, list(settings.checks))

    def readiness(self) -> dict[str, list[str]]:
        """Agent profiles each task intent needs but the registry lacks."""
        known = {manifest.id for manifest in self.handlers().manifests()}
        missing: dict[str, list[str]] = {}
        for intent in INTENTS:
            needed = {
                handler_key(step)
                for step in self.template(intent).steps
                if step.kind in PROCESS_KINDS
            }
            missing[intent] = sorted(needed - known)
        return missing

    def check(self, document: Json, publish: bool) -> dict[str, object]:
        flow = workflow_load(canonical(document))
        return {"workflow": encode(flow), "digest": self._verified(flow, publish)}

    def _ticket(self, settings: ProjectSettings, repositories: tuple[str, ...]) -> Workflow:
        """The project's ticket flow; `web` lets its agents use the internet."""
        flow = ticket(
            settings.ticket_checks(repositories),
            settings.roles,
            isolated=settings.isolation,
            auto_resolve=settings.auto_resolve,
            commit_messages=(settings.commit_message, settings.pin_message),
        )
        return with_tools(flow, ("web",) if settings.web else ())

    def _feature(self, settings: ProjectSettings, repositories: tuple[str, ...]) -> Workflow:
        # The planner learns what the project's ticket agents can and cannot do.
        agents = capabilities(self._ticket(settings, repositories))
        return feature(settings.roles, agents)

    def agents(self, project_id: str) -> str:
        """What the project's ticket agents can do, for planning and review briefs."""
        return capabilities(self._ticket(ProjectSettings.of(self.project(project_id)), ()))

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
                argv = step.options.argv
                if not argv or not Path(text(argv[0], "executable")).is_absolute():
                    raise ValueError(
                        "Configure an absolute check executable in the project or step"
                    )
            if step.kind in PROCESS_KINDS:
                try:
                    manifest = handlers.get(handler_key(step)).manifest
                except KeyError:
                    raise ValueError(
                        f"Step {step.id} needs the agent profile '{handler_key(step)}'; "
                        "connect it under Agents"
                    ) from None
                if step.kind not in manifest.capabilities:
                    raise ValueError(f"Incompatible handler on {step.id}")
                missing = sorted(set(step.options.tools) - set(manifest.capabilities))
                if missing:
                    raise ValueError(
                        f"Step {step.id}: agent '{handler_key(step)}' cannot use {', '.join(missing)}"
                    )
        return self.engine.store.publish(flow) if publish else None
