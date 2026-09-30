"""A feature's documents: its specification, the breakdown awaiting approval, and the files written on approval."""

from pathlib import Path

from sdd_core.codec import encode
from sdd_core.tickets import (
    tickets_of,
)
from sdd_runtime.engine import Engine

from sdd_factory.catalog import ProjectCatalog


class FeatureDocuments:
    def __init__(
        self,
        engine: Engine,
        catalog: ProjectCatalog,
    ) -> None:
        self.engine = engine
        self.catalog = catalog

    def specification(self, run_id: str) -> str:
        """The latest specification the feature's specification step produced, whole."""
        for result in self.engine.outputs(run_id, "specification"):
            if result.outcome == "done":
                return result.reason
        return ""

    def preview(self, run_id: str) -> list[dict[str, object]]:
        """Tickets awaiting approval on a feature, for the approval dialog."""
        try:
            return [encode(draft) for draft in tickets_of(self.engine.facts(run_id))]
        except ValueError:
            return []

    def artifacts_folder(self, run_id: str) -> Path:
        """Where the factory exports a feature's documents for people to read."""
        project = self.catalog.task(run_id).project or "_"
        return self.engine.store.path.parent / "artifacts" / project / run_id

    def export(self, run_id: str) -> None:
        folder = self.artifacts_folder(run_id)
        folder.mkdir(parents=True, exist_ok=True)
        title = self.catalog.task(run_id).title or run_id
        stored = self.catalog.artifacts(run_id)
        if "specification" in stored:
            text = f"# {title}\n\n{stored['specification'].content}\n"
            (folder / "spec.md").write_text(text, encoding="utf-8")
        if "tickets" in stored:
            (folder / "tickets.md").write_text(stored["tickets"].content, encoding="utf-8")

    def documents(self, run_id: str) -> dict[str, object]:
        """The feature's specification (PRD) and ticket breakdown: approved or in progress."""
        stored = self.catalog.artifacts(run_id)
        approved = "specification" in stored
        specification = stored["specification"].content if approved else self.specification(run_id)
        tickets = stored["tickets"].data if "tickets" in stored else self.preview(run_id)
        return {
            "specification": specification,
            "tickets": tickets,
            "approved": approved,
            "folder": str(self.artifacts_folder(run_id)) if approved else "",
        }
