"""Where drafts come from: numbered Markdown plans and a tracker (`sdd.trackers`).

A project names its sources in its settings. The factory reads work items through
`sdd_core.tracking.WorkSource`; a source that is also a `Tracker` receives the
factory's progress through the tracker outbox.
"""

from sdd_core.tracking import Tracker, WorkItem, WorkRow, WorkSource

__all__ = ["Tracker", "WorkItem", "WorkRow", "WorkSource"]
