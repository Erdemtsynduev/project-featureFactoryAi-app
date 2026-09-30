import os
import time
from pathlib import Path

import pytest
from sdd_runtime.interpreters import contained_path, launched_by
from sdd_runtime.platform import Job
from sdd_runtime.sandbox import Sandbox

ALIASES = Path(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps")
INSTALL_MANAGER = ALIASES / "python.exe"
# The alias exists on every Windows, but without the Python install manager it is the
# Store's stub, which starts no interpreter: only a resolving alias is the real thing.
needs_install_manager = pytest.mark.skipif(
    os.name != "nt" or not INSTALL_MANAGER.is_file() or launched_by(str(INSTALL_MANAGER)) is None,
    reason="Python install manager alias",
)


@pytest.mark.skipif(os.name != "nt", reason="Windows app execution aliases")
def test_alias_folders_are_preceded_by_the_interpreters_they_launch(tmp_path):
    aliases = tmp_path / "WindowsApps"
    aliases.mkdir()
    (aliases / "python.exe").write_bytes(b"")
    real = tmp_path / "pythoncore"
    other = tmp_path / "tools"
    path = os.pathsep.join([str(other), str(aliases), str(other)])
    asked = []

    def resolve(alias):
        asked.append(Path(alias).name)
        return str(real)

    assert contained_path(path, resolve) == os.pathsep.join([str(other), str(real), str(aliases)])
    assert asked == ["python.exe"]
    # Nothing to resolve: the PATH stays as it was, minus repeats.
    assert contained_path(str(other), resolve) == str(other)


@needs_install_manager
def test_the_alias_resolves_to_an_interpreter_whose_children_stay_in_the_job(tmp_path):
    folder = launched_by(str(INSTALL_MANAGER))
    assert folder is not None and Path(folder, "python.exe").is_file()
    started = tmp_path / "started.txt"
    code = (
        "import subprocess, sys, time;"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
        "open(sys.argv[1], 'w').write('started'); time.sleep(30)"
    )
    aggregate = Job()
    sandbox = Sandbox(tmp_path / "attempt", aggregate)
    root = sandbox.start([str(Path(folder, "python.exe")), "-c", code, str(started)])
    try:
        deadline = time.monotonic() + 15
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        sandbox.observe()
        assert len(sandbox.lineage.known) >= 3 and not sandbox.escaped
    finally:
        sandbox.end()
        sandbox.close()
        aggregate.close()
        root.wait(timeout=5)
