"""The gated host, run in-process: GO, stdin delivery and its durable exit receipt."""

import io
import json
import sys
from pathlib import Path

import pytest
from sdd_runtime import host
from sdd_runtime.coordinator import read_exit

ECHO = "import sys; data = sys.stdin.read(); print(len(data)); sys.exit(3 if data else 4)"


def launch(folder: Path, **extra: object) -> None:
    document = {
        "argv": [sys.executable, "-c", ECHO],
        "cwd": str(folder),
        "environment": {"SDD_TEST": "1"},
        "nonce": "n-1",
        **extra,
    }
    (folder / "launch.json").write_text(json.dumps(document), encoding="utf-8")


def run_host(monkeypatch: pytest.MonkeyPatch, folder: Path, stdin: str) -> int:
    monkeypatch.setattr(host, "watch_parent", lambda expected=None: None)
    monkeypatch.setattr(sys, "argv", ["host", str(folder)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return host.main()


def test_eof_before_go_executes_nothing(tmp_path, monkeypatch):
    launch(tmp_path)
    assert run_host(monkeypatch, tmp_path, "") == 2
    assert not (tmp_path / "stdout.log").exists() and not (tmp_path / "exit.json").exists()


def test_the_prompt_reaches_the_process_on_stdin_not_argv(tmp_path, monkeypatch):
    prompt = "x" * 50_000  # past the Windows command-line limit
    (tmp_path / "input.txt").write_text(prompt, encoding="utf-8")
    launch(tmp_path, input="input.txt")
    assert run_host(monkeypatch, tmp_path, "GO\n") == 3
    assert (tmp_path / "stdout.log").read_text(encoding="utf-8").strip() == str(len(prompt))
    assert read_exit(tmp_path / "exit.json", "n-1")[0] == 3
    assert (tmp_path / "tmp").is_dir(), "the attempt gets its own temporary folder"


def test_without_input_the_process_reads_an_empty_stdin(tmp_path, monkeypatch):
    launch(tmp_path)
    assert run_host(monkeypatch, tmp_path, "GO\n") == 4


def test_an_exit_receipt_from_another_host_is_refused(tmp_path):
    receipt = tmp_path / "exit.json"
    receipt.write_text(
        json.dumps({"exit_code": 0, "nonce": "old", "completed_at": 5.0}), encoding="utf-8"
    )
    assert read_exit(receipt, "old") == (0, 5.0)
    with pytest.raises(ValueError, match="Wrong host receipt"):
        read_exit(receipt, "new")
