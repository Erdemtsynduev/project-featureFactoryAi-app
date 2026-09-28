"""Load only named, installed extensions requested by operator configuration."""

from importlib.metadata import entry_points
from typing import cast

from sdd_core.execution import ExecutionBackend
from sdd_core.sdk import Handler, Registry


def load_extensions(registry: Registry, allowed: tuple[str, ...]) -> None:
    installed = {point.name: point for point in entry_points(group="sdd.handlers")}
    for name in allowed:
        if name not in installed:
            raise ValueError(f"Extension not installed: {name}")
        factory = installed[name].load()
        handler = cast(Handler, factory())
        registry.register(handler)


def load_executor(name: str) -> ExecutionBackend:
    """Only explicit application configuration may select an installed executor."""
    installed = {point.name: point for point in entry_points(group="sdd.executors")}
    if name not in installed:
        raise ValueError(f"Executor not installed: {name}")
    executor = cast(ExecutionBackend, installed[name].load()())
    if not isinstance(executor.id, str) or not executor.id:
        raise ValueError("Executor must have a stable identity")
    for method in ("start", "reconcile", "cancel"):
        if not callable(getattr(executor, method, None)):
            raise ValueError(f"Executor lacks {method}")
    return executor
