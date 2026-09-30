"""Repositories that pin each other: lanes check dependencies out, the engine commits
the work and advances pins to what accepted prerequisites delivered."""

from pathlib import Path

import pytest
from sdd_core.links import Link, infer_link, missing, stale
from sdd_core.tickets import TicketDraft, one_repository, ticket_title
from sdd_runtime.lanes import CommitMessages, LaneJob, attach_links, commit, open_lane, remove_lane
from sdd_runtime.submodules import GitSubmodules, NoLinks, links_of
from sdd_workflows.templates import ticket
from test_lanes import git, repository


def submodule(app: Path, url: str, path: str) -> None:
    git(app, "-c", "protocol.file.allow=always", "submodule", "add", "-q", url, path)
    git(app, "commit", "-q", "-m", f"pin {path}")


@pytest.fixture
def linked(tmp_path) -> Path:
    """A workspace where `app` pins libraries/alpha and libraries/beta as submodules."""
    root = tmp_path / "ws"
    repository(root / "libraries" / "alpha")
    repository(root / "libraries" / "beta")
    repository(root / "libraries" / "gamma")
    repository(root / "tools" / "lint")
    app = repository(root / "app")
    submodule(app, "../libraries/alpha", "addons/alpha")
    submodule(app, "../libraries/beta", "addons/beta")
    return root


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def gitlink(repo: Path, path: str) -> str:
    return git(repo, "ls-files", "--stage", "--", path).split()[1]


# Pure rules ---------------------------------------------------------------------------


def test_links_are_advanced_forward_and_inferred_from_neighbours():
    links = (Link("addons/sky", "libraries/sky", "a"), Link("addons/core", "godot-core", "b"))
    assert stale(links, {"libraries/sky": "c", "libraries/new": "d"}) == ((links[0], "c"),)
    assert missing(links, {"libraries/new": "d", "app": "x"}, "app") == ("libraries/new",)
    assert infer_link(links, "libraries/new") == Link("addons/new", "libraries/new")
    assert infer_link(links, "tools/lint") is None, "no neighbour to follow"


def test_a_ticket_title_is_read_back_from_its_brief():
    brief = "Response language: Russian.\n" + TicketDraft("T7", "Engine loops").context("")
    assert ticket_title(brief) == "Engine loops" and ticket_title("no ticket") == ""


def test_each_ticket_changes_one_repository():
    scope = {"a": ("app",), "b": ("app", "libraries/sky")}
    one_repository((TicketDraft("a", "A"),), lambda d: scope[d.id])
    with pytest.raises(ValueError, match="b: app, libraries/sky"):
        one_repository((TicketDraft("a", "A"), TicketDraft("b", "B")), lambda d: scope[d.id])


def test_commit_messages_follow_the_project_convention():
    default = CommitMessages()
    assert default.for_work("app", "Engine loops") == "feat(app): Engine loops"
    assert default.for_pin("app", "libraries/sky", "0123456789abcdef") == (
        "build(app): pin sky 0123456789"
    )
    assert len(default.for_work("app", "x" * 200)) == 72
    with pytest.raises(KeyError):
        CommitMessages("{ticket}").check()


# The git submodule adapter --------------------------------------------------------------


def test_submodules_are_read_as_links_to_workspace_repositories(linked):
    adapter = links_of(str(linked), "app")
    assert isinstance(adapter, GitSubmodules)
    assert isinstance(links_of(str(linked), "libraries/alpha"), NoLinks)
    alpha, beta = adapter.links(str(linked), "app")
    assert (alpha.path, alpha.dependency) == ("addons/alpha", "libraries/alpha")
    assert alpha.pinned == head(linked / "libraries" / "alpha")
    assert beta.dependency == "libraries/beta"


# Lanes --------------------------------------------------------------------------------------


def test_a_lane_checks_dependencies_out_where_the_repository_links_them(linked):
    lane = open_lane(linked, "t1", (linked / "app",))
    work = Path(lane.root) / "app"
    assert (work / "addons" / "alpha" / "NAME").read_text(encoding="utf-8") == "alpha"
    assert {a.path for a in lane.attached} == {"app/addons/alpha", "app/addons/beta"}
    assert git(work, "status", "--porcelain") == "", "checked-out dependencies are clean"
    assert attach_links(lane) is False, "attaching again changes nothing"
    remove_lane(lane)
    worktrees = git(linked / "libraries" / "alpha", "worktree", "list")
    assert len(worktrees.splitlines()) == 1, "the dependency's lane worktree is gone"


def test_commit_records_work_then_pins_what_prerequisites_delivered(linked):
    lane = open_lane(linked, "t2", (linked / "app",))
    work = Path(lane.root) / "app"
    (work / "feature.txt").write_text("done", encoding="utf-8")
    alpha = linked / "libraries" / "alpha"
    (alpha / "file.txt").write_text("delivered", encoding="utf-8")
    git(alpha, "commit", "-qam", "deliver")
    job = LaneJob("Engine loops", CommitMessages(), ("libraries/alpha",))

    outcome, reason = commit(lane, job)
    assert outcome == "done", reason
    subjects = git(work, "log", "--format=%s", "-2").splitlines()
    assert subjects == [f"build(app): pin alpha {head(alpha)[:10]}", "feat(app): Engine loops"]
    assert git(work, "log", "-1", "--format=%ae") == "t@example.invalid", "configured author"
    assert gitlink(work, "addons/alpha") == head(alpha)
    assert head(work / "addons" / "alpha") == head(alpha), "the lane shows the new pin"
    assert git(work, "status", "--porcelain") == ""
    assert commit(lane, job) == ("done", "Nothing to commit")
    remove_lane(lane)


def test_a_delivered_dependency_without_a_link_is_linked_like_its_neighbours(linked):
    lane = open_lane(linked, "t3", (linked / "app",))
    work = Path(lane.root) / "app"
    gamma = linked / "libraries" / "gamma"
    outcome, reason = commit(lane, LaneJob("Use gamma", delivered=("libraries/gamma",)))
    assert outcome == "done", reason
    modules = (work / ".gitmodules").read_text(encoding="utf-8")
    assert "addons/gamma" in modules and "../libraries/gamma" in modules
    assert gitlink(work, "addons/gamma") == head(gamma)
    assert (work / "addons" / "gamma" / "NAME").read_text(encoding="utf-8") == "gamma"
    blocked = commit(lane, LaneJob("Use lint", delivered=("tools/lint",)))
    assert blocked[0] == "blocked" and "no neighbouring link" in blocked[1]
    remove_lane(lane)


def test_a_repository_without_links_is_only_committed(linked):
    alpha = linked / "libraries" / "alpha"
    lane = open_lane(linked, "t4", (alpha,))
    (Path(lane.root) / "libraries" / "alpha" / "new.txt").write_text("x", encoding="utf-8")
    outcome, reason = commit(lane, LaneJob("Alpha work", delivered=("libraries/beta",)))
    assert outcome == "done" and "alpha: work" in reason
    remove_lane(lane)


# The ticket flow ------------------------------------------------------------------------------


def test_isolated_tickets_commit_after_every_agent_pass():
    flow = ticket((), isolated=True, commit_messages=("chore({repo}): {title}", ""))
    routes = {step.id: dict(step.transitions) for step in flow.steps}
    assert routes["implement"]["done"] == "commit" and routes["repair"]["done"] == "commit"
    assert routes["commit"] == {"done": "review"}
    committing = flow.step("commit")
    assert committing.handler == "lane-commit" and committing.mutates
    assert committing.options.commit_message == "chore({repo}): {title}"
    assert flow.step("rebase").options.commit_message == "chore({repo}): {title}"
    assert "Do not commit" in flow.step("implement").prompt
    plain = ticket(())
    assert "commit" not in {step.id for step in plain.steps}
