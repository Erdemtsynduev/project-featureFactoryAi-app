import json
import subprocess
import sys
from pathlib import Path

from sdd_runtime.cli import main, parser


def test_no_arguments_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ffai"])
    assert main() == 0
    assert "Feature Factory AI" in capsys.readouterr().out


def test_start_is_supervisor_alias():
    args = parser().parse_args(["start", "--config", "profiles.json"])
    assert args.action == "start" and args.config == Path("profiles.json")
    assert not args.reset_retries


def test_demo_isolated_and_rerunnable_from_another_directory(tmp_path):
    folder = tmp_path / "demo with spaces"
    folder.mkdir()
    marker = folder / "unrelated.txt"
    marker.write_text("keep")
    databases = []
    for _ in range(2):
        process = subprocess.run(
            [sys.executable, "-m", "sdd_runtime.cli", "demo", "--directory", str(folder)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert process.returncode == 0, process.stderr
        result = json.loads(process.stdout)
        assert result["status"] == "accepted" and result["model_calls"] == 0
        assert Path(result["database"]).is_file()
        databases.append(result["database"])
    assert databases[0] != databases[1] and marker.read_text() == "keep"


def test_portable_repository_launcher(tmp_path):
    launcher = Path(__file__).resolve().parents[1] / "ffai.py"
    process = subprocess.run(
        [sys.executable, str(launcher), "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 0 and process.stdout.strip() == "Feature Factory AI 0.1.0"
