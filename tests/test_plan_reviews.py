"""A plan lead reviews an approved breakdown; approved proposals change the tickets."""

import sys
from dataclasses import replace

import pytest
from sdd_core.codec import canonical
from sdd_core.models import Result, Step, Workflow
from sdd_core.ports import Conflict
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


def test_revise_rewrites_only_a_never_started_run(tmp_path, any_store):
    from sdd_runtime.application import ApplicationEngine
    from sdd_runtime.git import GitProject
    from sdd_runtime.workspace import LocalWorkspace

    store = any_store
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace())
    root = tmp_path / "work"
    root.mkdir()
    definition = store.publish(TICKET)
    for identifier in ("a", "b", "c"):
        engine.create(identifier, definition, root, "brief " + identifier, "rev", 0)
    with store.unit() as db:
        claim = db.location("c").claim
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


def test_approval_refuses_a_ticket_that_changes_two_repositories(service, tmp_path):
    from test_lanes import repository

    root = tmp_path / "project"
    repository(root / "libraries" / "sky")
    repository(root / "app")
    run = service.mutate(
        "create",
        {
            "title": "Wind",
            "project": "app",
            "definition": service.flows.ensure("feature", "app", "ru"),
        },
    )
    feature = run["id"]
    service.mutate("resume", {"id": feature, "version": run["version"]})
    finish(service, feature, "s1", reason="SPEC")
    tickets = [{"id": "wind", "title": "Wind everywhere", "paths": ["libraries/sky", "app"]}]
    finish(service, feature, "t1", tickets=tickets)
    waiting = service.engine.dispatch(feature, 12, "h1")
    with pytest.raises(ValueError, match="wind: app, libraries/sky"):
        service.mutate(
            "answer",
            {"id": feature, "outcome": "approved", "answer": "", "version": waiting.version},
        )
    assert service.engine.store.get(feature).active is not None, "the approval still waits"


def test_a_ticket_out_of_visits_asks_its_plan_for_a_review(service):
    feature, _ = approved_plan(service)
    first = review_of(service, feature)
    finish(service, first, "r1", outcome="unchanged", tickets=[], plan_changes=[])
    service.engine.dispatch(first, 12, "f1")
    api = f"{feature}-api"
    service.engine.block(api, 13, "Step visit limit", cause="visit_limit")
    stuck = replace(service.engine.store.get(api), previous_attempt="w9")
    review = service.reviews.blocked(stuck)
    assert review is not None and service.catalog.task(review).trigger == f"limit:{api}:w9"


def test_a_packet_names_the_repositories_accepted_prerequisites_delivered(tmp_path):
    from sdd_runtime.application import ApplicationEngine
    from sdd_runtime.git import GitProject
    from sdd_runtime.lanes import Lane, LaneRepo
    from sdd_runtime.packets import delivered
    from sdd_runtime.workspace import LocalWorkspace
    from sdd_storage.memory import MemoryStore

    store = MemoryStore()
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace())
    root = tmp_path / "work"
    root.mkdir()
    done = store.publish(Workflow("done", "end", (Step("end", "finish"),)))
    engine.create("lib", done, root, "", "rev", 0)
    engine.create("app", store.publish(TICKET), root, "", "rev", 0, ("lib",))
    lane = Lane("lib", str(root), str(root / "lane"), [LaneRepo("libraries/sky", "b", "c", "main")])
    with store.unit() as db:
        db.save_lane("lib", lane.document())
    assert delivered(store, "app") == (), "not accepted yet"
    engine.command("lib", "resume", "go", 0, 1)
    assert engine.dispatch("lib", 2, "a").status == "accepted"
    assert delivered(store, "app") == ("libraries/sky",)


def test_a_review_the_busy_plan_postponed_starts_once_the_plan_is_free(service):
    feature, _ = approved_plan(service)
    first = review_of(service, feature)
    api = f"{feature}-api"
    service.mutate("resume", {"id": api, "version": service.engine.store.get(api).version})
    blocked = finish(service, api, "w1", outcome="blocked", reason="No recordings")
    assert service.reviews.blocked(blocked) is None, "one review per plan at a time"
    finish(service, first, "r1", outcome="unchanged", tickets=[], plan_changes=[])
    service.engine.dispatch(first, 12, "f1")
    (started,) = service.reviews.sweep()
    assert service.catalog.task(started).trigger == f"blocked:{api}:w1"


def test_rebuilding_plans_again_what_never_started_and_keeps_started_work(service, tmp_path):
    root = tmp_path / "project"
    (root / "plans").mkdir()
    (root / "plans" / "110_RALLY_PLAN.md").write_text(
        "# Rally\n\n- [ ] **FH-02** — Мосты.\n- [ ] **FH-03** — Кабина.\n", encoding="utf-8"
    )
    project = {"id": "app", "name": "App", "workspace": str(root), "plans_folder": "plans"}
    service.mutate("project", project)
    feature, admitted = approved_plan(service)
    catalog = service.catalog
    catalog.update_task(feature, catalog.task(feature).changed(plan="110", rows=("FH-02",)))
    for key in admitted:
        catalog.update_task(key, catalog.task(key).changed(plan="110"))
    api = f"{feature}-api"
    service.mutate("resume", {"id": api, "version": service.engine.store.get(api).version})
    service.engine.dispatch(api, 13, "w1")

    review = review_of(service, feature)  # the breakdown's review, never started
    rebuilt = service.mutate("plans-rebuild", {"project": "app", "plan": "110"})
    assert rebuilt["removed"] == len(admitted) and rebuilt["reopened"] == [feature]
    assert rebuilt["created"] == ["feature_110"]
    runs = {r["id"] for r in service.state()["runs"]}
    assert {feature, api, "feature_110"} <= runs and not {f"{feature}-ui", review} & runs
    closed, started = catalog.task(feature), catalog.task(api)
    assert closed.closed and (started.parent, started.origin) == ("", feature)
    assert catalog.task("feature_110").rows == ("FH-02", "FH-03"), "its rows are planned again"
    with service.engine.store.unit() as unit:
        brief = unit.context("feature_110")
    assert f"{api} — Engine core (running)" in brief, "started work is not duplicated"
    assert service.mutate("plans-sync", {"project": "app"})["created"] == []
    assert service.reviews.request(feature, "blocked:api", "stuck") is None, "a closed plan"


def test_a_new_breakdown_waits_for_existing_tickets_of_its_plan_through_after(service):
    feature, _ = approved_plan(service)
    catalog = service.catalog
    for key, item in catalog.tasks().items():
        if key == feature or item.parent == feature:
            catalog.update_task(key, item.changed(plan="110"))
    run = service.mutate(
        "create",
        {
            "title": "Rally 2",
            "project": "app",
            "definition": service.flows.ensure("feature", "app", "ru"),
        },
    )
    again = run["id"]
    catalog.update_task(again, catalog.task(again).changed(plan="110"))
    service.mutate("resume", {"id": again, "version": run["version"]})
    finish(service, again, "s2", reason="SPEC")
    api = f"{feature}-api"

    def breakdown(attempt: str, after: list[str]) -> int:
        finish(service, again, attempt, tickets=[{"id": "cab", "title": "Cabin", "after": after}])
        return service.engine.dispatch(again, 12, "h" + attempt).version

    version = breakdown("t2", ["T1"])
    with pytest.raises(ValueError, match="not a ticket of this plan.*cab: T1"):
        service.mutate(
            "answer", {"id": again, "outcome": "approved", "answer": "", "version": version}
        )
    service.mutate(
        "answer", {"id": again, "outcome": "rework", "answer": "use run ids", "version": version}
    )
    finish(service, again, "s3", reason="SPEC")
    version = breakdown("t3", [api])
    answered = service.mutate(
        "answer", {"id": again, "outcome": "approved", "answer": "", "version": version}
    )
    (cab,) = answered["admitted"]
    with service.engine.store.unit() as unit:
        assert (cab, api) in set(unit.dependency_edges())


def test_started_tickets_are_superseded_by_the_feature_that_plans_them_again(service):
    feature, _ = approved_plan(service)
    catalog, engine = service.catalog, service.engine
    for key, item in catalog.tasks().items():
        if key == feature or item.parent == feature:
            catalog.update_task(key, item.changed(plan="110"))
    run = service.mutate(
        "create",
        {
            "title": "Rally 2",
            "project": "app",
            "definition": service.flows.ensure("feature", "app", "ru"),
        },
    )
    again = run["id"]
    catalog.update_task(again, catalog.task(again).changed(plan="110", kind="feature"))
    api, ui = f"{feature}-api", f"{feature}-ui"
    service.mutate("resume", {"id": api, "version": engine.store.get(api).version})
    engine.dispatch(api, 13, "w1")
    with pytest.raises(Conflict, match="live attempt"):
        service.mutate("supersede", {"ids": [api], "by": again})
    finish_attempt = engine.store.get(api)
    engine.complete(
        api,
        Result("w1", finish_attempt.generation, "blocked", "stuck", finish_attempt.revision),
        14,
    )
    service.mutate("resume", {"id": ui, "version": engine.store.get(ui).version})

    done = service.mutate("supersede", {"ids": [api, ui], "by": again})
    assert done["superseded"] == [api, ui]
    assert engine.store.get(ui).paused and catalog.task(api).superseded == again
    codes = {r["id"]: r["attention"]["code"] for r in service.state()["runs"]}
    assert codes[api] == codes[ui] == "superseded"
    assert api not in service.tasks.known_tickets(again), "a new ticket cannot wait for it"
    assert service.mutate("supersede", {"ids": [api], "by": again})["superseded"] == []
    with pytest.raises(ValueError, match="open feature"):
        service.mutate("supersede", {"ids": [api], "by": api})


def test_a_review_reflows_an_idle_ticket_and_moves_a_revised_one_to_its_repository_flow(
    service, tmp_path, monkeypatch
):
    (tmp_path / "project" / "beta" / ".git").mkdir(parents=True)
    feature, _ = approved_plan(service)
    engine = service.engine
    beta = engine.store.publish(replace(TICKET, id="ticket-beta"))
    ensure = service.flows.ensure
    monkeypatch.setattr(
        service.flows,
        "ensure",
        lambda name, project, language, repositories=(): (
            beta if repositories == ("beta",) else ensure(name, project, language)
        ),
    )
    api = f"{feature}-api"
    service.mutate("resume", {"id": api, "version": engine.store.get(api).version})
    finish(service, api, "w1")  # started, now idle before its finish
    snapshot, _ = service.reviews.snapshot(feature)
    assert snapshot.states["api"].flow == ("work",) and snapshot.states["api"].step == "end"
    review = review_of(service, feature)
    changes = [
        {
            "kind": "reflow",
            "reason": "The check cannot apply to recordings",
            "ticket": "api",
            "flow": [{"kind": "skip", "step": "work", "profile": "", "required": False}],
        },
        {
            "kind": "revise",
            "reason": "It changes the beta repository",
            "drafts": [{"id": "extra", "title": "Nice to have", "paths": ["beta/src"]}],
        },
    ]
    finish(service, review, "r1", tickets=[], plan_changes=changes)
    waiting = engine.dispatch(review, 12, "d1")
    service.mutate(
        "answer", {"id": review, "outcome": "approved", "answer": "", "version": waiting.version}
    )
    reflowed = engine.store.get(api)
    assert "work" not in {s.id for s in engine.store.workflow(reflowed.workflow_digest).steps}
    assert engine.store.get(f"{feature}-extra").workflow_digest == beta, "its repository's flow"
    assert service.reviews.apply(review) == [], "applying again changes nothing"


def test_a_person_changes_a_tasks_flow_and_moves_outdated_work_to_the_current_template(
    service, monkeypatch
):
    feature, admitted = approved_plan(service)
    engine = service.engine
    api, ui = f"{feature}-api", f"{feature}-ui"
    service.mutate("resume", {"id": api, "version": engine.store.get(api).version})
    engine.dispatch(api, 13, "live")  # a live attempt is never moved
    newer = engine.store.publish(replace(TICKET, id="ticket-v2", max_calls=12))
    ensure = service.flows.ensure
    monkeypatch.setattr(
        service.flows,
        "ensure",
        lambda name, project, language, repositories=(): (
            newer if name == "ticket" else ensure(name, project, language)
        ),
    )
    report = service.mutate("flows-update", {"project": "app", "dry": True})
    assert set(report["outdated"]) == set(admitted) and report["updated"] == []
    moved = service.mutate("flows-update", {"project": "app"})
    assert set(moved["updated"]) == set(admitted) - {api} and api in moved["refused"]
    assert engine.store.get(ui).workflow_digest == newer
    assert service.mutate("flows-update", {"project": "app", "dry": True})["outdated"] == [api]

    changed = service.mutate(
        "flow-change",
        {
            "id": ui,
            "version": engine.store.get(ui).version,
            "changes": [{"kind": "skip", "step": "work"}],
        },
    )
    assert changed["step"] == "end" and changed["workflow_digest"] != newer
    with pytest.raises(ValueError, match="own"):
        service.reflow.update({"id": feature, "version": engine.store.get(feature).version})
