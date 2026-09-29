"""Deterministic simulation: seeded schedules of commands, results, crashes and faults.

The engine has no clock, randomness or IO of its own, so a seed fully determines a
schedule. Each schedule drives several runs through the real application services
over the memory store and checks the engine's invariants after every step. A
failure names its seed; rerunning that seed replays the same schedule exactly.
"""

import random
from dataclasses import dataclass, field, replace

import pytest
from sdd_core import machine
from sdd_core.models import PROCESS_KINDS, Artifact, Result, Run, Step, Workflow
from sdd_core.ports import Conflict
from sdd_core.sdk import Manifest
from sdd_runtime.application import ApplicationEngine
from sdd_storage.memory import MemoryStore

SEEDS = range(40)
STEPS = 300
MAX_QUEUE_CALLS = 14
FLOW = Workflow(
    "simulated",
    "ask",
    (
        Step("ask", "human", transitions=(("go", "work"),)),
        Step(
            "work", "agent", "fake", transitions=(("done", "review"),), mutates=True, required=True
        ),
        Step(
            "review",
            "check",
            "fake",
            transitions=(("passed", "finish"), ("failed", "work")),
            required=True,
            gate=True,
        ),
        Step("finish", "finish"),
    ),
    max_calls=4,
)


@dataclass
class World:
    """Everything outside the engine: file revisions per workspace and the clock."""

    revisions: dict[str, int] = field(default_factory=dict)
    now: float = 1000.0

    def revision(self, path: str) -> str:
        return f"{path}@{self.revisions.get(path, 0)}"

    def change(self, path: str) -> str:
        self.revisions[path] = self.revisions.get(path, 0) + 1
        return self.revision(path)


class SimulatedProject:
    manifest = Manifest("simulated", "1")

    def __init__(self, world: World) -> None:
        self.world = world

    def revision(self, workspace: str) -> str:
        return self.world.revision(workspace)


class SimulatedWorkspace:
    """A claim is its workspace; evidence is trusted as delivered."""

    def resolve(self, path: str) -> str:
        return path

    def claim(self, root: str, scope: tuple[str, ...]) -> str:
        return root

    def paths(self, claim: str) -> tuple[str, ...]:
        return (claim,)

    def overlaps(self, left: str, right: str) -> bool:
        return left == right

    def normalize(self, run_id: str, result: Result, workspace: str) -> Result:
        return result

    def verify(self, result: Result, workspace: str, revision: str) -> None:
        return None


class Simulation:
    def __init__(self, seed: int) -> None:
        self.random = random.Random(seed)
        self.world = World()
        self.store = MemoryStore()
        self.definition = self.store.publish(FLOW)
        self.workspaces = {"a": "w1", "b": "w1", "c": "w2", "d": "w3"}
        self.engine = self._engine()
        self.accepted: set[str] = set()
        self.versions: dict[str, int] = {}
        self.delivered: dict[str, Result] = {}
        # Agent attempts that could have reached a model, counted outside the engine.
        self.model_calls: dict[str, int] = {}
        self.attempts = 0
        for run_id, workspace in self.workspaces.items():
            self.engine.create(run_id, self.definition, workspace, "", None, self.world.now)  # type: ignore[arg-type]

    def _engine(self) -> ApplicationEngine:
        return ApplicationEngine(
            self.store,
            SimulatedProject(self.world),
            SimulatedWorkspace(),
            max_queue_calls=MAX_QUEUE_CALLS,
        )

    # Actions ------------------------------------------------------------------

    def step(self) -> None:
        run_id = self.random.choice(sorted(self.workspaces))
        # Normal work is frequent; faults are rare enough that runs still finish.
        actions = [
            (self.command, 6),
            (self.dispatch, 8),
            (self.finish_attempt, 8),
            (self.answer, 4),
            (self.redeliver, 2),
            (self.lose_attempt, 1),
            (self.refuse, 1),
            (self.fail_launch, 1),
            (self.change_outside, 1),
            (self.restart_process, 1),
            (self.tick, 3),
        ]
        (action,) = self.random.choices(
            [act for act, _ in actions], [weight for _, weight in actions]
        )
        try:
            action(run_id)
        except (ValueError, Conflict):
            pass  # refused inputs are part of the schedule; they must change nothing

    def command(self, run_id: str) -> None:
        run = self.store.get(run_id)
        command = self.random.choice(["resume", "resume", "retry", "retry", "pause", "stop"])
        self.engine.command(run_id, command, self._id(), run.version, self.world.now)

    def dispatch(self, run_id: str) -> None:
        before = self.store.get(run_id).generation
        after = self.engine.dispatch(run_id, self.world.now, self._id())
        if after.generation > before and self._agent(after):
            self.model_calls[run_id] = self.model_calls.get(run_id, 0) + 1

    def _agent(self, run: Run) -> bool:
        return run.active is not None and FLOW.step(run.active.step).kind == "agent"

    def refuse(self, run_id: str) -> None:
        """The provider refuses at once (a spent subscription): no model work was done."""
        run = self.store.get(run_id)
        if not self._agent(run) or run.active is None:
            return
        refusal = Result(
            run.active.id,
            run.active.generation,
            "waiting",
            "usage limit",
            run.active.base_revision,
            resume_at=self.world.now + 60,
            data='{"failure":"usage_limit"}',
        )
        self.engine.complete(run_id, refusal, self.world.now)
        self.model_calls[run_id] -= 1

    def fail_launch(self, run_id: str) -> None:
        """The host dies before GO: the payload, and so any model, never started."""
        run = self.store.get(run_id)
        if not self._agent(run) or run.active is None:
            return
        revision = self.world.revision(self.workspaces[run_id])
        self.engine.recover(run_id, self.world.now, True, "no GO", revision, launched=False)
        self.model_calls[run_id] -= 1

    def finish_attempt(self, run_id: str) -> None:
        run = self.store.get(run_id)
        if run.active is None:
            return
        step = FLOW.step(run.active.step)
        if step.kind == "human":
            return
        outcome = self.random.choice([*dict(step.transitions), "waiting", "blocked"])
        revision = run.active.base_revision
        if step.mutates and outcome == "done":
            revision = self.world.change(self.workspaces[run_id])
        result = Result(
            run.active.id,
            run.active.generation,
            outcome,
            "simulated",
            revision,
            artifacts=(Artifact("evidence.txt", "0" * 64, revision),)
            if outcome == "passed"
            else (),
            resume_at=self.world.now + 30 if outcome == "waiting" else None,
        )
        self.engine.complete(run_id, result, self.world.now)
        self.delivered[run_id] = result

    def answer(self, run_id: str) -> None:
        run = self.store.get(run_id)
        self.engine.answer(run_id, "go", "Proceed", {}, run.version, self.world.now)

    def redeliver(self, run_id: str) -> None:
        """A host reports the same result twice: the second delivery changes nothing."""
        result = self.delivered.get(run_id)
        if result is None:
            return
        before = self.store.get(run_id)
        after = self.engine.complete(run_id, result, self.world.now)
        assert after == before, "a repeated result was applied twice"

    def lose_attempt(self, run_id: str) -> None:
        run = self.store.get(run_id)
        if run.active is None or FLOW.step(run.active.step).kind == "human":
            return
        confirmed = self.random.random() < 0.8
        revision = self.world.revision(self.workspaces[run_id])
        self.engine.recover(run_id, self.world.now, confirmed, "host lost", revision)

    def change_outside(self, run_id: str) -> None:
        """Someone edits the workspace; idle unfinished runs there see the change."""
        workspace = self.workspaces[run_id]
        revision = self.world.change(workspace)
        for other, path in self.workspaces.items():
            run = self.store.get(other)
            if path == workspace and run.active is None and run.status != "accepted":
                self.engine.invalidate(other, revision, self.world.now)

    def restart_process(self, run_id: str) -> None:
        """The engine process restarts: every in-memory cache is gone, storage stays."""
        self.engine = self._engine()

    def tick(self, run_id: str) -> None:
        self.world.now += self.random.choice([1.0, 10.0, 120.0, 2000.0])

    def _id(self) -> str:
        self.attempts += 1
        return f"x{self.attempts}"

    # Invariants ---------------------------------------------------------------

    def check(self) -> None:
        runs = [self.store.get(run_id) for run_id in sorted(self.workspaces)]
        for run in runs:
            self._check_run(run)
        running = [
            (run, FLOW.step(run.active.step).kind)
            for run in runs
            if run.active is not None and FLOW.step(run.active.step).kind in PROCESS_KINDS
        ]
        assert sum(kind == "agent" for _, kind in running) <= 2, "agent slots exceeded"
        assert sum(kind != "agent" for _, kind in running) <= 1, "operation slots exceeded"
        places = [self.workspaces[run.id] for run, _ in running]
        assert len(places) == len(set(places)), "two processes own one workspace"
        for workspace in set(self.workspaces.values()):
            holders = [
                run.id
                for run in runs
                if self.workspaces[run.id] == workspace and machine.holds_claim(run, FLOW)
            ]
            assert len(holders) <= 1, f"{holders} hold {workspace} at once"
        assert sum(run.spend.calls for run in runs) <= MAX_QUEUE_CALLS, "queue budget exceeded"

    def _check_run(self, run: Run) -> None:
        assert run.spend.calls == self.model_calls.get(run.id, 0), "a call no model made"
        assert run.version >= self.versions.get(run.id, 0), "version went back"
        self.versions[run.id] = run.version
        if run.status == "running":
            assert run.active is not None
        if run.status in ("ready", "waiting", "accepted"):
            assert run.active is None
        if run.cause not in ("", "stop"):
            assert run.status == "blocked"
        if run.status == "blocked":
            assert run.cause and run.reason, "blocked without a cause people can read"
        assert run.spend.calls <= FLOW.max_calls + run.spend.granted_calls
        if run.id in self.accepted:
            assert run.status == "accepted", "an accepted run changed"
        if run.status == "accepted":
            self.accepted.add(run.id)
            assert machine.unsatisfied(run, FLOW) == [], "accepted without its gates"


@pytest.mark.parametrize("seed", SEEDS)
def test_seeded_schedules_keep_the_engine_invariants(seed):
    simulation = Simulation(seed)
    for number in range(STEPS):
        simulation.step()
        try:
            simulation.check()
        except AssertionError as error:
            raise AssertionError(f"seed {seed}, step {number}: {error}") from error


def test_a_seed_replays_the_same_schedule():
    def final(seed: int) -> list[Run]:
        simulation = Simulation(seed)
        for _ in range(STEPS):
            simulation.step()
        return [replace(simulation.store.get(r)) for r in sorted(simulation.workspaces)]

    assert final(7) == final(7)


def test_schedules_reach_acceptance():
    accepted = set()
    for seed in SEEDS:
        simulation = Simulation(seed)
        for _ in range(STEPS):
            simulation.step()
        accepted |= {
            f"{seed}:{run}"
            for run in simulation.workspaces
            if simulation.store.get(run).status == "accepted"
        }
    assert accepted, "no schedule accepted any run: the simulation does not exercise finish"
