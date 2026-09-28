from sdd_core.machine import complete, dispatch
from sdd_core.models import Artifact, Result, Run, Usage
from sdd_workflows.templates import main_flow


def test_main_flow_failed_implementation_can_repair_and_pass_all_gates():
    workflow = main_flow()
    state = Run("flow", "digest", workflow.entry, "revision", paused=False)
    outcomes = ("done", "done", "failed", "done", "done", "passed", "passed")
    for index, outcome in enumerate(outcomes):
        state = dispatch(state, workflow, index * 10, str(index)).state
        result = Result(
            str(index),
            state.generation,
            outcome,
            "bounded result",
            "revision",
            (Artifact("proof", "hash", "revision"),),
            Usage(1, 1, 0, 0),
            True,
            True,
        )
        state = complete(state, workflow, result, index * 10 + 1).state
    assert dispatch(state, workflow, 100, "finish").state.status == "accepted"
