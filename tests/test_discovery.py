import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sdd_providers.catalog import AgentAdapter, adapters
from sdd_providers.model_catalog import cursor_models, opencode_models
from sdd_runtime.discovery import discover, list_models


def test_discovery_distinguishes_missing_ambiguous_and_auth(monkeypatch, tmp_path):
    monkeypatch.setattr("sdd_runtime.discovery.shutil.which", lambda _: None)
    spec = adapters()["codex"]
    assert discover(spec).status == "missing"
    first, second = tmp_path / "a.exe", tmp_path / "b.exe"
    first.touch()
    second.touch()
    monkeypatch.setattr(
        "sdd_runtime.discovery.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"1.0"),
    )
    spec = replace(spec, install_patterns=(str(tmp_path / "*.exe"),))
    result = discover(spec)
    assert result.status == "ambiguous" and len(result.candidates) == 2
    assert result.authentication == "unknown" and result.qualification == "not_checked"
    second.unlink()
    assert discover(spec).status == "installed"


def test_discovery_reports_timeout_and_skips_shell_wrappers(monkeypatch):
    monkeypatch.setattr("sdd_runtime.discovery.shutil.which", lambda _: "C:/tools/tool.cmd")
    assert discover(adapters()["codex"]).status == "missing"
    monkeypatch.setattr("sdd_runtime.discovery.shutil.which", lambda _: sys.executable)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("version", 5)

    monkeypatch.setattr("sdd_runtime.discovery.subprocess.run", timeout)
    result = discover(adapters()["codex"])
    assert result.status == "broken" and result.candidates[0].error == "TimeoutExpired"


def test_catalog_formats_do_not_treat_headings_as_ids(monkeypatch):
    assert cursor_models(
        "Available models\nauto - Auto (current, default)\nkimi-k3-max - Kimi K3\nTip: text"
    ) == ("auto", "kimi-k3-max")
    assert opencode_models("Models\nvendor/model\nvendor/other\n") == (
        "vendor/model",
        "vendor/other",
    )
    monkeypatch.setattr(
        "sdd_runtime.discovery.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b""),
    )
    with pytest.raises(ValueError, match="availability remains unknown"):
        list_models((sys.executable,), ("models",), opencode_models)


def test_external_adapter_registration_is_explicit(monkeypatch):
    factory = adapters()["codex"].factory
    calls = []
    extension = AgentAdapter("external", ("external",), factory)

    def load():
        calls.append("loaded")
        return lambda: extension

    monkeypatch.setattr(
        "sdd_providers.catalog.entry_points",
        lambda **kwargs: [SimpleNamespace(name="external", load=load)],
    )
    assert "external" not in adapters() and not calls
    assert adapters(("external",))["external"] == extension and calls == ["loaded"]
    with pytest.raises(ValueError, match="Duplicate"):
        adapters(("external", "external"))
