"""Bounded attempt packets and native session continuation after human answers."""

from pathlib import Path

from sdd_core.codec import object_json, result_load
from sdd_core.models import Step
from sdd_core.ports import StateStore
from sdd_core.sdk import Packet, Registry, handler_key

from sdd_runtime.files import attempt_folder

# Receipts carried into a fresh packet, and scanned when looking for a session.
RECENT_RESULTS = 5
SESSION_WINDOW = 12


def build_packet(store: StateStore, registry: Registry, run_id: str) -> Packet:
    run = store.get(run_id)
    if run.active is None:
        raise ValueError("No dispatched attempt")
    workflow = store.workflow(run.workflow_digest)
    with store.unit() as db:
        root = Path(db.location(run_id)[0])
        context = db.context(run_id)
        results = db.recent_results(run_id, RECENT_RESULTS)
    folder = attempt_folder(root, run.id, run.active.id)
    folder.mkdir(parents=True, exist_ok=True)
    if results:
        remaining = workflow.max_input_chars - len(context)
        carry = "\nRecent results (newest first):\n" + "\n".join(results)
        if remaining < len(carry):
            carry = "\nPrevious result artifact: " + str(
                folder.parent / str(run.previous_attempt) / "receipt.json"
            )
        if len(context) + len(carry) > workflow.max_input_chars:
            raise ValueError("Required context exceeds budget")
        context += carry
    step = workflow.step(run.step)
    resume = continuation(store, registry, run.id, run.revision, step, context)
    if resume is not None:
        return Packet(run.id, run.active, step, str(root), str(folder), resume[1], resume[0])
    return Packet(run.id, run.active, step, str(root), str(folder), context)


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
