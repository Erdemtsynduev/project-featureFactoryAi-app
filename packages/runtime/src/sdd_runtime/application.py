"""Transactional orchestration service. UI and workers are command clients.

`ApplicationEngine` is a facade over focused use cases that share one `RunContext`:
`RunCommands` (lifecycle and operator commands), `Scheduler` (admission and
dispatch), `ResultIntake` (results and lost attempts) and `HumanAnswers` (what a
run asks and the answers it gets). Each depends only on core ports.
"""

from pathlib import Path

from sdd_core.admission import QueueBudget, Slots
from sdd_core.models import Cause, Result, Run, Workflow
from sdd_core.ports import StateStore, Workspace
from sdd_core.sdk import ProjectAdapter

from sdd_runtime.answers import HumanAnswers
from sdd_runtime.commands import RunCommands
from sdd_runtime.intake import ResultIntake
from sdd_runtime.runs import RunContext
from sdd_runtime.scheduling import Scheduler

__all__ = ["ApplicationEngine"]


class ApplicationEngine:
    def __init__(
        self,
        store: StateStore,
        project: ProjectAdapter,
        workspace: Workspace,
        max_queue_calls: int | None = None,
        max_queue_planning_calls: int | None = None,
        max_agents: int = 2,
        max_operations: int = 1,
    ) -> None:
        self.runs = RunContext(store, project, workspace)
        self.scheduler = Scheduler(
            self.runs,
            Slots(max_agents, max_operations),
            QueueBudget(max_queue_calls, max_queue_planning_calls),
        )
        self.intake = ResultIntake(self.runs)
        self.answers = HumanAnswers(self.runs, self.intake)
        self.commands = RunCommands(self.runs)

    # Ports and settings ------------------------------------------------------

    @property
    def store(self) -> StateStore:
        return self.runs.store

    @property
    def project(self) -> ProjectAdapter:
        return self.runs.project

    @property
    def workspace(self) -> Workspace:
        return self.runs.workspace

    @property
    def budget(self) -> QueueBudget:
        """The optional queue-wide call cap (for agents billed per token)."""
        return self.scheduler.budget

    @budget.setter
    def budget(self, budget: QueueBudget) -> None:
        self.scheduler.budget = budget

    @property
    def max_queue_calls(self) -> int | None:
        return self.budget.calls

    @property
    def max_queue_planning_calls(self) -> int | None:
        return self.budget.planning_calls

    def workflow_of(self, run_id: str) -> Workflow:
        return self.runs.workflow_of(run_id)

    def revision_of(self, claim: str) -> str:
        return self.runs.revision_of(claim)

    def observe(self, run_id: str) -> str:
        """Current revision of everything the run owns."""
        return self.runs.observe(run_id)

    # Lifecycle and operator commands -----------------------------------------

    def create(
        self,
        identifier: str,
        definition: str,
        workspace: Path,
        context: str,
        revision: str | None,
        now: float,
        dependencies: tuple[str, ...] = (),
        scope: tuple[str, ...] = (),
    ) -> Run:
        """Create a paused run. `scope` limits ownership and revision to sub-folders."""
        return self.commands.create(
            identifier, definition, workspace, context, revision, now, dependencies, scope
        )

    def command(self, run_id: str, command: str, request_id: str, expected: int, now: float) -> Run:
        return self.commands.command(run_id, command, request_id, expected, now)

    def message(self, run_id: str, message: str, request_id: str, expected: int, now: float) -> Run:
        return self.commands.message(run_id, message, request_id, expected, now)

    def request_recovery(self, run_id: str, expected: int, now: float) -> Run:
        return self.commands.request_recovery(run_id, expected, now)

    def revise(
        self,
        run_id: str,
        context: str,
        claim: str,
        dependencies: tuple[str, ...],
        request_id: str,
        expected: int,
        now: float,
    ) -> Run:
        return self.commands.revise(run_id, context, claim, dependencies, request_id, expected, now)

    def relocate(self, run_id: str, workspace: str, claim: str, now: float) -> Run:
        return self.commands.relocate(run_id, workspace, claim, now)

    def invalidate(self, run_id: str, revision: str, now: float) -> Run:
        return self.commands.invalidate(run_id, revision, now)

    def rebase_revision(self, run_id: str, revision: str, now: float) -> Run:
        return self.commands.rebase_revision(run_id, revision, now)

    def block(self, run_id: str, now: float, reason: str, cause: Cause = "blocked") -> Run:
        return self.commands.block(run_id, now, reason, cause)

    # Scheduling ----------------------------------------------------------------

    def admissible(self, run_id: str) -> bool:
        return self.scheduler.admissible(run_id)

    def dispatch(self, run_id: str, now: float, attempt_id: str) -> Run:
        return self.scheduler.dispatch(run_id, now, attempt_id)

    # Results -------------------------------------------------------------------

    def complete(self, run_id: str, result: Result, now: float, expected: int | None = None) -> Run:
        return self.intake.complete(run_id, result, now, expected)

    def recover(
        self,
        run_id: str,
        now: float,
        confirmed: bool,
        reason: str,
        revision: str,
        launched: bool = True,
    ) -> Run:
        return self.intake.recover(run_id, now, confirmed, reason, revision, launched)

    def release_condition(self, run_id: str, now: float) -> Run:
        return self.intake.release_condition(run_id, now)

    # Human steps ---------------------------------------------------------------

    def facts(self, run_id: str) -> str:
        return self.answers.facts(run_id)

    def outputs(self, run_id: str, product: str) -> tuple[Result, ...]:
        return self.answers.outputs(run_id, product)

    def asked(self, run_id: str) -> str:
        return self.answers.asked(run_id)

    def answer(
        self,
        run_id: str,
        outcome: str,
        reply: str,
        choices: dict[str, str],
        expected: int,
        now: float,
    ) -> Run:
        return self.answers.answer(run_id, outcome, reply, choices, expected, now)

    def auto_answer(self, run_id: str, now: float) -> Run | None:
        return self.answers.auto_answer(run_id, now)
