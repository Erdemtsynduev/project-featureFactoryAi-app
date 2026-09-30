"""Answering a task: one policy per kind of task, chosen by what the task is.

A plan review's approval is checked before the answer and applied after it; any other
answer may approve a breakdown, whose tickets are admitted, and then the plan lead
reviews the fresh plan.
"""

from collections.abc import Callable

from sdd_core.codec import text
from sdd_core.models import Json

from sdd_factory.admission import TicketAdmission
from sdd_factory.catalog import ProjectCatalog
from sdd_factory.reviews import PlanReviews

type Policy = Callable[[dict[str, Json]], dict[str, object]]


class Answers:
    def __init__(
        self, catalog: ProjectCatalog, admission: TicketAdmission, reviews: PlanReviews
    ) -> None:
        self.catalog, self.admission, self.reviews = catalog, admission, reviews
        self.policies: dict[str, Policy] = {"review": self._review, "work": self._work}

    def answer(self, doc: dict[str, Json]) -> dict[str, object]:
        kind = "review" if self.catalog.task(text(doc.get("id"), "id")).reviews else "work"
        return self.policies[kind](doc)

    def _review(self, doc: dict[str, Json]) -> dict[str, object]:
        """An approved review is refused before the answer when its changes no longer
        apply, and applied after it."""
        review = text(doc.get("id"), "id")
        approved = text(doc.get("outcome"), "outcome") == "approved"
        if approved:
            self.reviews.revision(review)
        answered = self.admission.answer(doc)
        if approved:
            self.reviews.apply(review)
        return answered

    def _work(self, doc: dict[str, Json]) -> dict[str, object]:
        """An approved breakdown admits its tickets; the plan lead then reviews it."""
        run_id = text(doc.get("id"), "id")
        answered = self.admission.answer(doc)
        if answered.get("admitted"):
            self.reviews.request(run_id, f"breakdown:{run_id}", "a fresh breakdown was approved")
        return answered
