"""Convert a paused sdd-orchestrator portfolio into explicit engine tickets.

Pure translation: the caller reads the legacy database and supplies facts
observed on the machine (absolute executables, repository base commits).
Nothing is inferred from status prose. Legacy acceptance is historical and is
recorded as satisfied dependencies only; it never creates an accepted run.
"""

import re
from dataclasses import dataclass

from sdd_core.codec import mapping, sequence, text
from sdd_core.context import ContextRecord, assemble
from sdd_core.models import Json, Workflow
from sdd_workflows.templates import CheckCommand, feature, ticket

# Git's well-known empty tree: "every file is new" for a repository the ticket creates.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
PLACEHOLDER = re.compile(r"\{([a-z_]+):([^}]*)\}")


@dataclass(frozen=True)
class Environment:
    """Machine facts gathered by the IO boundary before translation."""

    executables: dict[str, str]
    heads: dict[str, str]
    language: str = "ru"
    implementer: str = "claude"
    reviewer: str = "codex"
    isolated: bool = True  # each ticket in its own worktree lane
    auto_resolve: bool = True  # merge conflicts go to an agent, not to a human


@dataclass(frozen=True)
class TicketPlan:
    id: str
    legacy_id: str
    title: str
    plan: str
    context: str
    scope: tuple[str, ...]
    dependencies: tuple[str, ...]
    workflow: Workflow
    notes: tuple[str, ...] = ()
    kind: str = "ticket"  # ticket | requirement
    parent: str = ""  # requirement id a ticket was decomposed from


@dataclass(frozen=True)
class PlanInfo:
    id: str
    title: str
    path: str
    requirements: int
    accepted: int
    tickets: int


@dataclass(frozen=True)
class ImportPlan:
    tickets: tuple[TicketPlan, ...]
    skipped: tuple[tuple[str, str], ...]
    satisfied: tuple[str, ...] = ()
    plans: tuple[PlanInfo, ...] = ()


def planning_scope(path: str) -> tuple[str, ...]:
    """A requirement plans in the folder of its plan file, never in code repositories."""
    folder = path.replace("\\", "/").rpartition("/")[0]
    return (folder,) if folder else ()


def plans_folder(paths: list[str]) -> str:
    """The one folder every legacy plan file lives in, or "" when they differ."""
    folders = {path.replace("\\", "/").rpartition("/")[0] for path in paths if path}
    return folders.pop() if len(folders) == 1 else ""


def plan_title(plan: str, path: str) -> str:
    stem = re.sub(r"\.md$", "", path.replace("\\", "/").split("/")[-1])
    words = re.sub(r"^\d+_", "", stem).replace("_PLAN", "").replace("_", " ").strip()
    return f"{plan} · {words.capitalize()}" if words else plan


def run_id(legacy: str) -> str:
    identifier = re.sub(r"[^A-Za-z0-9_-]", "_", legacy)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", identifier):
        raise ValueError(f"Legacy id cannot become a run id: {legacy}")
    return identifier


def quiescent(document: dict[str, Json]) -> None:
    """Refuse translation while the legacy scheduler may still act on the portfolio."""
    if document.get("active") not in (None, {}, []):
        raise ValueError("Legacy portfolio has an active activity; wait for it to settle")
    activities = document.get("activities")
    if isinstance(activities, dict) and activities:
        raise ValueError("Legacy portfolio has running activities; pause it first")
    if document.get("status") == "running" and document.get("paused") is not True:
        raise ValueError("Legacy portfolio is running; pause it before importing")


def _check(raw: Json, env: Environment) -> CheckCommand:
    item = mapping(raw)
    argv = [text(value, "argv") for value in sequence(item.get("argv"))]
    if not argv:
        raise ValueError("Empty check command")
    program = argv[0]
    if program in env.executables:
        argv[0] = env.executables[program]
    elif not re.match(r"^([A-Za-z]:[\\/]|/)", program):
        raise ValueError(f"Check executable is not resolved: {program}")

    def substitute(match: re.Match[str]) -> str:
        kind, value = match.groups()
        if kind != "base":
            raise ValueError(f"Unsupported check placeholder: {match.group(0)}")
        return env.heads.get(value, EMPTY_TREE)

    argv = [argv[0], *(PLACEHOLDER.sub(substitute, value) for value in argv[1:])]
    timeout = item.get("timeout", 900)
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise ValueError("Check timeout must be 1..86400 seconds")
    script = next((a for a in argv[1:] if a.endswith(".py") or "/" in a), argv[0])
    title = re.split(r"[\\/]", script)[-1]
    return CheckCommand(tuple(argv), text(item.get("cwd", "."), "cwd"), timeout, title)


def _lines(label: str, values: Json) -> str:
    if isinstance(values, list):
        body = "\n".join(f"- {text(v, label) if isinstance(v, str) else v}" for v in values)
    else:
        body = str(values)
    return f"{label}:\n{body}" if body.strip() else ""


def translate(document: dict[str, Json], env: Environment) -> ImportPlan:
    quiescent(document)
    units = {key: mapping(value) for key, value in mapping(document.get("units", {})).items()}
    sources = {key: value for key, value in mapping(document.get("sources", {})).items()}
    specs = mapping(document.get("specs", {}))
    decisions = mapping(document.get("decisions", {}))
    allow_commits = document.get("allow_commits") is True

    def source_of(unit: dict[str, Json]) -> tuple[str, ...]:
        contract = mapping(unit["contract"])
        ids = [text(x, "source") for x in sequence(contract.get("source_ids", []))]
        primary = contract.get("source_id")
        return tuple(dict.fromkeys([*([text(primary, "source")] if primary else []), *ids]))

    pending = {key for key, unit in units.items() if unit.get("status") != "accepted"}
    by_source: dict[str, list[str]] = {}
    for key, unit in units.items():
        for source in source_of(unit):
            by_source.setdefault(source, []).append(key)
    # Plan rows that were never decomposed into tickets become requirement runs.
    open_requirements = sorted(
        key
        for key, origin in sources.items()
        if key not in by_source and isinstance(origin, dict) and origin.get("mark") != "x"
    )
    open_set = set(open_requirements)

    skipped: dict[str, str] = {}
    satisfied: set[str] = set()
    requires: dict[str, tuple[str, ...]] = {}
    for key in sorted(pending):
        contract = mapping(units[key]["contract"])
        needed: list[str] = []
        for raw in sequence(contract.get("depends_on", [])):
            dependency = text(raw, "dependency")
            if dependency in units:
                owners = [dependency]
            elif dependency in by_source:
                owners = by_source[dependency]
            elif dependency in open_set:
                needed.append(dependency)
                continue
            else:
                origin = sources.get(dependency)
                if isinstance(origin, dict) and origin.get("mark") == "x":
                    satisfied.add(dependency)
                    continue
                skipped[key] = f"Depends on a requirement without tickets: {dependency}"
                break
            open_owners = [owner for owner in owners if owner in pending]
            if not open_owners:
                satisfied.add(dependency)
            needed.extend(open_owners)
        requires[key] = tuple(dict.fromkeys(needed))

    checks_of: dict[str, tuple[CheckCommand, ...]] = {}
    for key in sorted(pending):
        if key in skipped:
            continue
        try:
            checks_of[key] = tuple(
                _check(raw, env)
                for raw in sequence(mapping(units[key]["contract"]).get("checks", []))
            )
        except ValueError as error:
            skipped[key] = str(error)

    changed = True
    while changed:
        changed = False
        for key, wanted in requires.items():
            if key not in skipped and (bad := [d for d in wanted if d in skipped]):
                skipped[key] = f"Depends on skipped ticket: {bad[0]}"
                changed = True

    order: list[str] = list(open_requirements)
    remaining = sorted(key for key in pending if key not in skipped)
    while remaining:
        ready = [key for key in remaining if set(requires[key]) <= set(order)]
        if not ready:
            raise ValueError("Cyclic legacy ticket dependencies: " + ", ".join(remaining))
        order.extend(ready)
        remaining = [key for key in remaining if key not in ready]

    tickets: list[TicketPlan] = []
    for key in open_requirements:
        tickets.append(_requirement_plan(key, mapping(sources[key]), specs, decisions, env))
    for key in order[len(open_requirements) :]:
        unit = units[key]
        contract = mapping(unit["contract"])
        checks = checks_of[key]
        origin_ids = source_of(unit)
        origin = sources.get(origin_ids[0], {}) if origin_ids else {}
        origin = origin if isinstance(origin, dict) else {}
        plan = str(origin.get("plan", key.split(":")[0]))
        requirement = str(origin.get("requirement", key)).replace("**", "")
        spec: dict[str, Json] = {}
        plan_spec = specs.get(plan)
        if isinstance(plan_spec, dict):
            candidates = plan_spec.get("requirements", {})
            if isinstance(candidates, dict) and origin_ids:
                found = candidates.get(origin_ids[0], {})
                spec = found if isinstance(found, dict) else {}
        repos = tuple(text(x, "repo") for x in sequence(contract.get("repos", [])))
        records = [
            ContextRecord(
                "language",
                "rule",
                "Response language: "
                + ("Russian" if env.language == "ru" else "English")
                + ". Write summaries and explanations in this language; keep protocol keys in English.",
                "",
                "import",
                100,
                True,
            ),
            ContextRecord(
                "ticket",
                "ticket",
                f"Legacy ticket {key} from {origin.get('path', 'plan ' + plan)}.\n"
                f"Requirement: {requirement}\n"
                f"Acceptance (frozen; do not weaken):\n{text(contract.get('acceptance', ''), 'acceptance')}",
                "",
                "legacy-contract",
                90,
                True,
            ),
        ]
        optional = [
            ("criteria", _lines("Criteria", contract.get("criteria", [])), 80),
            ("manual-review", _lines("Manual review", contract.get("manual_review", "")), 80),
            ("repos", _lines("Owned repositories", list(repos)), 85),
            ("paths", _lines("Context paths to read first", contract.get("context", [])), 70),
            ("constraints", _lines("Constraints", spec.get("constraints", [])), 75),
            ("verification", _lines("Verification", spec.get("verification", "")), 60),
            ("out-of-scope", _lines("Out of scope", spec.get("out_of_scope", [])), 60),
            (
                "checks",
                _lines(
                    "Engine checks (run after implementation)", [" ".join(c.argv) for c in checks]
                ),
                65,
            ),
        ]
        decision = next(
            (decisions[s] for s in origin_ids if isinstance(decisions.get(s), dict)), None
        )
        if isinstance(decision, dict) and decision.get("guidance"):
            optional.append(("decisions", "Recorded decisions:\n" + str(decision["guidance"]), 78))
        if unit.get("guidance"):
            optional.append(("guidance", "Previous findings:\n" + str(unit["guidance"]), 72))
        if unit.get("status") == "blocked" and unit.get("problem"):
            optional.append(("blocker", "Legacy blocker to resolve:\n" + str(unit["problem"]), 74))
        records.extend(
            ContextRecord(name, name, body, "", "legacy-contract", priority)
            for name, body, priority in optional
            if body
        )
        workflow = ticket(
            checks,
            env.implementer,
            env.reviewer,
            allow_commits=allow_commits,
            isolated=env.isolated,
            auto_resolve=env.auto_resolve,
        )
        context, omitted = assemble(tuple(records), "", workflow.max_input_chars - 4000)
        notes = []
        if omitted:
            notes.append("Context omitted: " + ", ".join(omitted))
        missing = [repo for repo in repos if repo not in env.heads]
        if missing:
            notes.append("Repositories created by this ticket: " + ", ".join(missing))
        tickets.append(
            TicketPlan(
                run_id(key),
                key,
                requirement[:200],
                plan,
                context,
                repos,
                tuple(run_id(d) for d in requires[key]),
                workflow,
                tuple(notes),
                "ticket",
                run_id(origin_ids[0]) if origin_ids else "",
            )
        )
    identifiers = [plan.id for plan in tickets]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Legacy ids collide after normalization")
    plans = []
    for plan_id in dict.fromkeys(
        [
            *(str(x) for x in sequence(document.get("plans", []))),
            *(str(mapping(o).get("plan", "")) for o in sources.values() if isinstance(o, dict)),
        ]
    ):
        rows = [
            mapping(o)
            for o in sources.values()
            if isinstance(o, dict) and str(o.get("plan")) == plan_id
        ]
        if not plan_id or not rows:
            continue
        path = str(rows[0].get("path", ""))
        plans.append(
            PlanInfo(
                plan_id,
                plan_title(plan_id, path),
                path,
                len(rows),
                sum(o.get("mark") == "x" for o in rows),
                sum(1 for key in units if key.split(":")[0] == plan_id),
            )
        )
    return ImportPlan(
        tuple(tickets),
        tuple(sorted(skipped.items())),
        tuple(sorted(satisfied)),
        tuple(sorted(plans, key=lambda p: p.id)),
    )


def _requirement_plan(
    key: str,
    origin: dict[str, Json],
    specs: dict[str, Json],
    decisions: dict[str, Json],
    env: Environment,
) -> TicketPlan:
    plan = str(origin.get("plan", key.split(":")[0]))
    title = str(origin.get("requirement", key)).replace("**", "").strip()
    detail = "\n".join(str(line) for line in sequence(origin.get("detail", [])))
    spec: dict[str, Json] = {}
    plan_spec = specs.get(plan)
    if isinstance(plan_spec, dict) and isinstance(plan_spec.get("requirements"), dict):
        found = mapping(plan_spec["requirements"]).get(key, {})
        spec = found if isinstance(found, dict) else {}
    records = [
        ContextRecord(
            "language",
            "rule",
            "Response language: "
            + ("Russian" if env.language == "ru" else "English")
            + ". Write summaries and explanations in this language; keep protocol keys in English.",
            "",
            "import",
            100,
            True,
        ),
        ContextRecord(
            "requirement",
            "requirement",
            f"Plan requirement {key} from {origin.get('path', 'plan ' + plan)}.\n{title}",
            "",
            "legacy-plan",
            90,
            True,
        ),
    ]
    optional = [
        ("detail", _lines("Plan notes", detail), 80),
        ("acceptance", _lines("Recorded acceptance draft", spec.get("acceptance", "")), 78),
        ("constraints", _lines("Constraints", spec.get("constraints", [])), 75),
        ("verification", _lines("Verification", spec.get("verification", "")), 60),
        ("out-of-scope", _lines("Out of scope", spec.get("out_of_scope", [])), 60),
    ]
    decision = decisions.get(key)
    if isinstance(decision, dict) and decision.get("guidance"):
        optional.append(("decisions", "Recorded decisions:\n" + str(decision["guidance"]), 70))
    records.extend(
        ContextRecord(name, name, body, "", "legacy-plan", priority)
        for name, body, priority in optional
        if body
    )
    workflow = feature(env.reviewer)
    context, _ = assemble(tuple(records), "", workflow.max_input_chars - 4000)
    return TicketPlan(
        run_id(key),
        key,
        title[:200],
        plan,
        context,
        planning_scope(str(origin.get("path", ""))),
        (),
        workflow,
        (),
        "feature",
    )
