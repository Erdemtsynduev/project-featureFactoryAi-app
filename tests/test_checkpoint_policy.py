from dataclasses import replace
from pathlib import Path

import pytest
from sdd_core.models import Result
from sdd_runtime.files import evidence, revision
from test_runtime import runtime


@pytest.mark.parametrize("prefix", ["", "./"])
def test_mutable_checkpoint_is_not_acceptance_evidence(tmp_path, prefix):
    coordinator = runtime(tmp_path)
    engine = coordinator.engine
    try:
        with engine.store.transaction() as db:
            root = Path(db.execute("SELECT workspace FROM runs WHERE id='one'").fetchone()[0])
        checkpoint = root / ".sdd-engine/one/checkpoint.md"
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text("before")
        proof = root / ".sdd-engine/one/proof.log"
        proof.write_text("passed")
        state = engine.dispatch("one", 1, "a")
        current = revision(root)
        checkpoint_only = Result(
            "a", state.generation, "passed", "", current, (evidence(checkpoint, root, current),)
        )
        with pytest.raises(ValueError, match="checkpoint"):
            engine.complete("one", checkpoint_only, 2)
        result = Result(
            "a",
            state.generation,
            "passed",
            "",
            current,
            (
                replace(
                    evidence(checkpoint, root, current),
                    path=prefix + ".sdd-engine/one/checkpoint.md",
                ),
                evidence(proof, root, current),
            ),
        )
        engine.complete("one", result, 2)
        checkpoint.write_text("after")
        assert engine.dispatch("one", 3, "finish").status == "accepted"
    finally:
        coordinator.close()
