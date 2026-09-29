"""The Linear adapter against a recorded GraphQL conversation; no network."""

import pytest
from sdd_core.tracking import TicketMirror, TrackerUpdate
from sdd_factory.trackers import ProjectSources, installed_trackers
from sdd_trackers import linear
from sdd_trackers.graphql import TrackerError
from sdd_trackers.linear import LinearTracker

TEAM = {
    "teams": {
        "nodes": [
            {
                "id": "team-1",
                "key": "ENG",
                "states": {
                    "nodes": [
                        {"id": "s-done", "name": "Done", "type": "completed", "position": 3},
                        {"id": "s-todo", "name": "Todo", "type": "unstarted", "position": 1},
                        {"id": "s-work", "name": "In Progress", "type": "started", "position": 2},
                        {"id": "s-review", "name": "In Review", "type": "started", "position": 2.5},
                    ]
                },
            }
        ]
    }
}


def issue(identifier, state, parent=None, description=""):
    return {
        "id": "id-" + identifier,
        "identifier": identifier,
        "title": "Title " + identifier,
        "description": description,
        "url": "https://linear.app/x/" + identifier,
        "state": {"type": state},
        "parent": {"id": parent} if parent else None,
    }


class Linear:
    """A scripted Linear: answers by query, remembers every mutation."""

    def __init__(self):
        self.mutations = []
        self.comments = []
        self.updates = []
        self.children = []
        self.state = "s-todo"

    def __call__(self, query, variables):
        if query == linear.TEAM:
            return TEAM
        if query == linear.PROJECTS:
            return {
                "team": {
                    "projects": {
                        "nodes": [
                            {
                                "id": "p1",
                                "name": "Checkout",
                                "slugId": "a1b2c3",
                                "url": "https://linear.app/p/a1b2c3",
                                "description": "Short",
                                "content": "Pay online. Research first.",
                                "completedAt": None,
                                "canceledAt": None,
                                "issues": {
                                    "nodes": [
                                        issue(
                                            "ENG-1", "unstarted", description="Card\n\nand wallet"
                                        ),
                                        issue("ENG-2", "completed"),
                                        issue("ENG-3", "started"),
                                        issue("ENG-4", "unstarted", parent="id-ENG-1"),
                                    ]
                                },
                            },
                            {
                                "id": "p2",
                                "name": "Old",
                                "slugId": "zz",
                                "completedAt": "2026-01-01",
                                "canceledAt": None,
                                "issues": {"nodes": []},
                            },
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        if query == linear.ISSUE_COMMENTS:
            return {
                "issue": {
                    "id": variables["id"],
                    "state": {"id": self.state},
                    "comments": {"nodes": [{"body": b} for b in self.comments]},
                }
            }
        if query == linear.PROJECT_UPDATES:
            return {
                "project": {
                    "id": variables["id"],
                    "projectUpdates": {"nodes": [{"body": b} for b in self.updates]},
                }
            }
        if query == linear.CHILDREN:
            return {"issue": {"children": {"nodes": list(self.children)}}}
        self.mutations.append((query, variables))
        payload = variables.get("input", {})
        if query == linear.COMMENT:
            self.comments.append(payload["body"])
            return {"commentCreate": {"success": True}}
        if query == linear.PROJECT_UPDATE:
            self.updates.append(payload["body"])
            return {"projectUpdateCreate": {"success": True}}
        if query == linear.CREATE_ISSUE:
            created = "new-" + payload["title"].split(" ")[0]
            self.children.append({"id": created, "description": payload["description"]})
            return {"issueCreate": {"success": True, "issue": {"id": created}}}
        if query == linear.UPDATE_ISSUE:
            self.state = payload["stateId"]
            return {"issueUpdate": {"success": True}}
        if query == linear.RELATE:
            return {"issueRelationCreate": {"success": True}}
        raise AssertionError("unexpected query")


def tracker(fake, **settings):
    return LinearTracker({"kind": "linear", "team": "ENG", "language": "ru", **settings}, fake)


def test_projects_become_work_items_with_rows_from_their_issues():
    items = tracker(Linear()).items("C:/workspace")
    assert [i.key for i in items] == ["a1b2c3"], "a completed project is not work"
    item = items[0]
    assert item.link == "linear:project:p1" and item.body == "Pay online. Research first."
    assert [(r.id, r.mark) for r in item.rows] == [("ENG-1", " "), ("ENG-2", "x"), ("ENG-3", "~")]
    assert (
        item.rows[0].detail == ("Card", "and wallet")
        and item.rows[0].link == "linear:issue:id-ENG-1"
    )


def test_specification_and_state_are_posted_once():
    fake = Linear()
    t = tracker(fake)
    spec = TrackerUpdate("specification", "feature_a1b2c3", "linear:project:p1", "SPEC text")
    t.publish(spec)
    t.publish(spec)
    assert len(fake.updates) == 1 and "SPEC text" in fake.updates[0]
    assert f"ffai:{spec.id}" in fake.updates[0]
    blocked = TrackerUpdate(
        "state", "feature_a1b2c3", "linear:project:p1", "Needs a key", "blocked"
    )
    t.publish(blocked)
    health = [v["input"]["health"] for q, v in fake.mutations if q == linear.PROJECT_UPDATE]
    assert health == ["onTrack", "offTrack"] and "Заблокировано" in fake.updates[1]


def test_tickets_become_sub_issues_with_blocking_relations_once():
    fake = Linear()
    t = tracker(fake)
    tickets = (
        TicketMirror("f-T1", "T1", "Endpoint", "Build it", ("AC-1",)),
        TicketMirror("f-T2", "T2", "Screen", depends_on=("T1",), wave=2, hitl=True),
    )
    update = TrackerUpdate("tickets", "f", "linear:issue:parent", tickets=tickets)
    receipt = t.publish(update)
    assert dict(receipt.links) == {"f-T1": "linear:issue:new-T1", "f-T2": "linear:issue:new-T2"}
    created = [v["input"] for q, v in fake.mutations if q == linear.CREATE_ISSUE]
    assert [c["parentId"] for c in created] == ["parent", "parent"]
    assert created[0]["stateId"] == "s-todo" and "- [ ] AC-1" in created[0]["description"]
    assert "Нужен человек" in created[1]["description"] and "После: T1" in created[1]["description"]
    relations = [v["input"] for q, v in fake.mutations if q == linear.RELATE]
    assert relations == [{"issueId": "new-T1", "relatedIssueId": "new-T2", "type": "blocks"}]
    # Delivered again after a crash: the markers find the issues, nothing is duplicated.
    fake.mutations.clear()
    assert dict(t.publish(update).links) == dict(receipt.links)
    assert fake.mutations == []


def test_issue_states_move_along_the_workflow_and_named_states_win():
    fake = Linear()
    t = tracker(fake, states={"needs_person": "In Review"})
    t.publish(TrackerUpdate("state", "r", "linear:issue:i1", "", "working"))
    assert fake.state == "s-work"
    t.publish(TrackerUpdate("state", "r", "linear:issue:i1", "Answer the question", "needs_person"))
    assert fake.state == "s-review" and "Answer the question" in fake.comments[-1]
    t.publish(TrackerUpdate("state", "r", "linear:issue:i1", "", "done"))
    assert fake.state == "s-done"


def test_no_token_means_no_request_and_a_clear_error():
    t = LinearTracker(
        {"kind": "linear", "team": "ENG", "token_env": "FFAI_TEST_NO_TOKEN"}, environ={}
    )
    with pytest.raises(TrackerError, match="FFAI_TEST_NO_TOKEN"):
        t.items("C:/workspace")
    with pytest.raises(ValueError):
        LinearTracker({"kind": "linear"})
    with pytest.raises(TrackerError):
        linear.parse_link("jira:ENG-1")


def test_the_installed_adapter_is_chosen_by_project_settings():
    assert "linear" in installed_trackers()
    sources = ProjectSources()
    project = {"id": "app", "language": "ru", "tracker": {"kind": "linear", "team": "ENG"}}
    chosen = sources.tracker(project)
    assert isinstance(chosen, LinearTracker) and chosen.words["done"] == "Готово"
    assert sources.tracker(project) is chosen, "built once per settings"
    assert sources.tracker({"id": "x", "plans_folder": "plans"}) is None
    with pytest.raises(ValueError, match="not installed"):
        sources.tracker({"id": "y", "tracker": {"kind": "jira"}})
