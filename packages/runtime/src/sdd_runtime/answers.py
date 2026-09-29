"""Human steps: what a run asks, and the answers people (or recommendations) give."""

from sdd_core import machine, questions
from sdd_core.codec import canonical, result_load
from sdd_core.models import Json, Result, Run
from sdd_core.ports import Conflict

from sdd_runtime.intake import ResultIntake
from sdd_runtime.runs import RunContext, data_of


class HumanAnswers:
    def __init__(self, runs: RunContext, intake: ResultIntake) -> None:
        self.runs = runs
        self.intake = intake

    def facts(self, run_id: str) -> str:
        """Data of the result that led to the current step (the agent's questions)."""
        with self.runs.store.unit() as db:
            run = db.run(run_id)
            previous = run.active.previous if run.active else run.previous_attempt
            return data_of(db.results(run_id), previous)

    def outputs(self, run_id: str, product: str) -> tuple[Result, ...]:
        """Processed results of the run's steps that declare `produces: <product>`, newest
        first. A product is what a step's result is for: a specification, tickets."""
        workflow = self.runs.workflow_of(run_id)
        steps = {step.id for step in workflow.steps if step.options.produces == product}
        with self.runs.store.unit() as db:
            found = db.step_results(run_id)
        return tuple(result_load(document) for step, document in found if step in steps)

    def asked(self, run_id: str) -> str:
        """Questions for the waiting human step: the agent's, else the step's own."""
        facts = self.facts(run_id)
        if questions.questions(facts):
            return facts
        run = self.runs.store.get(run_id)
        if run.active is None:
            return "{}"
        step = self.runs.flow(run.workflow_digest).step(run.active.step)
        return canonical({"questions": list[Json](step.options.questions)})

    def answer(
        self,
        run_id: str,
        outcome: str,
        reply: str,
        choices: dict[str, str],
        expected: int,
        now: float,
    ) -> Run:
        """Resolve a waiting human step with chosen options and/or free text."""
        run = self.runs.store.get(run_id)
        workflow = self.runs.flow(run.workflow_digest)
        if run.version != expected:
            raise Conflict("Stale answer; refresh the task")
        if run.active is None or workflow.step(run.active.step).kind != "human":
            raise ValueError("Not waiting for a human")
        asked = questions.questions(self.asked(run_id)) if choices else ()
        if choices and not set(choices) <= {q.id for q in asked}:
            raise ValueError("Answer refers to an unknown question")
        text, data = questions.answer(asked, choices, reply)
        result = machine.human_result(run, outcome, text, data)
        return self.intake.complete(run_id, result, now, expected)

    def auto_answer(self, run_id: str, now: float) -> Run | None:
        """Answer a waiting human step with the agent's recommendations when allowed.

        Allowed by the task's operator switch or the step's `auto_answer:
        recommended` policy, and only when every question has a recommendation.
        """
        run = self.runs.store.get(run_id)
        if run.active is None:
            return None
        step = self.runs.flow(run.workflow_digest).step(run.active.step)
        settings = step.options
        if step.kind != "human" or not (run.auto_answer or settings.auto_answer == "recommended"):
            return None
        try:
            decision = questions.recommended(self.asked(run_id))
        except ValueError:
            return None
        if decision is None:
            return None
        outcome = settings.auto_answer_outcome(step.transitions)
        text, data = decision
        return self.intake.complete(run_id, machine.human_result(run, outcome, text, data), now)
