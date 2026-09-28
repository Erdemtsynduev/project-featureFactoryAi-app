"""Check package imports, pure core and declared dependency directions."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    "ui": {"ui", "core", "runtime", "storage", "providers", "workflows"},
    "core": {"core"},
    "storage": {"core", "storage"},
    "runtime": {"core", "runtime"},
    "providers": {"core", "providers"},
    "workflows": {"core", "workflows"},
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
errors = []
for package, allowed in ALLOWED.items():
    for path in (ROOT / "packages" / package / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                package == "runtime"
                and isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"execute", "executemany", "executescript", "transaction"}
            ):
                errors.append(
                    f"{path}:{node.lineno}: persistence implementation leaked into runtime"
                )
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
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
                if package == "core" and module in FORBIDDEN:
                    errors.append(f"{path}:{node.lineno}: IO dependency in core: {module}")
                if module.startswith("sdd_") and module[4:] not in allowed:
                    if package == "runtime" and (
                        (
                            path.name == "cli.py"
                            and module in {"sdd_providers", "sdd_workflows", "sdd_storage"}
                        )
                        or (path.name == "engine.py" and module == "sdd_storage")
                    ):
                        continue
                    errors.append(f"{path}:{node.lineno}: forbidden dependency: {module}")
if errors:
    raise SystemExit("\n".join(errors))
print("Package boundaries and core purity: PASS")
