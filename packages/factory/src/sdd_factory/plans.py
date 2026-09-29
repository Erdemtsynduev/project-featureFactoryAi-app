"""Plans are sources of features: one plan becomes one feature run.

Every feature takes the same path: a specification (a PRD, with questions when a
decision is missing), a breakdown into tickets (vertical slices), the operator's
approval, then its tickets run. A work item from the project's `WorkSource` (by
default numbered Markdown files in its plans folder; a tracker such as Linear when
the project names one) only supplies a feature's scope: its open and partial rows.
The specification and the tickets belong to the factory.

`sync` creates one paused feature `feature_<NNN>` per plan whose open rows are not
covered yet; rows added to the file later (research adds rows) become a follow-up
feature `feature_<NNN>_<n>`. `rebuild` backs the database up, removes never-started
runs of the earlier per-row import and syncs. Nothing here edits plan files or
starts work.
"""

import re
import time
from collections.abc import Callable
from pathlib import Path

from sdd_core import machine
from sdd_core.models import Json, Run
from sdd_core.tracking import WorkItem, WorkRow, WorkSource
from sdd_runtime.engine import Engine

from sdd_factory.catalog import ProjectCatalog
from sdd_factory.flows import FlowLibrary
from sdd_factory.journal import FlightLog
from sdd_factory.model import TaskRecord, language_rule
from sdd_factory.sources.markdown import MarkdownPlans, row_run_id

# Sections of a per-row brief that carry recorded work worth keeping in the feature.
RECORDED = re.compile(
    r"^(Recorded acceptance draft|Constraints|Verification|Out of scope|Recorded decisions):",
    re.M,
)
RECORDED_CHARS = 1500
# Claim of a planning run whose item lives in a tracker, not in a workspace folder: a
# path nothing writes, so planning never waits for tickets or blocks them.
TRACKER_SCOPE = ".sdd-tracker-items"
BODY_CHARS = 6000


class PlanService:
    def __init__(
        self,
        engine: Engine,
        catalog: ProjectCatalog,
        flows: FlowLibrary,
        log: FlightLog,
        sources: Callable[[dict[str, Json]], WorkSource] | None = None,
    ) -> None:
        """`sources(project)` chooses where a project's work items come from; by default
        the numbered Markdown files in the folder the project names."""
        self.engine, self.catalog, self.flows, self.log = engine, catalog, flows, log
        self.sources = sources or (
            lambda project: MarkdownPlans(str(project.get("plans_folder", "")))
        )

    def _project(self, doc: dict[str, Json]) -> tuple[str, dict[str, Json], Path]:
        project_id = str(doc.get("project", ""))
        project = self.catalog.project(project_id)
        if not project:
            raise ValueError("Unknown project")
        return project_id, project, Path(str(project["workspace"])).resolve(strict=True)

    # Syncing -------------------------------------------------------------------

    def sync(self, doc: dict[str, Json]) -> dict[str, object]:
        """One feature per plan for open rows that no feature or queued ticket covers yet."""
        return self._sync(doc, {})

    def _sync(self, doc: dict[str, Json], carried: dict[str, str]) -> dict[str, object]:
        """`carried` holds briefs of per-row requirements removed just before."""
        project_id, project, workspace = self._project(doc)
        language = str(project.get("language", "ru"))
        only = str(doc.get("plan", ""))
        plans = [
            plan
            for plan in self.sources(project).items(str(workspace))
            if not only or plan.key == only
        ]
        if only and not plans:
            raise ValueError(f"No plan {only} in the project's plan source")
        records = self.catalog.tasks()
        with self.engine.store.unit() as unit:
            runs = {run.id: run for run in unit.runs()}
            contexts = carried | {
                key: unit.context(key)
                for key, item in records.items()
                if key in runs and item.kind == "feature" and not item.rows
            }
        features = {
            key: item
            for key, item in records.items()
            if key in runs and item.kind == "feature" and item.rows
        }
        flow = ""
        created: list[Json] = []
        for plan in plans:
            covered = self._covered(plan, features, records, runs)
            scope = [row for row in plan.rows if row.open and row.id not in covered]
            if not scope:
                continue
            previous = [key for key, item in features.items() if item.plan == plan.key]
            identifier = f"feature_{plan.key}" + (f"_{len(previous) + 1}" if previous else "")
            flow = flow or self.flows.ensure("feature", project_id, language)
            queued = self._queued(plan, records, runs)
            recorded = {
                row.id: recorded_work(contexts.get(row_run_id(plan.key, row), "")) for row in scope
            }
            self.engine.create(
                identifier,
                flow,
                workspace,
                language_rule(language) + feature_brief(plan, scope, queued, recorded),
                None,
                time.time(),
                (),
                (str(Path(plan.path).parent),) if plan.path else (TRACKER_SCOPE,),
            )
            self.catalog.save_task(
                identifier,
                TaskRecord(
                    project=project_id,
                    title=plan.title[:200],
                    kind="feature",
                    language=language,
                    plan=plan.key,
                    rows=tuple(row.id for row in scope),
                    source=plan.path or plan.url,
                    link=plan.link,
                ),
            )
            created.append(identifier)
        self.catalog.save_plans(
            project_id,
            [
                {
                    "id": plan.key,
                    "title": plan.title,
                    "path": plan.path,
                    "url": plan.url,
                    **plan.counts(),
                }
                for plan in plans
            ],
        )
        self.log.record(
            "plans_synced", project=project_id, plan=only, plans=len(plans), created=len(created)
        )
        return {"plans": len(plans), "created": created}

    @staticmethod
    def _covered(
        plan: WorkItem,
        features: dict[str, TaskRecord],
        records: dict[str, TaskRecord],
        runs: dict[str, Run],
    ) -> set[str]:
        """Rows already in a feature's scope or decomposed into queued legacy tickets."""
        covered = {row for item in features.values() if item.plan == plan.key for row in item.rows}
        parents = {
            item.parent
            for key, item in records.items()
            if key in runs and item.kind == "ticket" and item.legacy_id
        }
        covered |= {row.id for row in plan.rows if row_run_id(plan.key, row) in parents}
        return covered

    @staticmethod
    def _queued(plan: WorkItem, records: dict[str, TaskRecord], runs: dict[str, Run]) -> list[str]:
        return [
            f"{key} — {item.title}"
            for key, item in records.items()
            if key in runs
            and item.plan == plan.key
            and item.kind == "ticket"
            and runs[key].status != "accepted"
        ]

    # Rebuilding the board ----------------------------------------------------------

    def rebuild(self, doc: dict[str, Json]) -> dict[str, object]:
        """Back up, drop never-started runs of the per-row import, then create features.

        Started runs, legacy tickets with their contracts, features and anything a
        kept run depends on stay untouched.
        """
        project_id, project, workspace = self._project(doc)
        self.sources(project).items(str(workspace))  # refuse before changing anything
        records = self.catalog.tasks()
        with self.engine.store.unit() as unit:
            runs = {run.id: run for run in unit.runs()}
            edges = unit.dependency_edges()
        candidates = {
            key
            for key, item in records.items()
            if item.project == project_id
            and key in runs
            and machine.discardable(runs[key])
            # Per-row requirements of the old import; features carry their rows.
            and item.kind == "feature"
            and not item.rows
        }
        while True:
            kept_needs = {needed for key, needed in edges if key not in candidates}
            if not candidates & kept_needs:
                break
            candidates -= kept_needs
        path = self.engine.store.path
        backup = path.with_name(f"{path.stem}.before-plan-rebuild-{int(time.time())}.db")
        self.engine.store.backup(backup)
        # Recorded drafts of removed per-row requirements carry over into the features.
        with self.engine.store.unit() as unit:
            carried = {key: unit.context(key) for key in candidates}
        removed = self.engine.store.discard(tuple(sorted(candidates)))
        self.log.record(
            "board_rebuilt", project=project_id, removed=len(removed), backup=str(backup)
        )
        return {"removed": len(removed), "backup": str(backup), **self._sync(doc, carried)}


# Briefs ----------------------------------------------------------------------------


def recorded_work(context: str) -> str:
    """Specification drafts and decisions a per-row requirement had recorded."""
    match = RECORDED.search(context)
    return context[match.start() :][:RECORDED_CHARS].strip() if match else ""


def feature_brief(
    plan: WorkItem, scope: list[WorkRow], queued: list[str], recorded: dict[str, str]
) -> str:
    if plan.path:
        lines = [
            f"Feature: plan {plan.title}.",
            f"Source: {plan.path}. Read it: its context, rules (for example research first)"
            " and every row. Rows marked [x] or [-] are context, not scope.",
        ]
    else:
        # A tracker item is not in the workspace: its description travels in the brief.
        lines = [
            f"Feature: {plan.title}.",
            f"Source: {plan.url or plan.link} (tracker). Its description, rules and findings:",
            plan.body[:BODY_CHARS],
            "Rows marked [x] or [-] are context, not scope.",
        ]
    lines += ["", "Scope: these open rows of the plan (id, mark, text):"]
    for row in scope:
        mark = "partial - continue the recorded work, do not restart" if row.mark == "~" else "open"
        # Wrapped rows continue on indented lines; evidence notes stay in the file.
        text = " ".join([row.text, *row.detail])
        lines.append(f"- {row.id} ({mark}): {text[:400]}")
    if queued:
        lines += ["", "Already queued tickets of this plan (do not duplicate them):"]
        lines += [f"- {item[:200]}" for item in queued]
    drafts = {key: value for key, value in recorded.items() if value}
    if drafts:
        lines += ["", "Recorded specification drafts and decisions (reuse, do not weaken):"]
        for key, value in drafts.items():
            lines += [f"### {key}", value]
    return "\n".join(lines)
