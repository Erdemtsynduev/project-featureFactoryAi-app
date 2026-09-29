"""Subscriptions pace agents by their windows; attempts that reach no model cost nothing."""

from dataclasses import replace
from datetime import datetime

import pytest
from sdd_core import machine
from sdd_core.admission import QueueBudget
from sdd_core.codec import canonical
from sdd_core.models import Attempt, Result, Run, Spend, Step, Usage, Workflow
from sdd_core.sdk import Registry
from sdd_providers.failures import classify
from sdd_runtime.application import ApplicationEngine
from sdd_runtime.coordinator import Coordinator
from sdd_runtime.git import GitProject
from sdd_runtime.rotation import Cooldowns
from sdd_runtime.workspace import LocalWorkspace
from sdd_storage.memory import MemoryStore
from sdd_ui.queue import _uncapped
from sdd_ui.subscriptions import QuotaMonitor, overview
from sdd_usage.quota import (
    CLOSE_POLL_SECONDS,
    POLL_SECONDS,
    REOPENED,
    REST_PREFIX,
    Quota,
    Rest,
    Window,
    claude_quota,
    codex_quota,
    next_reading,
    next_rest,
)

NOW = datetime(2026, 9, 29, 22, 0).timestamp()


# Reset moments the providers print ------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
            "to purchase more credits or try again at Oct 4th, 2026 11:41 PM.",
            datetime(2026, 10, 4, 23, 41),
        ),
        ("Claude usage limit reached. Your limit resets 3am", datetime(2026, 9, 30, 3, 0)),
        ("usage limit; try again at 11:41 PM", datetime(2026, 9, 29, 23, 41)),
        ("5-hour limit reached - resets 2:30pm (Europe/Berlin)", datetime(2026, 9, 30, 14, 30)),
    ],
)
def test_absolute_and_clock_reset_moments_are_read(message, expected):
    failure = classify(message, NOW)
    assert failure.kind == "usage_limit" and failure.reset_at == expected.timestamp()


def test_a_reset_beyond_a_week_or_without_a_moment_is_not_invented():
    assert classify("usage limit reached", NOW).reset_at is None
    far = "usage limit; try again at Dec 24th, 2026 9:00 AM"
    assert classify(far, NOW).reset_at is None


# Reading the providers' reports -----------------------------------------------------


def test_claude_report_gives_session_and_weekly_windows():
    report = {
        "five_hour": {"utilization": 4.0, "resets_at": "2026-09-29T23:50:00+00:00"},
        "seven_day": {"utilization": 93.0, "resets_at": "2026-09-29T22:00:00+00:00"},
        "seven_day_opus": None,
        "extra_usage": {"is_enabled": False},
    }
    quota = claude_quota(report, NOW, "pro")
    assert [(w.name, w.remaining_percent) for w in quota.windows] == [
        ("session", 96.0),
        ("weekly", 7.0),
    ]
    assert (
        quota.windows[1].resets_at
        == datetime.fromisoformat("2026-09-29T22:00:00+00:00").timestamp()
    )
    assert quota.rest_until(NOW) is None, "7% left is still room to work"


def test_codex_report_names_windows_by_duration():
    buckets = {
        "codex": {
            "primary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": NOW + 5e5},
            "secondary": None,
        }
    }
    quota = codex_quota(buckets, NOW)
    (weekly,) = quota.windows
    assert (weekly.name, weekly.spent) == ("weekly", True)
    assert quota.rest_until(NOW) == NOW + 5e5
    assert "weekly 100%" in quota.describe()


# Decisions ----------------------------------------------------------------------------


def spent(until: float) -> Quota:
    return Quota("codex", "available", (Window("weekly", 100, until, 10080),), NOW)


def test_a_spent_window_rests_the_profile_until_it_resets():
    rest = next_rest(spent(NOW + 3600), None, NOW)
    assert rest == Rest(NOW + 3600, REST_PREFIX + "codex subscription spent (weekly 100%)")
    assert next_rest(spent(NOW + 3600), rest, NOW) is None, "an unchanged rest is not rewritten"


def test_room_lifts_only_a_rest_a_report_set():
    room = Quota("codex", "available", (Window("weekly", 40, NOW + 3600),), NOW)
    ours = Rest(NOW + 3600, REST_PREFIX + "codex subscription spent (weekly 100%)")
    refusal = Rest(NOW + 3600, "usage_limit")
    assert next_rest(room, ours, NOW) == Rest(0.0, REOPENED)
    assert next_rest(room, refusal, NOW) is None, "a refusal may name a limit the report omits"


def test_a_failed_reading_decides_nothing():
    stale = spent(NOW + 3600).failed("URLError: offline", NOW)
    assert stale.windows and not stale.current
    assert next_rest(stale, None, NOW) is None


def test_readings_are_rare_close_to_a_limit_sooner_and_back_off_on_errors():
    calm = Quota("claude", "available", (Window("weekly", 50),), NOW)
    close = Quota("claude", "available", (Window("weekly", 93),), NOW)
    assert next_reading(calm, 0, NOW) == NOW + POLL_SECONDS
    assert next_reading(close, 0, NOW) == NOW + CLOSE_POLL_SECONDS
    assert next_reading(calm, 1, NOW) < next_reading(calm, 3, NOW) <= NOW + 3600


# The monitor --------------------------------------------------------------------------


class Probe:
    def __init__(self, quota: Quota) -> None:
        self.quota, self.reads = quota, 0

    def __call__(self, now: float) -> Quota:
        self.reads += 1
        return self.quota


def test_monitor_rests_spent_profiles_and_reads_on_schedule(tmp_path):
    cooldowns = Cooldowns(tmp_path / "cooldowns.json")
    probe = Probe(spent(NOW + 7200))
    monitor = QuotaMonitor(
        cooldowns, lambda: {"codex": "codex", "claude": "claude"}, {"codex": probe}
    )
    monitor.refresh(NOW)
    assert cooldowns.until("codex") == NOW + 7200 and cooldowns.until("claude") == 0.0
    monitor.refresh(NOW + 60)
    assert probe.reads == 1, "the next reading waits for its schedule"
    view = monitor.overview(NOW)
    (codex,) = view["subscriptions"]  # type: ignore[misc]
    assert codex["windows"][0]["remaining_percent"] == 0
    assert codex["profiles"] == [
        {"name": "codex", "resting_until": NOW + 7200, "reason": codex["profiles"][0]["reason"]}
    ]


def test_a_refusal_triggers_a_reading_at_once(tmp_path):
    cooldowns = Cooldowns(tmp_path / "cooldowns.json")
    probe = Probe(Quota("codex", "available", (Window("weekly", 40, NOW + 3600),), NOW))
    monitor = QuotaMonitor(cooldowns, lambda: {"codex": "codex"}, {"codex": probe})
    monitor.refresh(NOW)
    cooldowns.rest("codex", NOW + 3600, "usage_limit")  # the rotation saw a refusal
    monitor.refresh(NOW + 60)
    assert probe.reads == 2


def test_overview_without_readings_is_unavailable():
    assert overview({}, {}, {}, NOW) == {
        "status": "unavailable",
        "checked_at": None,
        "subscriptions": [],
    }


# Honest call accounting ---------------------------------------------------------------


def flow() -> Workflow:
    return Workflow(
        "paced",
        "work",
        (
            Step("work", "agent", "fake", transitions=(("done", "finish"),)),
            Step("finish", "finish"),
        ),
    )


def running() -> tuple[Run, Workflow]:
    workflow = flow()
    run = machine.dispatch(Run("r", "d", "work", "v", paused=False), workflow, 1, "a1").state
    assert run.spend.calls == 1 and run.active and run.active.calls == 1
    return run, workflow


def test_a_provider_refusal_returns_the_reserved_call():
    run, workflow = running()
    assert run.active
    refusal = Result(
        "a1",
        1,
        "waiting",
        "codex: usage limit",
        "v",
        resume_at=100.0,
        data=canonical({"failure": "usage_limit"}),
    )
    after = machine.complete(run, workflow, refusal, 2).state
    assert after.spend.calls == 0 and not after.spend.usage_unknown


def test_work_that_measured_tokens_keeps_its_call():
    run, workflow = running()
    worked = Result(
        "a1",
        1,
        "waiting",
        "rate limited mid-way",
        "v",
        usage=Usage(1000, 50),
        resume_at=100.0,
        data=canonical({"failure": "rate_limit"}),
    )
    assert machine.complete(run, workflow, worked, 2).state.spend.calls == 1


def test_an_attempt_that_never_launched_costs_nothing():
    run, _ = running()
    lost = machine.recover(
        run, 2, termination_confirmed=True, reason="no GO", observed_revision="v", launched=False
    ).state
    assert lost.spend == Spend() and lost.infrastructure_failures == 1
    launched = machine.recover(
        run, 2, termination_confirmed=True, reason="host died", observed_revision="v"
    ).state
    assert launched.spend.calls == 1 and launched.spend.usage_unknown
    with pytest.raises(ValueError, match="never launched"):
        machine.recover(
            run, 2, termination_confirmed=False, reason="?", observed_revision="v", launched=False
        )


def test_attempts_stored_before_reservations_return_nothing():
    run, _ = running()
    assert run.active
    older = replace(run, active=replace(run.active, calls=0, planning_calls=0))
    assert isinstance(older.active, Attempt)
    lost = machine.recover(
        older, 2, termination_confirmed=True, reason="no GO", observed_revision="v", launched=False
    ).state
    assert lost.spend.calls == 1


# Queue caps and dispatch --------------------------------------------------------------


def test_legacy_default_caps_are_read_as_no_cap():
    assert _uncapped({"max_calls": 40, "max_planning_calls": 8})["max_calls"] is None
    assert _uncapped({"max_calls": 60, "max_planning_calls": 8})["max_calls"] == 60


def test_queue_caps_are_optional_and_consistent():
    assert not QueueBudget().exhausted(10**6, 10**6, True)
    with pytest.raises(ValueError, match="planning cannot exceed"):
        QueueBudget(12, 13)
    with pytest.raises(ValueError, match="cannot exceed 100000"):
        QueueBudget(100001)


def test_a_resting_profile_leaves_its_steps_unstarted(tmp_path):
    store = MemoryStore()
    engine = ApplicationEngine(store, GitProject(), LocalWorkspace())
    root = tmp_path / "project"
    root.mkdir()
    engine.create("one", store.publish(flow()), root, "", "rev", 0)
    engine.command("one", "resume", "resume", 0, 1)
    coordinator = Coordinator(engine, Registry(), available=lambda profile: False)
    try:
        assert coordinator.tick(2) == 0
        run = store.get("one")
        assert run.generation == 0 and run.spend.calls == 0 and run.status == "ready"
    finally:
        coordinator.close()
