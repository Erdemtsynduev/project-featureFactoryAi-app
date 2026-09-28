"""All libraries share one version and pin their siblings to it."""

import re
import tomllib
from pathlib import Path

from sdd_runtime.versions import PACKAGES, consistent, engine, installed

ROOT = Path(__file__).parents[1]
PROJECTS = [ROOT / "pyproject.toml", *sorted((ROOT / "packages").glob("*/pyproject.toml"))]


def test_every_library_declares_the_same_version_and_exact_sibling_pins():
    documents = [tomllib.loads(path.read_text(encoding="utf-8")) for path in PROJECTS]
    versions = {doc["project"]["name"]: doc["project"]["version"] for doc in documents}
    assert set(versions) == set(PACKAGES)
    release = versions["sdd-runtime"]
    assert set(versions.values()) == {release}
    for doc in documents:
        pins = [*doc["project"].get("dependencies", [])]
        for extra in doc["project"].get("optional-dependencies", {}).values():
            pins += extra
        for pin in pins:
            match = re.match(r"(sdd-[a-z]+|feature-factory-ai)(\[[a-z]+\])?==(.+)", pin)
            if match:
                assert match.group(3) == release, pin


def test_installed_versions_are_reported_and_consistent():
    found = installed()
    assert engine() == found["sdd-runtime"]
    assert consistent(found)
    assert not consistent({"a": "1", "b": "2"}) and consistent({"a": "1", "b": None})
