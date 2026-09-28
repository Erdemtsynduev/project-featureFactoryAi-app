import subprocess
from dataclasses import replace

import pytest
from sdd_core.models import Run
from sdd_core.portfolio import Portfolio, Ticket, accepted_requirements, ordered
from sdd_runtime.git import GitProject


def test_requirement_closes_only_after_all_its_tickets():
    p = Portfolio(
        "p", ("REQ-1",), (Ticket("a", ("REQ-1",), (), "a"), Ticket("b", ("REQ-1",), ("a",), "b"))
    )
    a = Run("a", "d", "finish", "r", status="accepted")
    b = replace(a, id="b", status="ready")
    assert [ticket.id for ticket in ordered(p)] == ["a", "b"]
    assert accepted_requirements(p, (a, b)) == ()
    assert accepted_requirements(p, (a, replace(b, status="accepted"))) == ("REQ-1",)
    with pytest.raises(ValueError, match="Cyclic"):
        ordered(replace(p, tickets=(replace(p.tickets[0], depends_on=("b",)), p.tickets[1])))


def test_git_worktree_integration_retry_and_conflict(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = GitProject()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    git.command(repo, "config", "user.name", "Fixture")
    git.command(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "code.txt").write_text("one")
    git.command(repo, "add", ".")
    git.command(repo, "commit", "-m", "fixture")
    base = git.command(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "worktree"
    assert git.create_worktree(repo, worktree, base) == base
    (worktree / "code.txt").write_text("two")
    git.command(worktree, "add", ".")
    git.command(worktree, "commit", "-m", "change")
    candidate = git.command(worktree, "rev-parse", "HEAD")
    journal = tmp_path / "integration.json"
    assert git.integrate(repo, candidate, base, journal) == candidate
    assert git.integrate(repo, candidate, base, journal) == candidate
    (repo / "code.txt").write_text("dirty")
    with pytest.raises(ValueError, match="dirty"):
        git.integrate(repo, base, candidate, tmp_path / "other.json")
