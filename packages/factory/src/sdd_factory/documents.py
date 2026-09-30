"""A parent's documents: its specification, the breakdown awaiting approval, and the files written on approval."""

from pathlib import Path

from sdd_core.codec import encode
from sdd_core.models import Json
from sdd_core.tickets import drafts_of
from sdd_runtime.engine import Engine

from sdd_factory.catalog import ProjectCatalog
from sdd_factory.model import BREAKDOWNS, children_of


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
        found = self.engine.latest(run_id, "specification")
        return found.reason if found is not None else ""

    def preview(self, run_id: str) -> list[dict[str, object]]:
        """The breakdown awaiting approval (a feature's tickets, a draft's features),
        for the approval dialog."""
        product = children_of(self.catalog.task(run_id).kind).product
        try:
            return [encode(draft) for draft in drafts_of(self.engine.facts(run_id), product)]
        except ValueError:
            return []

    def artifacts_folder(self, run_id: str) -> Path:
        """Where the factory exports a parent's documents for people to read."""
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
        for kind in BREAKDOWNS:
            if kind in stored:
                (folder / f"{kind}.md").write_text(stored[kind].content, encoding="utf-8")

    def documents(self, run_id: str) -> dict[str, object]:
        """The parent's specification (PRD) and breakdown: approved or in progress."""
        stored = self.catalog.artifacts(run_id)
        approved = any(kind in stored for kind in BREAKDOWNS)
        specification = (
            stored["specification"].content
            if "specification" in stored
            else self.specification(run_id)
        )
        breakdown: Json = list[Json](self.catalog.breakdown(run_id))
        return {
            "specification": specification,
            "tickets": breakdown if approved else self.preview(run_id),
            "approved": approved,
            "folder": str(self.artifacts_folder(run_id)) if approved else "",
        }
