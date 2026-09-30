"""Multi-repository workspaces: scoped ownership and revisions of nested repositories."""

import subprocess
from pathlib import Path

import pytest
from sdd_core.models import Step, Workflow
from sdd_core.ports import Conflict
from sdd_runtime.application import ApplicationEngine
from sdd_runtime.files import revision
from sdd_runtime.git import GitProject
from sdd_runtime.workspace import LocalWorkspace, claim_paths, overlaps, scoped_claim
from sdd_storage.store import Store


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def repository(path: Path, ignore_all: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("start\n", encoding="utf-8")
    (path / "NAME").write_text(path.name, encoding="utf-8")
    if ignore_all:
        (path / ".gitignore").write_text("/*\n!/.gitignore\n!/*.md\n!/NAME\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def workspace(tmp_path):
    root = repository(tmp_path / "meta", ignore_all=True)
    repository(root / "libraries" / "alpha")
    repository(root / "libraries" / "beta")
    return root


def test_revision_tracks_every_change_against_head(tmp_path):
    repo = repository(tmp_path / "repo")
    clean = revision(repo)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    modified = revision(repo)
    assert modified != clean
    (repo / "README.md").write_text("start\n", encoding="utf-8")
    assert revision(repo) == clean
    (repo / "new.txt").write_text("x", encoding="utf-8")
    untracked = revision(repo)
    assert untracked not in (clean, modified)
    (repo / "new.txt").write_text("y", encoding="utf-8")
    assert revision(repo) != untracked
    (repo / "new.txt").unlink()
    (repo / "README.md").unlink()
    assert revision(repo) not in (clean, modified, untracked)
    git(repo, "checkout", "--", "README.md")
    assert revision(repo) == clean
    (repo / ".sdd-engine").mkdir()
    (repo / ".sdd-engine" / "scratch").write_text("ignored", encoding="utf-8")
    assert revision(repo) == clean
    git(repo, "mv", "README.md", "RENAMED.md")
    assert revision(repo) != clean


def test_parent_revision_does_not_see_ignored_nested_repositories(workspace):
    parent = revision(workspace)
    (workspace / "libraries" / "alpha" / "README.md").write_text("edit", encoding="utf-8")
    assert revision(workspace) == parent
    assert revision(workspace / "libraries" / "alpha") != revision(workspace / "libraries" / "beta")


def test_scoped_claims_validate_and_overlap(workspace):
    root = str(workspace)
    assert scoped_claim(root, ()) == str(workspace.resolve())
    assert scoped_claim(root, (".",)) == str(workspace.resolve())
    single = scoped_claim(root, ("libraries/alpha",))
    assert claim_paths(single) == (str((workspace / "libraries/alpha").resolve()),)
    both = scoped_claim(root, ("libraries/beta", "libraries/alpha"))
    assert len(claim_paths(both)) == 2 and both.startswith("[")
    assert overlaps(both, single) and overlaps(both, root)
    assert not overlaps(single, scoped_claim(root, ("libraries/beta",)))
    planned = scoped_claim(root, ("libraries/gamma",))
    assert GitProject().revision(planned) != GitProject().revision(single)
    (workspace / "README.md").write_text("x", encoding="utf-8")
    for bad in (
        ("../outside",),
        (str(workspace),),
        ("libraries", "libraries/alpha"),
        ("missing/child",),
        ("README.md",),
        (".",) * 2 + ("libraries",),
    ):
        with pytest.raises((ValueError, OSError)):
            scoped_claim(root, bad)


def test_scoped_runs_own_only_their_repositories(tmp_path, workspace):
    store = Store(tmp_path / "state.db")
    flow = Workflow(
        "scoped",
        "work",
        (
            Step("work", "agent", "fake", transitions=(("done", "finish"),), mutates=True),
            Step("finish", "finish"),
        ),
    )
    definition = store.publish(flow)
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace())
    alpha = engine.create("alpha", definition, workspace, "", None, 0, scope=("libraries/alpha",))
    beta = engine.create("beta", definition, workspace, "", None, 0, scope=("libraries/beta",))
    both = engine.create(
        "both", definition, workspace, "", None, 0, scope=("libraries/alpha", "libraries/beta")
    )
    assert alpha.revision == revision(workspace / "libraries" / "alpha")
    assert len({alpha.revision, beta.revision, both.revision}) == 3
    for run in (alpha, beta, both):
        engine.command(run.id, "resume", run.id + "-resume", 0, 1)
    engine.dispatch("alpha", 2, "a1")
    engine.dispatch("beta", 2, "b1")
    with pytest.raises(Conflict):
        engine.dispatch("both", 2, "c1")
    before = engine.observe("beta")
    (workspace / "libraries" / "alpha" / "README.md").write_text("alpha work", encoding="utf-8")
    assert engine.observe("beta") == before
    assert engine.observe("alpha") != alpha.revision
    assert engine.observe("both") != both.revision


def test_folder_revision_ignores_the_rest_of_its_repository(tmp_path):
    repo = repository(tmp_path / "repo")
    plans = repo / "plans"
    plans.mkdir()
    (plans / "one.md").write_text("plan", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "plans")
    base = revision(plans)
    (repo / "README.md").write_text("elsewhere", encoding="utf-8")
    (repo / "other.txt").write_text("untracked elsewhere", encoding="utf-8")
    assert revision(plans) == base
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "unrelated commit")
    assert revision(plans) == base
    (plans / "one.md").write_text("edited plan", encoding="utf-8")
    assert revision(plans) != base


def test_committing_or_staging_unchanged_content_keeps_the_revision(tmp_path):
    repo = repository(tmp_path / "repo")
    plans = repo / "plans"
    plans.mkdir()
    (plans / "one.md").write_text("plan\r\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "plans")
    (plans / "one.md").write_text("plan edited\r\n", encoding="utf-8")
    (plans / "two.md").write_text("new", encoding="utf-8")
    edited, whole = revision(plans), revision(repo)
    git(repo, "add", "-A")
    assert (revision(plans), revision(repo)) == (edited, whole), "staging moves no content"
    git(repo, "commit", "-q", "-m", "commit the same content")
    assert (revision(plans), revision(repo)) == (edited, whole), "a commit moves no content"
    (plans / "two.md").write_text("changed", encoding="utf-8")
    assert revision(plans) != edited
