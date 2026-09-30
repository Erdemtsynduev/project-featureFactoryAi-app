"""Interface copy: every key the code or markup uses exists in both languages."""

import re
from pathlib import Path

STATIC = Path(__file__).parents[1] / "packages/ui/src/sdd_ui/static"
KEY = re.compile(r'^\s*"([\w.\-]+)":', re.MULTILINE)


def dictionary(name: str) -> set[str]:
    return set(KEY.findall((STATIC / "js/i18n" / name).read_text(encoding="utf-8")))


def used() -> set[str]:
    keys: set[str] = set()
    for path in (STATIC / "js").rglob("*.js"):
        if "i18n" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        keys |= set(re.findall(r'\bt\(\s*"([\w.\-]+)"\s*[,)]', source))
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    keys |= set(re.findall(r'data-i18n(?:-[\w-]+)?="([\w.\-]+)"', markup))
    return keys


def test_languages_share_every_key():
    russian, english = dictionary("ru.js"), dictionary("en.js")
    assert russian - english == set()
    assert english - russian == set()


def test_every_literal_key_is_defined():
    assert used() - dictionary("ru.js") == set()


def test_dynamic_key_families_are_complete():
    russian = dictionary("ru.js")
    families = {
        "attention.": [
            "accepted",
            "answer",
            "working",
            "limits_exhausted",
            "uncertain",
            "blocked",
            "no_profile",
            "profile_resting",
            "waiting",
            "paused",
            "dependencies",
            "queue_paused",
            "paths_held",
            "queued",
            "delivered",
            "closed",
            "children_need",
            "delivering",
            "delivery_waiting",
            "partial",
            "delivery_paused",
        ],
        "tone.": ["done", "working", "waiting", "attention", "blocked", "idle"],
        "board.lane.": ["queue", "running", "needs", "done"],
        "board.empty.": ["queue", "running", "needs", "done"],
        "command.done.": ["resume", "start", "pause", "stop", "retry", "recover"],
        "board.mode.": ["tree", "live", "plans"],
        "board.modeHint.": ["tree", "live", "plans"],
        "flows.mode.": ["view", "edit"],
        "intent.": [
            f"{i}.{p}"
            for i in ("feature", "main-flow", "ticket", "custom")
            for p in ("title", "text", "path")
        ],
        "help.": [
            f"{s}.{p}"
            for s in ("flows", "queue", "answers", "stuck", "limits", "usage", "memory", "log")
            for p in ("title", "text")
        ],
        "welcome.": [f"{s}.{p}" for s in ("agents", "project", "task") for p in ("title", "text")],
        "project.role.": ["lead", "analyst", "implementer", "reviewer"],
        "project.roleHint.": ["lead", "analyst", "implementer", "reviewer"],
        "role.": ["replan"],
        "step.": ["replan", "decide"],
        "nav.": ["overview", "board", "team", "flows", "agents", "usage", "journal"],
        "template.": ["main-flow", "feature", "ticket", "approved-feature", "interview", "demo"],
    }
    missing = {prefix + name for prefix, names in families.items() for name in names} - russian
    assert missing == set()


def test_old_translation_by_dom_rewrite_is_gone():
    assert not (STATIC / "locale.js").exists()
    for path in (STATIC / "js").rglob("*.js"):
        assert "MutationObserver" not in path.read_text(encoding="utf-8"), path
