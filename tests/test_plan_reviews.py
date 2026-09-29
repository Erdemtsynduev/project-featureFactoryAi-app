"""A plan lead reviews an approved breakdown; approved proposals change the tickets."""

import sys

import pytest
from sdd_core.codec import canonical
from sdd_core.models import Result, Step, Workflow
from sdd_ui.service import WorkspaceService

ARGV = [sys.executable, "-c", "pass"]


def check(step: str, produces: str, transitions: tuple[tuple[str, str], ...]) -> Step:
    # A check stands in for an agent: the test supplies its result.
    config = canonical({"argv": ARGV, "produces": produces})
    return Step(step, "check", "command", transitions=transitions, config=config)


FEATURE = Workflow(
    "feature",
    "spec",
    (
        check("spec", "specification", (("done", "tickets"),)),
        check("tickets", "tickets", (("done", "approve"),)),
        Step(
            "approve",
            "human",
            prompt="Approve",
            transitions=(("approved", "accepted"), ("rework", "spec")),
        ),
        Step("accepted", "finish"),
    ),
)
REVIEW = Workflow(
    "plan-review",
    "review",
    (
        check("review", "plan_changes", (("done", "decide"), ("unchanged", "accepted"))),
        Step(
            "decide",
            "human",
            prompt="Decide",
            transitions=(("approved", "accepted"), ("rejected", "accepted"), ("rework", "review")),
        ),
        Step("accepted", "finish"),
    ),
)
TICKET = Workflow("ticket", "work", (check("work", "", (("done", "end"),)), Step("end", "finish")))


@pytest.fixture
def service(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path / "ui.db")
    root = tmp_path / "project"
    root.mkdir()
    service.mutate("project", {"id": "app", "name": "App", "workspace": str(root)})
    digests = {
        name: service.engine.store.publish(flow)
        for name, flow in (("plan-review", REVIEW), ("ticket", TICKET), ("feature", FEATURE))
    }
    monkeypatch.setattr(
        service.flows, "ensure", lambda name, project, language, repositories=(): digests[name]
    )
    yield service
    service.coordinator.close()


def finish(
    service: WorkspaceService,
    run_id: str,
    attempt: str,
    outcome: str = "done",
    reason: str = "ok",
    **data,
):
    engine = service.engine
    run = engine.dispatch(run_id, 10, attempt)
    return engine.complete(
        run_id,
        Result(attempt, run.generation, outcome, reason, run.revision, data=canonical(data)),
        11,
    )


def approved_plan(service: WorkspaceService) -> tuple[str, list[str]]:
    run = service.mutate(
        "create",
        {
            "title": "Rally",
            "project": "app",
            "definition": service.flows.ensure("feature", "app", "ru"),
            "context": "Go",
        },
    )
    feature = run["id"]
    service.mutate("resume", {"id": feature, "version": run["version"]})
    finish(service, feature, "s1", reason="SPEC: AC-1 engine, AC-2 docs")
    tickets = [
        {"id": "api", "title": "Engine core"},
        {"id": "ui", "title": "Engine UI", "depends_on": ["api"], "paths": ["ui/"]},
        {"id": "docs", "title": "Engine docs", "depends_on": ["ui"], "paths": ["ui/"]},
        {"id": "extra", "title": "Nice to have", "depends_on": ["docs"]},
    ]
    finish(service, feature, "t1", tickets=tickets)
    waiting = service.engine.dispatch(feature, 12, "h1")
    answered = service.mutate(
        "answer", {"id": feature, "outcome": "approved", "answer": "", "version": waiting.version}
    )
    return feature, answered["admitted"]


def review_of(service: WorkspaceService, feature: str) -> str:
    (review,) = [
        key
        for key, item in service.reviews.reviews_of(feature).items()
        if item.trigger.startswith("breakdown")
    ]
    return review


def test_approving_a_breakdown_asks_the_plan_lead_once(service):
    feature, children = approved_plan(service)
    assert children == [f"{feature}-{key}" for key in ("api", "ui", "docs", "extra")]
    review = review_of(service, feature)
    item = service.catalog.task(review)
    assert (item.reviews, item.trigger, item.intent) == (
        feature,
        f"breakdown:{feature}",
        "plan-review",
    )
    with service.engine.store.unit() as db:
        brief = db.context(review)
    assert "Engine docs" in brief and "never started" in brief and "AC-1 engine" in brief
    assert not service.engine.store.get(review).paused, "a review starts by itself"
    assert service.reviews.request(feature, f"breakdown:{feature}", "again") is None


def test_an_approved_review_merges_cancels_rewires_and_holds(service):
    feature, _ = approved_plan(service)
    review = review_of(service, feature)
    changes = [
        {
            "kind": "merge",
            "reason": "UI and docs own the same folder",
            "tickets": ["ui", "docs"],
            "drafts": [
                {"id": "ui", "title": "Engine UI and docs", "depends_on": ["api"], "paths": ["ui/"]}
            ],
        },
        {"kind": "cancel", "reason": "No acceptance criterion needs it", "ticket": "extra"},
        {
            "kind": "need",
            "reason": "Recordings must be licensed",
            "ticket": "api",
            "needs": ["asset"],
        },
        {
            "kind": "guide",
            "reason": "Say where loops live",
            "ticket": "api",
            "text": "Loops arrive in assets/engine",
        },
    ]
    finish(service, review, "r1", tickets=[], plan_changes=changes)
    assert [c["kind"] for c in service.detail(review)["changes"]] == [
        "merge",
        "cancel",
        "need",
        "guide",
    ]
    waiting = service.engine.dispatch(review, 12, "d1")
    service.mutate(
        "answer", {"id": review, "outcome": "approved", "answer": "", "version": waiting.version}
    )

    for gone in ("docs", "extra"):
        with pytest.raises(KeyError):
            service.engine.store.get(f"{feature}-{gone}")
    ui, api = f"{feature}-ui", f"{feature}-api"
    with service.engine.store.unit() as db:
        assert "Engine UI and docs" in db.context(ui)
        assert "Loops arrive in assets/engine" in db.context(api)
        edges = set(db.dependency_edges())
    assert edges == {(ui, api)}
    assert service.engine.store.get(api).paused, "a ticket that needs an asset waits for a person"
    breakdown = service.catalog.artifacts(feature)["tickets"].data
    assert [(t["id"], t["run"], t["needs"]) for t in breakdown] == [
        ("api", api, ["asset"]),
        ("ui", ui, []),
    ]
    assert "plan_changes" in service.catalog.artifacts(review)
    assert any(e["kind"] == "plan_revised" for e in service.engine.store.history(ui))
    assert service.reviews.apply(review) == [], "applying again changes nothing"
    assert service.reviews.reapply() == []


def test_a_ticket_its_agent_blocks_asks_for_one_review_with_the_reason(service):
    feature, _ = approved_plan(service)
    first = review_of(service, feature)
    finish(service, first, "r1", outcome="unchanged", tickets=[], plan_changes=[])
    assert service.engine.dispatch(first, 12, "f1").status == "accepted"
    api = f"{feature}-api"
    service.mutate("resume", {"id": api, "version": service.engine.store.get(api).version})
    blocked = finish(service, api, "w1", outcome="blocked", reason="No licensed engine recordings")
    review = service.reviews.blocked(blocked)
    assert review is not None and service.catalog.task(review).trigger == f"blocked:{api}:w1"
    with service.engine.store.unit() as db:
        brief = db.context(review)
    assert "ticket " + api + " was blocked by its agent" in brief
    assert brief.count("No licensed engine recordings") == 1, "the reason appears once"
    assert service.reviews.blocked(service.engine.store.get(api)) is None, "one review per block"
    assert service.reviews.sweep() == []


def test_a_review_touching_started_work_is_refused_before_the_answer(service):
    feature, _ = approved_plan(service)
    review = review_of(service, feature)
    finish(service, review, "r1", tickets=[], plan_changes=[{"kind": "cancel", "ticket": "api"}])
    waiting = service.engine.dispatch(review, 12, "d1")
    api = f"{feature}-api"
    service.mutate("resume", {"id": api, "version": service.engine.store.get(api).version})
    service.engine.dispatch(api, 13, "w1")  # the ticket started while the person decided
    with pytest.raises(ValueError, match="has started"):
        service.mutate(
            "answer",
            {"id": review, "outcome": "approved", "answer": "", "version": waiting.version},
        )
    assert service.engine.store.get(review).active is not None, "the review still awaits a decision"


def test_a_person_deciding_holds_no_paths():
    from sdd_core import machine
    from sdd_core.models import Attempt, Run

    flow = REVIEW
    deciding = Run(
        "r",
        "d",
        "decide",
        "v",
        status="running",
        paused=False,
        active=Attempt("h", "decide", 2, 0, 9, "v"),
    )
    reading = Run(
        "r",
        "d",
        "review",
        "v",
        status="running",
        paused=False,
        active=Attempt("a", "review", 1, 0, 9, "v"),
    )
    assert not machine.holds_claim(deciding, flow)
    assert machine.holds_claim(reading, flow), "a live process still holds its paths"


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_revise_rewrites_only_a_never_started_run(tmp_path, backend):
    from sdd_runtime.application import ApplicationEngine
    from sdd_runtime.git import GitProject
    from sdd_runtime.workspace import LocalWorkspace
    from sdd_storage.memory import MemoryStore
    from sdd_storage.store import Store

    store = MemoryStore() if backend == "memory" else Store(tmp_path / "state.db")
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace())
    root = tmp_path / "work"
    root.mkdir()
    definition = store.publish(TICKET)
    for identifier in ("a", "b", "c"):
        engine.create(identifier, definition, root, "brief " + identifier, "rev", 0)
    with store.unit() as db:
        claim = db.location("c")[1]
    revised = engine.revise("c", "new brief", claim, ("a", "b"), "req", 0, 1)
    assert engine.revise("c", "new brief", claim, ("a", "b"), "req", 0, 2) == revised
    with store.unit() as db:
        assert set(db.dependency_edges()) == {("c", "a"), ("c", "b")}
        assert db.context("c") == "new brief"
    with pytest.raises(ValueError, match="cycle"):
        engine.revise("a", "x", claim, ("c",), "cycle", 0, 3)
    engine.command("b", "resume", "resume-b", 0, 4)
    engine.dispatch("b", 5, "started")
    with pytest.raises(ValueError, match="never started"):
        engine.revise("b", "x", claim, (), "late", store.get("b").version, 6)
