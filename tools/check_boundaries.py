"""Check package imports, pure core and declared dependency directions."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    "ui": {"ui", "factory", "core", "runtime", "providers", "workflows", "usage"},
    "factory": {"factory", "core", "runtime", "workflows"},
    "usage": {"core", "usage"},
    "core": {"core"},
    "storage": {"core", "storage"},
    "runtime": {"core", "runtime"},
    "providers": {"core", "providers"},
    "trackers": {"core", "trackers"},
    "workflows": {"core", "workflows"},
}
# Composition roots may choose concrete adapters; nothing else may.
COMPOSITION = {
    ("runtime", "composition.py"): {"sdd_providers", "sdd_storage"},
    ("runtime", "cli.py"): {"sdd_providers", "sdd_workflows", "sdd_storage"},
    ("runtime", "engine.py"): {"sdd_storage"},
}
FORBIDDEN = {
    "os",
    "pathlib",
    "subprocess",
    "sqlite3",
    "time",
    "random",
    "uuid",
    "socket",
    "threading",
    "asyncio",
}
SQL_CALLS = {"execute", "executemany", "executescript", "transaction"}
errors = []
for package, allowed in ALLOWED.items():
    for path in (ROOT / "packages" / package / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = (package, path.name)
        for node in ast.walk(tree):
            if (
                package in ("runtime", "ui", "factory")
                and isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in SQL_CALLS
            ):
                errors.append(
                    f"{path}:{node.lineno}: persistence implementation leaked into {package}"
                )
            if (
                package != "core"
                and isinstance(node, ast.Attribute)
                and node.attr == "changed"
                and isinstance(node.value, ast.Name)
                and node.value.id == "machine"
            ):
                errors.append(f"{path}:{node.lineno}: use a named transition, not machine.changed")
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
                if (
                    package != "core"
                    and node.module == "sdd_core.machine"
                    and any(alias.name == "changed" for alias in node.names)
                ):
                    errors.append(f"{path}:{node.lineno}: use a named transition, not changed()")
            for name in names:
                module = name.split(".")[0]
                if path.name == "application.py" and module in {
                    "sqlite3",
                    "sdd_storage",
                    "sdd_providers",
                    "subprocess",
                }:
                    errors.append(
                        f"{path}:{node.lineno}: concrete infrastructure in application: {module}"
                    )
                if package in ("core", "usage") and module in FORBIDDEN:
                    errors.append(f"{path}:{node.lineno}: IO dependency in {package}: {module}")
                if package in ("ui", "factory") and module == "sqlite3":
                    errors.append(f"{path}:{node.lineno}: SQL in {package}; use a storage port")
                if package in ("ui", "factory") and name == "sdd_runtime.cli":
                    errors.append(f"{path}:{node.lineno}: compose through sdd_runtime.composition")
                if module.startswith("sdd_") and module[4:] not in allowed:
                    if module in COMPOSITION.get(where, set()):
                        continue
                    errors.append(f"{path}:{node.lineno}: forbidden dependency: {module}")
if errors:
    raise SystemExit("\n".join(errors))
print("Package boundaries and core purity: PASS")
