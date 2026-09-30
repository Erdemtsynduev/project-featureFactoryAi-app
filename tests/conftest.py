"""Suite-wide isolation from the operator's own accounts, and failures CI can show."""

import annotations
import pytest

# GitHub shows the first ten error annotations of a step.
ANNOTATIONS = 10


def pytest_terminal_summary(terminalreporter):
    """On GitHub Actions each failed test becomes an annotation of the run, so a red
    job says which test failed and why without opening its log."""
    if not annotations.enabled():
        return
    stats = terminalreporter.stats
    failed = [*stats.get("failed", ()), *stats.get("error", ())]
    for report in failed[:ANNOTATIONS]:
        path, line, _ = report.location
        terminalreporter.write_line(
            annotations.error(report.nodeid, report.longreprtext, path, (line or 0) + 1)
        )


@pytest.fixture(autouse=True)
def no_subscription_sign_in(tmp_path_factory, monkeypatch):
    """Quota probes never read the real Claude sign-in or reach its usage endpoint."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-config")))


@pytest.fixture(params=["sqlite", "memory"])
def any_store(request, tmp_path):
    """Each storage backend in turn: the same contract must hold for both."""
    from sdd_storage.memory import MemoryStore
    from sdd_storage.store import Store

    return Store(tmp_path / "state.db") if request.param == "sqlite" else MemoryStore()
