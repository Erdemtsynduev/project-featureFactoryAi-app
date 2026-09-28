import time

import pytest
from sdd_core.portfolio import Portfolio, Ticket
from sdd_runtime.portfolio import PortfolioService
from test_runtime import runtime


def test_approved_tickets_create_paused_dependent_runs_idempotently(tmp_path):
    coordinator = runtime(tmp_path)
    engine = coordinator.engine
    definition = engine.store.get("one").workflow_digest
    portfolio = Portfolio(
        "release",
        ("feature",),
        (Ticket("a", ("feature",), (), "first"), Ticket("b", ("feature",), ("a",), "second")),
    )
    roots = {key: tmp_path / key for key in ("a", "b")}
    for root in roots.values():
        root.mkdir()
    service = PortfolioService(engine)
    try:
        assert service.admit(portfolio, definition, roots, 0) == ("a", "b")
        assert service.admit(portfolio, definition, roots, 1) == ("a", "b")
        assert engine.store.get("b").paused
        assert service.accepted(portfolio) == ()
        for identifier in ("a", "b"):
            engine.command(identifier, "resume", "resume-" + identifier, 0, time.time())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and service.accepted(portfolio) != ("feature",):
            coordinator.tick()
            time.sleep(0.05)
        assert service.accepted(portfolio) == ("feature",)
        from dataclasses import replace

        with pytest.raises(ValueError, match="immutable"):
            service.admit(
                replace(
                    portfolio,
                    requirement_ids=("different",),
                    tickets=(Ticket("a", ("different",), (), "x"),),
                ),
                definition,
                {"a": roots["a"]},
                2,
            )
    finally:
        coordinator.close()
