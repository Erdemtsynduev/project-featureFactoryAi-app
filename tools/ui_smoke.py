"""Probe the installed UI entry point in an isolated temporary database; no model calls."""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen

import psutil


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ffai-ui-smoke-") as directory:
        folder = Path(directory)
        log_path = folder / "server.log"
        with log_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "sdd_runtime.cli",
                    "--database",
                    str(folder / "ui.db"),
                    "ui",
                    "--no-browser",
                    "--port",
                    "0",
                ],
                cwd=folder,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    log = log_path.read_text(encoding="utf-8")
                    if "Feature Factory AI: http://" in log:
                        url = log.split("Feature Factory AI: ", 1)[1].splitlines()[0]
                        with urlopen(url + "/api/state", timeout=5) as response:
                            state = json.load(response)
                        assert state["totals"]["calls"] == 0
                        assert state["settings"]["running"] is False
                        for asset in (
                            "/",
                            "/app.js",
                            "/style.css",
                            "/preferences.js",
                            "/locale.js",
                            "/workspace.js",
                            "/workspace.css",
                            "/pipeline.js",
                            "/office.js",
                            "/nav.js",
                            "/persist.js",
                            "/onboarding.js",
                        ):
                            with urlopen(url + asset, timeout=5) as response:
                                assert response.status == 200 and response.read()
                        with urlopen(
                            Request(
                                url + "/api/shutdown",
                                b"{}",
                                {
                                    "Origin": url,
                                    "X-FFAI-Token": state["token"],
                                    "Content-Type": "application/json",
                                },
                            ),
                            timeout=5,
                        ) as response:
                            assert response.status == 200
                        process.wait(timeout=10)
                        print("Installed UI entry point and static assets: PASS")
                        return
                    if process.poll() is not None:
                        raise RuntimeError(log)
                    time.sleep(0.05)
                raise TimeoutError(log_path.read_text(encoding="utf-8"))
            finally:
                # This process owns an empty, isolated database and no executions.
                if process.poll() is None:
                    children = psutil.Process(process.pid).children(recursive=True)
                    for child in reversed(children):
                        try:
                            child.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    psutil.wait_procs(children, timeout=10)
                    process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
