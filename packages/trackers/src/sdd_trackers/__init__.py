"""Tracker adapters: each reads work items and mirrors the factory's progress.

An adapter implements `sdd_core.tracking.Tracker` and registers under the
`sdd.trackers` entry-point group; a project chooses one by kind in its settings.
"""
