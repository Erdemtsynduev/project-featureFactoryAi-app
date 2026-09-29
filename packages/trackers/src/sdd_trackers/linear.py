"""Linear as a work source and a mirror of the factory's progress.

Settings (a project's `tracker`), never a token:

    {"kind": "linear", "team": "ENG", "source": "projects",
     "label": "factory", "token_env": "LINEAR_API_KEY",
     "states": {"needs_person": "In Review", "blocked": "Blocked"}}

Work items:

- `source: "projects"` (default): each unfinished project of the team is a feature;
  its top-level issues are the rows.
- `source: "issues"`: each unfinished issue with the `label` is a feature; its
  sub-issues are the rows.

A row's mark comes from its issue's state type: completed `x`, canceled `-`,
started `~`, anything else open.

Mirror:

- the approved specification: a comment on the issue, or a project update;
- the approved tickets: issues in the project (or sub-issues of the issue), with
  "blocks" relations along their dependencies;
- states: an issue moves to the workflow state of its mirror state (named in
  `states`, else the first state of the matching type); a project gets an update
  whose health says on track, at risk (needs a person) or off track (blocked).

Every publication carries a marker (`ffai:<id>`, `ffai-run:<run>`) and delivery
checks for it first, so a repeated delivery never duplicates a comment or an issue.
"""

import os
from collections.abc import Mapping
from typing import Any

from sdd_core.codec import mapping, text
from sdd_core.models import Json
from sdd_core.tracking import TicketMirror, TrackerReceipt, TrackerUpdate, WorkItem, WorkRow

from sdd_trackers.graphql import GraphQL, TrackerError, Transport

ENDPOINT = "https://api.linear.app/graphql"
PAGE = 50
BODY_CHARS = 50000
DETAIL_LINES = 12
MARKS = {"completed": "x", "canceled": "-", "started": "~"}
# The workflow state type a mirror state moves an issue to, unless `states` names one.
STATE_TYPES = {
    "queued": "unstarted",
    "working": "started",
    "needs_person": "started",
    "blocked": "started",
    "done": "completed",
}
HEALTH = {"blocked": "offTrack", "needs_person": "atRisk"}

TEXT = {
    "ru": {
        "specification": "Спецификация, утверждённая в Feature Factory",
        "after": "После",
        "wave": "Волна",
        "hitl": "Нужен человек: в этом тикете решение или проверка человека.",
        "acceptance": "Приёмка",
        "queued": "В очереди",
        "working": "В работе",
        "needs_person": "Нужен человек",
        "blocked": "Заблокировано",
        "done": "Готово",
        "truncated": "Текст сокращён; полная версия — в Feature Factory.",
    },
    "en": {
        "specification": "Specification approved in Feature Factory",
        "after": "After",
        "wave": "Wave",
        "hitl": "Needs a person: a decision or check by a person is part of this ticket.",
        "acceptance": "Acceptance",
        "queued": "Queued",
        "working": "In progress",
        "needs_person": "Needs a person",
        "blocked": "Blocked",
        "done": "Done",
        "truncated": "Shortened; the full text is in Feature Factory.",
    },
}

TEAM = """query($key: String!) { teams(filter: {key: {eq: $key}}) { nodes {
  id key states { nodes { id name type position } } } } }"""

ISSUE_FIELDS = "id identifier title description url state { type }"

PROJECTS = f"""query($team: String!, $after: String) {{ team(id: $team) {{
  projects(first: {PAGE}, after: $after) {{
    nodes {{ id name slugId url description content completedAt canceledAt
      issues(first: 250) {{ nodes {{ {ISSUE_FIELDS} parent {{ id }} }} }} }}
    pageInfo {{ hasNextPage endCursor }} }} }} }}"""

LABELLED = f"""query($team: ID!, $label: String!, $after: String) {{
  issues(first: {PAGE}, after: $after,
    filter: {{team: {{id: {{eq: $team}}}}, labels: {{name: {{eq: $label}}}}}}) {{
    nodes {{ {ISSUE_FIELDS} children(first: 250) {{ nodes {{ {ISSUE_FIELDS} }} }} }}
    pageInfo {{ hasNextPage endCursor }} }} }}"""

ISSUE_COMMENTS = """query($id: String!) { issue(id: $id) { id state { id }
  comments(last: 100) { nodes { body } } } }"""
PROJECT_UPDATES = """query($id: String!) { project(id: $id) { id
  projectUpdates(last: 100) { nodes { body } } } }"""
CHILDREN = """query($id: String!) { issue(id: $id) { children(first: 250) {
  nodes { id description } } } }"""
PROJECT_ISSUES = """query($id: String!) { project(id: $id) { issues(first: 250) {
  nodes { id description } } } }"""

COMMENT = """mutation($input: CommentCreateInput!) { commentCreate(input: $input) {
  success } }"""
PROJECT_UPDATE = """mutation($input: ProjectUpdateCreateInput!) {
  projectUpdateCreate(input: $input) { success } }"""
CREATE_ISSUE = """mutation($input: IssueCreateInput!) { issueCreate(input: $input) {
  success issue { id } } }"""
UPDATE_ISSUE = """mutation($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success } }"""
RELATE = """mutation($input: IssueRelationCreateInput!) {
  issueRelationCreate(input: $input) { success } }"""


class LinearTracker:
    id = "linear"

    def __init__(
        self,
        settings: dict[str, Json],
        transport: Transport | None = None,
        environ: Mapping[str, str] = os.environ,
    ) -> None:
        self.team_key = text(settings.get("team"), "team").strip()
        if not self.team_key:
            raise ValueError("Linear needs the team key, e.g. ENG")
        self.source = text(settings.get("source", "projects"), "source")
        if self.source not in ("projects", "issues"):
            raise ValueError("Linear source must be projects or issues")
        self.label = text(settings.get("label", "factory"), "label")
        self.states = {k: text(v, "state") for k, v in mapping(settings.get("states", {})).items()}
        language = text(settings.get("language", "en"), "language")
        self.words = TEXT.get(language, TEXT["en"])
        variable = text(settings.get("token_env", "") or "LINEAR_API_KEY", "token_env")

        def headers() -> dict[str, str]:
            token = environ.get(variable, "")
            if not token:
                raise TrackerError(f"Set the Linear API key in the environment variable {variable}")
            return {"Authorization": token}

        self.call: Transport = transport or GraphQL(ENDPOINT, headers)
        self._team: dict[str, Any] | None = None

    # Work items ------------------------------------------------------------------

    def items(self, workspace: str) -> list[WorkItem]:
        team = self._team_record()
        if self.source == "issues":
            return [self._issue_item(node) for node in self._labelled(team["id"])]
        return [
            self._project_item(node)
            for node in self._pages(PROJECTS, {"team": team["id"]}, ("team", "projects"))
            if not node.get("completedAt") and not node.get("canceledAt")
        ]

    def _project_item(self, node: dict[str, Any]) -> WorkItem:
        issues = [i for i in node["issues"]["nodes"] if not i.get("parent")]
        return WorkItem(
            key=str(node["slugId"]),
            title=str(node["name"]),
            body=str(node.get("content") or node.get("description") or ""),
            rows=tuple(row(issue) for issue in issues),
            link=f"linear:project:{node['id']}",
            url=str(node.get("url") or ""),
        )

    def _issue_item(self, node: dict[str, Any]) -> WorkItem:
        return WorkItem(
            key=str(node["identifier"]),
            title=f"{node['identifier']} · {node['title']}",
            body=str(node.get("description") or ""),
            rows=tuple(row(child) for child in node["children"]["nodes"]),
            link=f"linear:issue:{node['id']}",
            url=str(node.get("url") or ""),
        )

    def _labelled(self, team: str) -> list[dict[str, Any]]:
        nodes = self._pages(LABELLED, {"team": team, "label": self.label}, ("issues",))
        return [n for n in nodes if n["state"]["type"] not in ("completed", "canceled")]

    def _pages(
        self, query: str, variables: dict[str, object], path: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data: Any = self.call(query, {**variables, "after": after})
            for key in path:
                data = data[key]
            found += data["nodes"]
            if not data["pageInfo"]["hasNextPage"]:
                return found
            after = data["pageInfo"]["endCursor"]

    def _team_record(self) -> dict[str, Any]:
        if self._team is None:
            nodes = self.call(TEAM, {"key": self.team_key})["teams"]["nodes"]
            if not nodes:
                raise TrackerError(f"No Linear team with key {self.team_key}")
            self._team = nodes[0]
        return self._team

    # Mirror ------------------------------------------------------------------------

    def publish(self, update: TrackerUpdate) -> TrackerReceipt:
        kind, identifier = parse_link(update.link)
        marker = f"ffai:{update.id}"
        if update.kind == "specification":
            body = self._body(f"## {self.words['specification']}\n\n{update.text}", marker)
            self._post(kind, identifier, body, marker, "onTrack")
            return TrackerReceipt()
        if update.kind == "tickets":
            return self._tickets(kind, identifier, update.tickets)
        return self._state(kind, identifier, update, marker)

    def _post(self, kind: str, identifier: str, body: str, marker: str, health: str) -> None:
        """A comment on an issue or an update of a project, once per marker."""
        if kind == "issue":
            issue = self.call(ISSUE_COMMENTS, {"id": identifier})["issue"]
            if not any(marker in (c.get("body") or "") for c in issue["comments"]["nodes"]):
                self.call(COMMENT, {"input": {"issueId": identifier, "body": body}})
            return
        project = self.call(PROJECT_UPDATES, {"id": identifier})["project"]
        if not any(marker in (u.get("body") or "") for u in project["projectUpdates"]["nodes"]):
            self.call(
                PROJECT_UPDATE, {"input": {"projectId": identifier, "body": body, "health": health}}
            )

    def _tickets(
        self, kind: str, identifier: str, tickets: tuple[TicketMirror, ...]
    ) -> TrackerReceipt:
        if kind == "issue":
            existing = self.call(CHILDREN, {"id": identifier})["issue"]["children"]["nodes"]
        else:
            existing = self.call(PROJECT_ISSUES, {"id": identifier})["project"]["issues"]["nodes"]
        known = {
            run: node["id"]
            for node in existing
            for run in [run_marker(node.get("description") or "")]
            if run
        }
        team = self._team_record()
        queued = self._state_id("queued")
        created: set[str] = set()
        issues: dict[str, str] = {}
        for ticket in tickets:
            if ticket.run in known:
                issues[ticket.run] = known[ticket.run]
                continue
            fields: dict[str, object] = {
                "teamId": team["id"],
                "title": f"{ticket.key} · {ticket.title}",
                "description": self._ticket_body(ticket),
            }
            fields["parentId" if kind == "issue" else "projectId"] = identifier
            if queued:
                fields["stateId"] = queued
            result = self.call(CREATE_ISSUE, {"input": fields})["issueCreate"]
            if not result.get("success") or not result.get("issue"):
                raise TrackerError(f"Linear did not create the issue for {ticket.key}")
            issues[ticket.run] = result["issue"]["id"]
            created.add(ticket.run)
        by_key = {t.key: issues[t.run] for t in tickets}
        for ticket in tickets:
            if ticket.run not in created:
                continue  # relations were made when the issue was created
            for needed in ticket.depends_on:
                if needed in by_key:
                    self.call(
                        RELATE,
                        {
                            "input": {
                                "issueId": by_key[needed],
                                "relatedIssueId": issues[ticket.run],
                                "type": "blocks",
                            }
                        },
                    )
        return TrackerReceipt(tuple((run, f"linear:issue:{i}") for run, i in issues.items()))

    def _state(
        self, kind: str, identifier: str, update: TrackerUpdate, marker: str
    ) -> TrackerReceipt:
        label = self.words.get(update.state, update.state)
        note = f"**{label}**" + (f"\n\n{update.text}" if update.text else "")
        if kind == "project":
            self._post(
                kind,
                identifier,
                self._body(note, marker),
                marker,
                HEALTH.get(update.state, "onTrack"),
            )
            return TrackerReceipt()
        target = self._state_id(update.state)
        issue = self.call(ISSUE_COMMENTS, {"id": identifier})["issue"]
        if target and issue["state"]["id"] != target:
            self.call(UPDATE_ISSUE, {"id": identifier, "input": {"stateId": target}})
        if update.state in ("needs_person", "blocked"):
            self._post(kind, identifier, self._body(note, marker), marker, "")
        return TrackerReceipt()

    def _state_id(self, state: str) -> str:
        """The workflow state for a mirror state: named in settings, else by type."""
        nodes = sorted(self._team_record()["states"]["nodes"], key=lambda n: n["position"])
        named = self.states.get(state)
        if named:
            match = next((n for n in nodes if n["name"] == named), None)
            if match is None:
                raise TrackerError(f"No workflow state named {named} in team {self.team_key}")
            return str(match["id"])
        wanted = STATE_TYPES.get(state, "")
        return next((str(n["id"]) for n in nodes if n["type"] == wanted), "")

    def _ticket_body(self, ticket: TicketMirror) -> str:
        lines = [ticket.goal, ""] if ticket.goal else []
        if ticket.hitl:
            lines += [f"> {self.words['hitl']}", ""]
        if ticket.acceptance:
            lines += [
                f"**{self.words['acceptance']}**",
                *[f"- [ ] {a}" for a in ticket.acceptance],
                "",
            ]
        lines.append(f"{self.words['wave']} {ticket.wave}")
        if ticket.depends_on:
            lines.append(f"{self.words['after']}: {', '.join(ticket.depends_on)}")
        lines += ["", f"`ffai-run:{ticket.run}`"]
        return "\n".join(lines)

    def _body(self, content: str, marker: str) -> str:
        if len(content) > BODY_CHARS:
            content = content[:BODY_CHARS].rsplit("\n", 1)[0] + f"\n\n_{self.words['truncated']}_"
        return f"{content}\n\n`{marker}`"


def row(issue: dict[str, Any]) -> WorkRow:
    description = [line.strip() for line in str(issue.get("description") or "").splitlines()]
    return WorkRow(
        str(issue["identifier"]),
        MARKS.get(str(issue["state"]["type"]), " "),
        str(issue["title"]),
        tuple(line for line in description if line)[:DETAIL_LINES],
        link=f"linear:issue:{issue['id']}",
    )


def parse_link(link: str) -> tuple[str, str]:
    parts = link.split(":", 2)
    if len(parts) != 3 or parts[0] != "linear" or parts[1] not in ("issue", "project"):
        raise TrackerError(f"Not a Linear reference: {link}")
    return parts[1], parts[2]


def run_marker(description: str) -> str:
    """The factory run an issue was created for, from its `ffai-run:` marker."""
    start = description.find("`ffai-run:")
    if start < 0:
        return ""
    end = description.find("`", start + 1)
    return description[start + len("`ffai-run:") : end] if end > start else ""
