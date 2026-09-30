"""Plans are sources of features: one plan becomes one feature run.

Every feature takes the same path: a specification (a PRD, with questions when a
decision is missing), a breakdown into tickets (vertical slices), the operator's
approval, then its tickets run. A work item from the project's `WorkSource` (by
default numbered Markdown files in its plans folder; a tracker such as Linear when
the project names one) only supplies a feature's scope: its open and partial rows.
The specification and the tickets belong to the factory.

`sync` creates one paused feature `feature_<NNN>` per plan whose open rows are not
covered yet; rows added to the file later (research adds rows) become a follow-up
feature `feature_<NNN>_<n>`. `rebuild` backs the database up and plans again
everything that never started: never-started features and tickets are removed, a
feature whose tickets were removed is closed, and each plan's rows that no kept work
covers become one fresh feature, planned under the current rules. Nothing here edits
plan files or starts work.
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
from sdd_factory.model import TaskRecord, language_rule, unfinished
from sdd_factory.sources.markdown import MarkdownPlans, row_run_id

# Sections of a per-row brief that carry recorded work worth keeping in the feature.
RECORDED = re.compile(
    r"^(Recorded acceptance draft|Constraints|Verification|Out of scope|Recorded decisions"
    r"|Requirement):",
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
        return self._sync(doc, {}, frozenset())

    def _sync(
        self, doc: dict[str, Json], carried: dict[str, str], reopened: frozenset[str]
    ) -> dict[str, object]:
        """`carried` holds recorded work of removed runs by the row run they belong to;
        the rows of `reopened` features are planned again."""
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
            contexts = {
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
            covering = {key: item for key, item in features.items() if key not in reopened}
            covered = self._covered(plan, covering, records, runs)
            scope = [row for row in plan.rows if row.open and row.id not in covered]
            if not scope:
                continue
            previous = [key for key, item in features.items() if item.plan == plan.key]
            identifier = f"feature_{plan.key}" + (f"_{len(previous) + 1}" if previous else "")
            flow = flow or self.flows.ensure("feature", project_id, language)
            queued = self._queued(plan, records, runs)
            recorded = {
                row.id: carried.get(key) or recorded_work(contexts.get(key, ""))
                for row in scope
                if (key := row_run_id(plan.key, row))
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
            f"{key} — {item.title} ({runs[key].status})"
            for key, item in records.items()
            if key in runs
            and item.plan == plan.key
            and item.kind == "ticket"
            and runs[key].status != "accepted"
        ]

    # Rebuilding the board ----------------------------------------------------------

    def rebuild(self, doc: dict[str, Json]) -> dict[str, object]:
        """Back up, then plan again all work of the project (or of `plan`) that never started.

        Never-started features and tickets are removed; what a removed per-row
        requirement or imported ticket recorded (its requirement, acceptance and
        decisions) carries over into the new feature. A feature whose tickets were
        removed is closed: what it delivered stays, its started tickets keep running
        as top-level work, and its rows are planned again. Started work, tasks, plan
        reviews and anything a kept run depends on stay untouched.
        """
        project_id, project, workspace = self._project(doc)
        only = str(doc.get("plan", ""))
        # Refuse before changing anything: the plans must read and features must be creatable.
        self.sources(project).items(str(workspace))
        self.flows.ensure("feature", project_id, str(project.get("language", "ru")))
        with self.engine.store.unit() as unit:
            runs = {run.id: run for run in unit.runs()}
            edges = unit.dependency_edges()
        records = {key: item for key, item in self.catalog.tasks().items() if key in runs}

        def planned(item: TaskRecord) -> bool:
            """A plan's feature, an imported ticket, or a ticket of an open plan feature."""
            if item.kind == "feature" or item.legacy_id:
                return True
            parent = records.get(item.parent)
            return item.kind == "ticket" and parent is not None and bool(parent.rows)

        candidates = {
            key
            for key, item in records.items()
            if item.project == project_id
            and item.plan
            and (not only or item.plan == only)
            and planned(item)
            and not item.reviews
            and not records.get(item.parent, item).closed
            and machine.discardable(runs[key])
        }
        while True:
            kept_needs = {needed for key, needed in edges if key not in candidates}
            if not candidates & kept_needs:
                break
            candidates -= kept_needs
        reopened = frozenset(
            item.parent
            for key, item in records.items()
            if key in candidates
            and item.kind == "ticket"
            and not item.legacy_id
            and item.parent not in candidates
        )
        path = self.engine.store.path
        backup = path.with_name(f"{path.stem}.before-plan-rebuild-{time.time_ns()}.db")
        self.engine.store.backup(backup)
        carried: dict[str, str] = {}
        with self.engine.store.unit() as unit:
            for key in sorted(candidates):
                item = records[key]
                # Imported tickets belong to their row's requirement; features to themselves.
                owner = item.parent if item.legacy_id else key
                if item.legacy_id or not item.rows:
                    work = recorded_work(unit.context(key))
                    carried[owner] = "\n\n".join(filter(None, (carried.get(owner, ""), work)))
        removed = self.engine.store.discard(tuple(sorted(candidates)))
        status = {key: run.status for key, run in runs.items() if key not in candidates}
        kept = {key: item for key, item in records.items() if key not in candidates}
        for feature in sorted(reopened):
            self.catalog.close_task(feature, unfinished(feature, kept, status))
        self.log.record(
            "board_rebuilt",
            project=project_id,
            plan=only,
            removed=len(removed),
            reopened=list[Json](sorted(reopened)),
            backup=str(backup),
        )
        return {
            "removed": len(removed),
            "reopened": sorted(reopened),
            "backup": str(backup),
            **self._sync(doc, carried, reopened),
        }


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
