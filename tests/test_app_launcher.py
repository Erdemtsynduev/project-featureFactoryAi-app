import threading

import pytest
from sdd_ui import app
from sdd_ui.server import create_server
from sdd_ui.service import WorkspaceService


def test_second_click_reuses_only_matching_application(tmp_path, monkeypatch):
    database = tmp_path / "ui.db"
    service = WorkspaceService(database)
    # Starting never asks DNS for this machine's name: that lookup hangs where it fails.
    monkeypatch.setattr("socket.getfqdn", lambda *name: pytest.fail("No name lookup"))
    server = create_server(service, 0)
    assert server.server_name == "127.0.0.1" and server.server_port > 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    opened = []
    monkeypatch.setattr(app, "URL", url)
    monkeypatch.setattr(app.webbrowser, "open", opened.append)
    monkeypatch.setattr(app, "launch", lambda *args: pytest.fail("Second server must not launch"))
    try:
        assert app.open_application(database) == 0
        assert app.open_application(database) == 0
        assert opened == [url, url]
        with pytest.raises(RuntimeError, match="another workspace"):
            app.open_application(tmp_path / "different.db")
        assert not service.settings["running"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        service.coordinator.close()


@pytest.mark.parametrize(
    "platform,variable", [("win32", "LOCALAPPDATA"), ("linux", "XDG_DATA_HOME")]
)
def test_data_directory_is_independent_of_working_directory(
    tmp_path, monkeypatch, platform, variable
):
    monkeypatch.setattr(app.sys, "platform", platform)
    monkeypatch.setenv(variable, str(tmp_path / "user data"))
    before = app.data_directory()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert app.data_directory() == before == tmp_path / "user data/FeatureFactoryAI"
