"""Creating work: a draft, a feature, a ticket or a task from a person's request, and its title."""

import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from sdd_core.codec import encode, sequence, text
from sdd_core.models import Json
from sdd_core.tracking import WorkItem
from sdd_runtime.engine import Engine
from sdd_runtime.files import revision
from sdd_workflows.templates import question_example

from sdd_factory.briefs import NO_WORK, draft_brief
from sdd_factory.catalog import Artifact, ProjectCatalog
from sdd_factory.flows import FlowLibrary
from sdd_factory.journal import FlightLog
from sdd_factory.model import (
    INTENT_KIND,
    INTENTS,
    LANGUAGES,
    PLANNING_KINDS,
    PLANNING_SCOPE,
    TaskRecord,
    language_rule,
)

CYRILLIC = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        "a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch - y - e yu ya".split(),
        strict=True,
    )
)


def slug(title: str, suffix: str) -> str:
    """A readable run id from a title (Cyrillic is transliterated) plus a unique suffix."""
    latin = "".join(CYRILLIC.get(char, char) for char in title.lower())
    base = re.sub(r"[^a-z0-9]+", "-", latin).strip("-")[:48].strip("-") or "task"
    return f"{base}-{suffix}"


class WorkCreation:
    def __init__(
        self,
        engine: Engine,
        catalog: ProjectCatalog,
        flows: FlowLibrary,
        log: FlightLog,
        bind: Callable[[str], None],
    ) -> None:
        self.engine = engine
        self.catalog = catalog
        self.flows = flows
        self.log = log
        self.bind = bind

    def create(self, doc: dict[str, Json]) -> dict[str, object]:
        project_id = text(doc.get("project", ""), "project")
        project = self.catalog.project(project_id)
        if project_id and not project:
            raise ValueError("Unknown project")
        workspace = Path(text(project.get("workspace", doc.get("workspace")), "workspace")).resolve(
            strict=True
        )
        if self.engine.store.path.is_relative_to(workspace):
            raise ValueError("Keep the UI database outside the task workspace")
        language = text(doc.get("language", project.get("language", "ru")), "language")
        if language not in LANGUAGES:
            raise ValueError("Unsupported response language")
        title = text(doc.get("title", ""), "title").strip()
        identifier = text(doc.get("id", ""), "id").strip() or slug(title, uuid.uuid4().hex[:4])
        intent = text(doc.get("intent", ""), "intent")
        if intent:
            if intent not in INTENTS:
                raise ValueError("Unknown task type")
            definition = self.flows.ensure(intent, project_id, language)
        else:
            definition = text(doc.get("definition"), "definition")
        kind = INTENT_KIND.get(intent, "task")
        planning = kind in PLANNING_KINDS
        words = text(doc.get("context", ""), "context")
        # A typed draft is a work item like any other: the lead cuts features from it.
        item = WorkItem(identifier, title or identifier, words, ()) if kind == "draft" else None
        if item is not None:
            words = draft_brief(item, NO_WORK, self.catalog.labels(project_id))
        run = self.engine.create(
            identifier,
            definition,
            workspace,
            language_rule(language) + words,
            # Work that only plans owns no folder: edits elsewhere never invalidate it.
            None if planning else revision(workspace),
            time.time(),
            tuple(text(x, "dependency") for x in sequence(doc.get("dependencies", []))),
            (PLANNING_SCOPE,) if planning else (),
        )
        self.catalog.save_task(
            run.id,
            TaskRecord(
                project=project_id,
                title=title or run.id,
                kind=kind,
                language=language,
                intent=intent,
            ),
        )
        if item is not None:
            self.catalog.save_artifact(Artifact(run.id, "draft", item.body, encode(item)))
        self.bind(run.id)
        self.log.record("task_created", run=run.id, intent=intent or "definition")
        return encode(run)

    def demo(self, doc: dict[str, Json]) -> dict[str, object]:
        """A human-only task in a sample folder: shows a question without a model."""
        language = text(doc.get("language", "ru"), "language")
        identifier = "question-" + uuid.uuid4().hex[:8]
        root = self.engine.store.path.parent / "question-examples" / identifier
        root.mkdir(parents=True)
        run = self.engine.create(
            identifier,
            self.engine.store.publish(question_example(language)),
            root,
            "Interactive question example; no model calls or project edits.",
            revision(root),
            time.time(),
        )
        self.engine.command(run.id, "resume", uuid.uuid4().hex, run.version, time.time())
        title = "Пример вопроса команды" if language == "ru" else "Team question example"
        self.catalog.save_task(run.id, TaskRecord(title=title, language=language))
        return encode(self.engine.dispatch(run.id, time.time(), uuid.uuid4().hex))

    def rename(self, doc: dict[str, Json]) -> dict[str, Json]:
        """Correct a task's title after creation."""
        identifier = text(doc.get("id"), "id")
        title = text(doc.get("title"), "title").strip()
        if not title:
            raise ValueError("A title is required")
        current = self.catalog.task(identifier)
        self.engine.store.get(identifier)
        record = current.changed(title=title[:200])
        self.catalog.update_task(identifier, record)
        self.log.record("task_renamed", run=identifier)
        return record.document()
