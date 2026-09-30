"""Local composition; application use cases depend only on persistence ports."""

from pathlib import Path

from sdd_core.sdk import ProjectAdapter
from sdd_storage.store import Store

from sdd_runtime.application import ApplicationEngine
from sdd_runtime.files import work_folder
from sdd_runtime.git import GitProject
from sdd_runtime.workspace import LocalWorkspace
from sdd_runtime.workspace import overlaps as overlaps


class Engine(ApplicationEngine):
    store: Store

    def __init__(
        self, store: Store, project: ProjectAdapter | None = None, work: Path | None = None
    ) -> None:
        """`work` is the engine's own folder; by default it lies beside the database."""
        super().__init__(
            store, project or GitProject(), LocalWorkspace(work or work_folder(store.path))
        )
