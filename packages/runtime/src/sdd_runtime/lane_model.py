"""The lane record and what a lane step is asked to do: values, names and commit
messages, without any IO (see `lanes` and `lane_actions`)."""

from dataclasses import dataclass, field
from pathlib import Path

from sdd_core.codec import canonical, decode, encode, object_json
from sdd_core.options import StepOptions


def lane_branch(run_id: str) -> str:
    """The branch a run's lane works on in every repository of its scope."""
    return "ffai/" + run_id


# The lane's record on disk, in the run's own folder beside its attempts: a lane
# step reads it there, and nothing of the engine lies in a working copy.
LANE_FILE = "lane.json"


def lane_file(run_folder: Path) -> Path:
    return run_folder / LANE_FILE


RESULT_FILE = "lane-result.json"


# Version 2 lanes check linked dependencies out (`attached`).
LANE_VERSION = 2


# Subjects longer than this are cut: commit tools and reviewers expect short subjects.
SUBJECT_CHARS = 72


@dataclass(frozen=True)
class CommitMessages:
    """A project's commit convention for engine commits; Conventional without attribution
    by default. `{repo}` is the repository's folder name."""

    work: str = "feat({repo}): {title}"
    pin: str = "build({repo}): pin {dependency} {sha}"

    @classmethod
    def of(cls, options: StepOptions) -> "CommitMessages":
        defaults = cls()
        return cls(options.commit_message or defaults.work, options.pin_message or defaults.pin)

    def check(self) -> None:
        """Refuse a template that names an unknown field."""
        self.for_work("repo", "title")
        self.for_pin("repo", "dependency", "0" * 40)

    def for_work(self, repo: str, title: str) -> str:
        return _subject(self.work.format(repo=repo, title=title))

    def for_pin(self, repo: str, dependency: str, sha: str) -> str:
        name = dependency.rsplit("/", 1)[-1]
        return _subject(self.pin.format(repo=repo, dependency=name, sha=sha[:10]))


def _subject(message: str) -> str:
    line = " ".join(message.split())
    return line if len(line) <= SUBJECT_CHARS else line[: SUBJECT_CHARS - 1].rstrip() + "…"


@dataclass(frozen=True)
class LaneJob:
    """What a lane action needs from its attempt: the ticket, the convention, and the
    repositories its accepted prerequisites delivered."""

    title: str
    messages: CommitMessages = CommitMessages()
    delivered: tuple[str, ...] = ()


@dataclass
class LaneRepo:
    path: str  # relative to the workspace
    branch: str
    base: str  # commit the lane started from ("" for a repository the task creates)
    origin: str  # branch checked out in the main copy at creation ("" if detached)
    fresh: bool = False


@dataclass
class Attached:
    """A dependency checked out inside the lane where a repository links it."""

    path: str  # relative to the lane root
    dependency: str  # the dependency repository, relative to the workspace


@dataclass
class Lane:
    run: str
    workspace: str
    root: str
    repos: list[LaneRepo] = field(default_factory=list)
    status: str = "active"  # opening | active | removing | removed
    version: int = LANE_VERSION
    attached: list[Attached] = field(default_factory=list)

    def document(self) -> str:
        return canonical(encode(self))

    def work(self, repo: LaneRepo) -> Path:
        """The repository's working copy in the lane."""
        return Path(self.root) if repo.path == "." else Path(self.root) / repo.path

    def main(self, repo: LaneRepo) -> Path:
        """The repository's main copy in the workspace."""
        return Path(self.workspace) if repo.path == "." else Path(self.workspace) / repo.path


def load_lane(document: str) -> Lane:
    raw = object_json(document)
    # A lane written before versions existed is version 1: it has no attached links yet.
    return decode(Lane, {"version": 1, **raw})
