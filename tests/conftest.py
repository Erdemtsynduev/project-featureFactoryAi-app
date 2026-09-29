"""Suite-wide isolation from the operator's own accounts."""

import pytest


@pytest.fixture(autouse=True)
def no_subscription_sign_in(tmp_path_factory, monkeypatch):
    """Quota probes never read the real Claude sign-in or reach its usage endpoint."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-config")))
