"""UI service actions: connecting discovered CLIs."""

import json
import sys

import pytest
from sdd_runtime.discovery import InstallationProbe
from sdd_ui.service import WorkspaceService


def test_connect_requires_a_discovered_installation(tmp_path):
    service = WorkspaceService(tmp_path / "ui.db")
    try:
        request = {"adapter": "claude", "argv": [sys.executable], "model": "opus"}
        with pytest.raises(ValueError, match="discovery"):
            service.mutate("connect", request)
        service.catalog.probes["claude"] = (InstallationProbe((sys.executable,), "2.1", None),)
        manifests = service.mutate("connect", request)
        assert "claude" in {item["id"] for item in manifests}
        config = json.loads(service.config.read_text(encoding="utf-8"))
        assert config["runners"]["claude"]["executable"] == sys.executable
        assert config["profiles"]["claude"] == {
            "runner": "claude",
            "model": "opus",
            "permissions": "workspace-write",
            "timeout_seconds": 3600,
        }
        # Reconnecting under another name keeps the first profile.
        service.mutate("connect", {**request, "name": "reviewer", "permissions": "read-only"})
        profiles = json.loads(service.config.read_text(encoding="utf-8"))["profiles"]
        assert set(profiles) == {"claude", "reviewer"}
        assert service.state()["totals"]["calls"] == 0
    finally:
        service.coordinator.close()
