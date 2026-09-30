"""Plan documents: a project's numbered Markdown plans and their requirement rows.

A plan is a numbered file such as `plans/110_RALLY_UPDATE.md`: a title, a
preamble (goals, rules such as "research first", findings) and rows

    - [ ] **FH-07** — requirement text
          continuation lines with acceptance and notes

The mark is the plan author's status: `x` done, `~` partially done, a space for
open and `-` for rejected. A plan is imported as a draft over its open and partial
rows; research may add rows to the file, and the next import takes those as a
follow-up draft. Parsing is pure; `MarkdownPlans` reads the folder.
"""

import re
from pathlib import Path

from sdd_core.tracking import WorkItem, WorkRow

NAME = re.compile(r"^(\d{3})_(.+)\.md$")
ROW = re.compile(r"^- \[(.)\] \*\*([A-Za-z]+\d*-\d+)\*\*\s*(?:[—–-]\s*)?(.*)$")
PREAMBLE_CHARS = 6000


def plan_number(name: str) -> str | None:
    match = NAME.match(name)
    return match.group(1) if match else None


def parse_plan(path: str, text: str) -> WorkItem:
    """Read one plan; `path` is relative to the workspace and names the number."""
    name = path.replace("\\", "/").split("/")[-1]
    number = plan_number(name)
    if number is None:
        raise ValueError(f"Plan file name must start with a three-digit number: {name}")
    lines = text.lstrip("﻿").splitlines()
    heading = next((line[2:].strip() for line in lines if line.startswith("# ")), "")
    words = NAME.match(name)
    fallback = words.group(2).replace("_PLAN", "").replace("_", " ").capitalize() if words else ""
    title = f"{number} · {heading or fallback}"
    rows: list[WorkRow] = []
    preamble: list[str] = []
    seen: set[str] = set()
    current: list[str] | None = None
    for line in lines:
        match = ROW.match(line)
        if match:
            mark, identifier, body = match.groups()
            if identifier in seen:
                raise ValueError(f"Duplicate row {number}:{identifier}")
            seen.add(identifier)
            current = []
            rows.append(WorkRow(identifier, mark, body.strip()))
        elif current is not None and line.startswith("      "):
            current.append(line.strip())
            rows[-1] = WorkRow(rows[-1].id, rows[-1].mark, rows[-1].text, tuple(current))
        else:
            current = None
            if not rows:
                preamble.append(line)
    return WorkItem(
        number,
        title,
        "\n".join(preamble).strip()[:PREAMBLE_CHARS],
        tuple(rows),
        path=path.replace("\\", "/"),
    )


class MarkdownPlans:
    """A work source over a folder of numbered Markdown plans in the workspace."""

    def __init__(self, folder: str) -> None:
        if not folder:
            raise ValueError("The project has no plans folder; set it in the project settings")
        self.folder = folder

    def items(self, workspace: str) -> list[WorkItem]:
        """Numbered plan files with rows; reviews and notes without rows are not plans."""
        root = Path(workspace)
        folder = root / self.folder
        if not folder.is_dir():
            raise ValueError(f"The project has no plan folder {self.folder}")
        documents = [
            parse_plan(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8-sig"))
            for path in sorted(folder.glob("[0-9][0-9][0-9]_*.md"))
        ]
        plans = [document for document in documents if document.rows]
        numbers = [plan.key for plan in plans]
        if duplicate := sorted({n for n in numbers if numbers.count(n) > 1}):
            raise ValueError("Several plan files share a number: " + ", ".join(duplicate))
        return plans
