"""Bounded attempt packets and native session continuation after human answers."""

from pathlib import Path

from sdd_core.codec import object_json, result_load
from sdd_core.memory import brief, fit
from sdd_core.models import Step
from sdd_core.ports import StateStore
from sdd_core.sdk import Packet, Registry, handler_key

from sdd_runtime.files import attempt_folder
from sdd_runtime.lanes import load_lane

# Receipts scanned for task memory (notes and handoff), and when looking for a session.
MEMORY_WINDOW = 12
SESSION_WINDOW = 12


def build_packet(store: StateStore, registry: Registry, run_id: str) -> Packet:
    run = store.get(run_id)
    if run.active is None:
        raise ValueError("No dispatched attempt")
    workflow = store.workflow(run.workflow_digest)
    with store.unit() as db:
        root = Path(db.location(run_id).workspace)
        context = db.context(run_id)
        results = db.recent_results(run_id, MEMORY_WINDOW)
    folder = attempt_folder(root, run.id, run.active.id)
    folder.mkdir(parents=True, exist_ok=True)
    if len(context) > workflow.max_input_chars:
        raise ValueError("Required context exceeds budget")
    if results:
        # A compact brief instead of raw receipts; the full previous receipt stays linked.
        link = "Previous receipt: " + str(
            folder.parent / str(run.previous_attempt) / "receipt.json"
        )
        memory = brief(results).render() + "\n" + link
        context = fit(context, memory, workflow.max_input_chars)
    step = workflow.step(run.step)
    done = delivered(store, run_id)
    resume = continuation(store, registry, run.id, run.revision, step, context)
    if resume is not None:
        return Packet(run.id, run.active, step, str(root), str(folder), resume[1], resume[0], done)
    return Packet(run.id, run.active, step, str(root), str(folder), context, delivered=done)


def delivered(store: StateStore, run_id: str) -> tuple[str, ...]:
    """Repositories the run's accepted prerequisites changed, from their lanes."""
    found: set[str] = set()
    with store.unit() as db:
        for prerequisite in db.dependencies(run_id):
            document = db.lane(prerequisite.id) if prerequisite.status == "accepted" else None
            if document:
                found.update(repo.path for repo in load_lane(document).repos if repo.path != ".")
    return tuple(sorted(found))


def continuation(
    store: StateStore, registry: Registry, run_id: str, revision: str, step: Step, context: str
) -> tuple[str, str] | None:
    """Continue the step's own CLI session when only human answers came since.

    The session is reused only for the same step and handler, an unchanged
    workspace revision and a handler that declares `resume`. The new packet
    then carries just the answers, so the model does not re-read the task.
    """
    if step.kind != "agent":
        return None
    try:
        manifest = registry.get(handler_key(step)).manifest
    except KeyError:
        return None
    if "resume" not in manifest.capabilities:
        return None
    with store.unit() as db:
        documents = db.recent_results(run_id, SESSION_WINDOW)
    answers: list[str] = []
    for document in documents:
        result = result_load(document)
        data = object_json(result.data)
        if "answer" in data:
            answers.append(result.reason)
            continue
        session = data.get("session")
        if (
            not answers
            or not isinstance(session, dict)
            or session.get("step") != step.id
            or session.get("handler") != manifest.id
            or not isinstance(session.get("id"), str)
            or result.revision != revision
        ):
            return None
        guidance = context.split("\nOperator guidance:\n", 1)
        delta = "Answers to your questions (oldest first):\n\n" + "\n\n".join(reversed(answers))
        if len(guidance) == 2:
            delta += "\n\nOperator guidance:\n" + guidance[1]
        return str(session["id"]), delta
    return None
