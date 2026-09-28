"""Desktop entry point: persistent user data and reuse of an existing local server."""

import json
import os
import sys
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from sdd_ui.server import launch

URL = "http://127.0.0.1:8787"


def data_directory() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    if not base.is_absolute():
        raise ValueError("Application data directory must be absolute")
    return base / "FeatureFactoryAI"


def existing(database: Path) -> bool:
    try:
        with urlopen(URL + "/api/info", timeout=1) as response:
            info = json.load(response)
    except (URLError, TimeoutError):
        return False
    if (
        not isinstance(info, dict)
        or info.get("application") != "feature-factory-ai"
        or info.get("database") != str(database.resolve())
    ):
        raise RuntimeError(
            "Port 8787 belongs to another workspace; close it before opening the app"
        )
    return True


def open_application(database: Path) -> int:
    if existing(database):
        webbrowser.open(URL)
        return 0
    try:
        return launch(database, None, 8787, True)
    except (OSError, RuntimeError):
        # Two simultaneous clicks may race before the first server binds its port.
        for _ in range(20):
            if existing(database):
                webbrowser.open(URL)
                return 0
            time.sleep(0.1)
        raise


def main() -> int:
    try:
        return open_application(data_directory() / "ui.db")
    except Exception as error:
        message = f"Feature Factory AI could not start:\n{error}"
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        try:
            from tkinter import messagebox

            messagebox.showerror("Feature Factory AI", message)
        except Exception:
            pass  # Tk is optional on headless installations.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
