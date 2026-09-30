import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from sdd_core.models import Artifact, Result
from sdd_runtime.files import evidence, revision
from test_runtime import runtime


@pytest.mark.parametrize("prefix", ["", "./"])
def test_mutable_checkpoint_is_not_acceptance_evidence(tmp_path, prefix):
    coordinator = runtime(tmp_path)
    engine = coordinator.engine
    try:
        with engine.store.transaction() as db:
            root = Path(db.execute("SELECT workspace FROM runs WHERE id='one'").fetchone()[0])
        # The run's own files lie in the engine's folder, not in the workspace.
        folder = Path(engine.workspace.folder("one"))
        checkpoint = folder / "checkpoint.md"
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text("before")
        proof = folder / "a" / "proof.log"
        proof.parent.mkdir()
        proof.write_text("passed")
        state = engine.dispatch("one", 1, "a")
        current = revision(root)
        digest = hashlib.sha256(b"before").hexdigest()
        mutable = Artifact(".sdd-engine/one/checkpoint.md", digest, current)
        assert evidence(proof, root, folder / "a", current).path == ".sdd-engine/one/a/proof.log"
        assert not list(root.iterdir()), "nothing of the engine lies in the project"
        checkpoint_only = Result("a", state.generation, "passed", "", current, (mutable,))
        with pytest.raises(ValueError, match="checkpoint"):
            engine.complete("one", checkpoint_only, 2)
        result = Result(
            "a",
            state.generation,
            "passed",
            "",
            current,
            (
                replace(mutable, path=prefix + ".sdd-engine/one/checkpoint.md"),
                evidence(proof, root, folder / "a", current),
            ),
        )
        engine.complete("one", result, 2)
        checkpoint.write_text("after")
        assert engine.dispatch("one", 3, "finish").status == "accepted"
    finally:
        coordinator.close()
