"""Read-only Codex app-server quota probe; no threads or model turns are created."""

import json
import queue
import subprocess
import threading
import time

from sdd_runtime.platform import NO_WINDOW
from sdd_runtime.versions import engine


def read_codex_limits(argv: tuple[str, ...], timeout: float = 12) -> dict[str, object]:
    process = subprocess.Popen(
        [*argv, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        creationflags=NO_WINDOW,
    )
    messages: queue.Queue[str | None] = queue.Queue(maxsize=256)
    assert process.stdout and process.stdin

    def read() -> None:
        assert process.stdout
        for line in process.stdout:
            try:
                messages.put_nowait(line)
            except queue.Full:
                break
        try:
            messages.put_nowait(None)
        except queue.Full:
            pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def send(value: dict[str, object]) -> None:
        assert process.stdin
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    def response(identifier: int) -> dict[str, object]:
        while True:
            line = messages.get(timeout=max(0, deadline - time.monotonic()))
            if line is None:
                raise ValueError("Account probe exited before replying")
            message = json.loads(line)
            if message.get("id") != identifier:
                continue
            if "error" in message:
                raise ValueError("Account endpoint unavailable; check CLI sign-in")
            result = message.get("result")
            if not isinstance(result, dict):
                raise ValueError("Unrecognized account response")
            return result

    try:
        send(
            {
                "id": 0,
                "method": "initialize",
                "params": {"clientInfo": {"name": "feature_factory_ai", "version": engine()}},
            }
        )
        response(0)
        send({"method": "initialized"})
        send({"id": 1, "method": "account/rateLimits/read"})
        result = response(1)
        buckets = result.get("rateLimitsByLimitId")
        if not isinstance(buckets, dict) or not buckets:
            buckets = {"codex": result.get("rateLimits")}
        windows = []
        for name, bucket in buckets.items():
            if not isinstance(bucket, dict):
                continue
            for kind in ("primary", "secondary"):
                value = bucket.get(kind)
                if not isinstance(value, dict):
                    continue
                used = value.get("usedPercent")
                if not isinstance(used, (int, float)) or isinstance(used, bool):
                    continue
                windows.append(
                    {
                        "bucket": name,
                        "window": kind,
                        "used_percent": used,
                        "remaining_percent": max(0, min(100, 100 - used)),
                        "duration_minutes": value.get("windowDurationMins"),
                        "resets_at": value.get("resetsAt"),
                    }
                )
        return {
            "status": "available" if windows else "unavailable",
            "windows": windows,
            "checked_at": time.time(),
            "source": "codex app-server",
        }
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        reader.join(timeout=1)
        process.stdin.close()
        process.stdout.close()
