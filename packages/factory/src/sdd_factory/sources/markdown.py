"""Plan documents: a project's numbered Markdown plans and their requirement rows.

A plan is a file such as `.kimi-plans/110_RALLY_FH4_UPDATE_PLAN.md`: a title, a
preamble (goals, rules such as "research first", findings) and rows

    - [ ] **FH-07** — requirement text
          continuation lines with acceptance and notes

The mark is the plan author's status: `x` done, `~` partially done, a space for
open and `-` for rejected. A plan is the source of one feature whose scope is its
open and partial rows; research rows may add new rows to the file, so plans are
re-read, never imported once. Parsing is pure; `MarkdownPlans` reads the folder.
"""

import re
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"^(\d{3})_(.+)\.md$")
ROW = re.compile(r"^- \[(.)\] \*\*([A-Za-z]+\d*-\d+)\*\*\s*(?:[—–-]\s*)?(.*)$")
OPEN_MARKS = (" ", "~")
PREAMBLE_CHARS = 6000


@dataclass(frozen=True)
class PlanRow:
    id: str
    mark: str  # "x" done, "~" partial, " " open, "-" rejected
    text: str
    detail: tuple[str, ...] = ()

    @property
    def open(self) -> bool:
        return self.mark in OPEN_MARKS

    @property
    def title(self) -> str:
        return f"{self.id} — {self.text}" if self.text else self.id


@dataclass(frozen=True)
class PlanDocument:
    number: str
    title: str
    path: str
    preamble: str
    rows: tuple[PlanRow, ...]

    def counts(self) -> dict[str, int]:
        return {
            "requirements": len(self.rows),
            "accepted": sum(row.mark == "x" for row in self.rows),
            "partial": sum(row.mark == "~" for row in self.rows),
            "open": sum(row.mark == " " for row in self.rows),
            "rejected": sum(row.mark == "-" for row in self.rows),
        }


def plan_number(name: str) -> str | None:
    match = NAME.match(name)
    return match.group(1) if match else None


def parse_plan(path: str, text: str) -> PlanDocument:
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
    rows: list[PlanRow] = []
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
            rows.append(PlanRow(identifier, mark, body.strip()))
        elif current is not None and line.startswith("      "):
            current.append(line.strip())
            rows[-1] = PlanRow(rows[-1].id, rows[-1].mark, rows[-1].text, tuple(current))
        else:
            current = None
            if not rows:
                preamble.append(line)
    return PlanDocument(
        number,
        title,
        path.replace("\\", "/"),
        "\n".join(preamble).strip()[:PREAMBLE_CHARS],
        tuple(rows),
    )


def row_run_id(number: str, row: PlanRow) -> str:
    """The run id the per-row import gave a row, e.g. `110_FH-07`."""
    return f"{number}_{row.id}"


class MarkdownPlans:
    """A `FeatureSource` over a folder of numbered Markdown plans in the workspace."""

    def __init__(self, folder: str = ".kimi-plans") -> None:
        self.folder = folder

    def documents(self, workspace: Path) -> list[PlanDocument]:
        """Numbered plan files with rows; reviews and notes without rows are not plans."""
        folder = workspace / self.folder
        if not folder.is_dir():
            raise ValueError(f"The project has no plan folder {self.folder}")
        documents = [
            parse_plan(path.relative_to(workspace).as_posix(), path.read_text(encoding="utf-8-sig"))
            for path in sorted(folder.glob("[0-9][0-9][0-9]_*.md"))
        ]
        plans = [document for document in documents if document.rows]
        numbers = [plan.number for plan in plans]
        if duplicate := sorted({n for n in numbers if numbers.count(n) > 1}):
            raise ValueError("Several plan files share a number: " + ", ".join(duplicate))
        return plans
