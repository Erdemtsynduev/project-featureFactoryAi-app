"""Choose each project's work sources and tracker from its settings.

A project's drafts come from every source it names: numbered Markdown plans in its
plans folder, and the tracker it names (`{"kind": "linear", ...}`), which also gets
the factory's progress mirrored back. Trackers are trusted
installed code registered under the `sdd.trackers` entry-point group; a project can
only choose among installed ones, never import code by name. Settings never hold
secrets: an adapter reads its token from the environment variable the settings name.
"""

import re
from collections.abc import Callable
from importlib.metadata import entry_points

from sdd_core.codec import canonical, mapping, text
from sdd_core.models import Json
from sdd_core.tracking import Tracker, WorkSource

from sdd_factory.sources.markdown import MarkdownPlans

GROUP = "sdd.trackers"
KIND = re.compile(r"[a-z][a-z0-9_-]{0,31}")
ENVIRONMENT = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
# Settings keys that would hold a secret; a token belongs in the environment.
SECRET = re.compile(r"(^|_)(token|secret|password|api_?key)$", re.IGNORECASE)

type TrackerFactory = Callable[[dict[str, Json]], Tracker]


def installed_trackers() -> dict[str, TrackerFactory]:
    """Tracker adapters installed in this environment, by kind."""
    return {point.name: point.load() for point in entry_points(group=GROUP)}


def tracker_settings(raw: Json) -> dict[str, Json]:
    """Validated tracker settings of a project, or {} when it has none."""
    if raw in (None, {}, ""):
        return {}
    settings = dict(mapping(raw))
    kind = text(settings.get("kind"), "tracker kind")
    if not KIND.fullmatch(kind):
        raise ValueError(f"Invalid tracker kind: {kind}")
    for key in settings:
        if SECRET.search(key):
            raise ValueError(
                f"Tracker setting {key} would store a secret; name an environment variable "
                "in token_env instead"
            )
    variable = settings.get("token_env", "")
    if variable and not ENVIRONMENT.fullmatch(text(variable, "token_env")):
        raise ValueError("token_env must name an environment variable, e.g. LINEAR_API_KEY")
    return settings


class ProjectSources:
    """Where each project's work comes from and where its progress is shown."""

    def __init__(self, factories: Callable[[], dict[str, TrackerFactory]] = installed_trackers):
        self.factories = factories
        self._trackers: dict[str, Tracker] = {}

    def tracker(self, project: dict[str, Json]) -> Tracker | None:
        """The project's tracker, built once per distinct settings; None without one."""
        settings = tracker_settings(project.get("tracker"))
        if not settings:
            return None
        # Text the tracker shows follows the project's language unless set.
        settings = {"language": project.get("language", "ru"), **settings}
        key = canonical(settings)
        if key not in self._trackers:
            kind = text(settings["kind"], "tracker kind")
            factories = self.factories()
            if kind not in factories:
                raise ValueError(f"Tracker not installed: {kind}")
            tracker = factories[kind](settings)
            for method in ("items", "publish"):
                if not callable(getattr(tracker, method, None)):
                    raise ValueError(f"Tracker {kind} lacks {method}")
            self._trackers[key] = tracker
        return self._trackers[key]

    def sources(self, project: dict[str, Json]) -> list[WorkSource]:
        """Every source the project names: its Markdown plans folder and its tracker."""
        found: list[WorkSource] = []
        folder = str(project.get("plans_folder", ""))
        if folder:
            found.append(MarkdownPlans(folder))
        tracker = self.tracker(project)
        if tracker is not None:
            found.append(tracker)
        if not found:
            raise ValueError(
                "The project has no source of drafts; set a plans folder or a tracker in "
                "its settings"
            )
        return found
