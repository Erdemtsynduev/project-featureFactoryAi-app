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

import time
from collections.abc import Callable
from pathlib import Path

from sdd_core.models import Json, Run
from sdd_core.tracking import WorkItem, WorkRow, WorkSource
from sdd_runtime.engine import Engine
from sdd_runtime.lane_model import lane_branch

from sdd_factory.board import replan
from sdd_factory.catalog import ProjectCatalog
from sdd_factory.flows import FlowLibrary
from sdd_factory.journal import FlightLog
from sdd_factory.model import PLANNING_SCOPE, TaskRecord, language_rule, unfinished
from sdd_factory.sources.markdown import MarkdownPlans

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
        """One feature per plan for open rows that no feature covers yet."""
        return self._sync(doc, frozenset())

    def _sync(self, doc: dict[str, Json], reopened: frozenset[str]) -> dict[str, object]:
        """The rows of `reopened` features are planned again."""
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
        features = {
            key: item
            for key, item in records.items()
            if key in runs and item.kind == "feature" and item.rows
        }
        flow = ""
        created: list[Json] = []
        for plan in plans:
            covering = {key: item for key, item in features.items() if key not in reopened}
            covered = {
                row for item in covering.values() if item.plan == plan.key for row in item.rows
            }
            scope = [row for row in plan.rows if row.open and row.id not in covered]
            if not scope:
                continue
            previous = [key for key, item in features.items() if item.plan == plan.key]
            identifier = f"feature_{plan.key}" + (f"_{len(previous) + 1}" if previous else "")
            flow = flow or self.flows.ensure("feature", project_id, language)
            queued, delivered, dropped = self._tickets(plan, records, runs)
            self.engine.create(
                identifier,
                flow,
                workspace,
                language_rule(language) + feature_brief(plan, scope, queued, delivered, dropped),
                None,
                time.time(),
                (),
                # A tracker item is no workspace folder: its planning owns none.
                (str(Path(plan.path).parent),) if plan.path else (PLANNING_SCOPE,),
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
            "plans_synced",
            project=project_id,
            plan=only,
            plans=len(plans),
            created=len(created),
        )
        return {"plans": len(plans), "created": created}

    @staticmethod
    def _tickets(
        plan: WorkItem, records: dict[str, TaskRecord], runs: dict[str, Run]
    ) -> tuple[list[str], list[str], list[str]]:
        """The plan's tickets still queued (with their state), those delivered, and those
        superseded (their scope is planned again; their lane branch keeps their work)."""
        queued: list[str] = []
        delivered: list[str] = []
        superseded: list[str] = []
        for key, item in records.items():
            if key in runs and item.plan == plan.key and item.kind == "ticket":
                status = runs[key].status
                if status == "accepted":
                    delivered.append(f"{key} — {item.title}")
                elif item.superseded:
                    superseded.append(f"{key} — {item.title} (branch {lane_branch(key)})")
                else:
                    queued.append(f"{key} — {item.title} ({status})")
        return queued, delivered, superseded

    # Rebuilding the board ----------------------------------------------------------

    def rebuild(self, doc: dict[str, Json]) -> dict[str, object]:
        """Back up, then plan again all work of the project (or of `plan`) that never started.

        Never-started features and tickets are removed. A feature whose tickets were
        removed is closed: what it delivered stays, its started tickets keep running
        as top-level work, its never-started plan reviews go, and its rows are planned
        again. Other started work, tasks, reviews and anything a kept run depends on stay
        untouched.
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
        decided = replan(records, runs, edges, project_id, only)
        candidates, reopened = decided.removed, decided.reopened
        path = self.engine.store.path
        backup = path.with_name(f"{path.stem}.before-plan-rebuild-{time.time_ns()}.db")
        self.engine.store.backup(backup)
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
            **self._sync(doc, reopened),
        }


# Briefs ----------------------------------------------------------------------------


def feature_brief(
    plan: WorkItem,
    scope: list[WorkRow],
    queued: list[str],
    delivered: list[str] | None = None,
    superseded: list[str] | None = None,
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
        lines += [
            "",
            "Already queued tickets of this plan (do not duplicate them; a new ticket that"
            " waits for one names its run id in `after`):",
        ]
        lines += [f"- {item[:200]}" for item in queued]
    if delivered:
        lines += ["", "Delivered tickets of this plan (done; build on them, do not redo them):"]
        lines += [f"- {item[:200]}" for item in delivered]
    if superseded:
        lines += [
            "",
            "Superseded tickets of this plan (plan their scope again, one repository per"
            " ticket; reuse the partial work on their lane branch):",
        ]
        lines += [f"- {item[:240]}" for item in superseded]
    return "\n".join(lines)
