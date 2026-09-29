"""Installed versions of the engine's libraries.

Every library is released with the same version and pins its siblings exactly,
so one number names the engine. A mixed installation (a stale wheel beside a
new one) is reported instead of hidden.
"""

from importlib.metadata import PackageNotFoundError, version

PACKAGES = (
    "sdd-core",
    "sdd-storage",
    "sdd-runtime",
    "sdd-providers",
    "sdd-workflows",
    "sdd-usage",
    "sdd-factory",
    "sdd-trackers",
    "sdd-ui",
    "feature-factory-ai",
)


def installed() -> dict[str, str | None]:
    """Version of each library, or None when it is not installed."""
    found: dict[str, str | None] = {}
    for name in PACKAGES:
        try:
            found[name] = version(name)
        except PackageNotFoundError:
            found[name] = None
    return found


def engine() -> str:
    """The engine version: sdd-runtime, which every composition installs."""
    return installed()["sdd-runtime"] or "unknown"


def consistent(found: dict[str, str | None]) -> bool:
    return len({v for v in found.values() if v is not None}) <= 1
