"""Preparing an attempt's launch: the handler it runs with, its packet and its files."""

import subprocess
from pathlib import Path

from sdd_core.codec import canonical, encode, object_json
from sdd_core.models import PROCESS_KINDS, Step
from sdd_core.sdk import PACKET_FILE, Manifest, Packet, Registry, handler_key

from sdd_runtime.application import ApplicationEngine
from sdd_runtime.files import atomic_write
from sdd_runtime.packets import build_packet
from sdd_runtime.supervisor import INPUT_FILE, Plan

# Windows refuses a command line past 32,767 characters; keep a margin for quoting.
COMMAND_LINE_CHARS = 32000
HANDLER_FILE = "handler.json"


class Launcher:
    def __init__(self, engine: ApplicationEngine, registry: Registry) -> None:
        self.engine, self.registry = engine, registry

    def manifest(self, step: Step) -> Manifest:
        """The handler a step runs with now, checked against the step's needs."""
        manifest = self.registry.get(handler_key(step)).manifest
        required_profile = step.options.profile_snapshot
        if not step.handler and step.profile != "default" and required_profile is None:
            raise ValueError("Resolve named profiles when creating the run with --config")
        if (
            required_profile is not None
            and object_json(manifest.settings).get("profile") != required_profile
        ):
            raise ValueError("Run profile differs from its creation snapshot")
        if step.kind not in manifest.capabilities:
            raise ValueError(f"Handler {manifest.id} lacks {step.kind} capability")
        return manifest

    def bind(self, run_id: str) -> None:
        """Pin every process step's current handler; waits (Conflict) while one runs."""
        workflow = self.engine.store.workflow(self.engine.store.get(run_id).workflow_digest)
        with self.engine.store.unit() as db:
            for step in workflow.steps:
                if step.kind in PROCESS_KINDS:
                    document = canonical(encode(self.manifest(step)))
                    db.bind_handler(run_id, handler_key(step), document)

    def packet(self, run_id: str) -> Packet:
        return build_packet(self.engine.store, self.registry, run_id, self.engine.workspace)

    def prepare(self, run_id: str) -> tuple[Packet, Plan]:
        """The dispatched attempt's packet and launch plan, its files written.

        A launch that could not run safely is refused before any host starts: its cwd
        must stay in the owned workspace, its executable be explicit, its command line
        fit the platform.
        """
        self.bind(run_id)
        packet = self.packet(run_id)
        folder = Path(packet.directory)
        launch = self.registry.get(handler_key(packet.step)).prepare(packet)
        if not Path(launch.cwd).resolve().is_relative_to(Path(packet.workspace).resolve()):
            raise ValueError("Handler cwd must stay inside the owned workspace")
        if not launch.argv or not Path(launch.argv[0]).is_absolute():
            raise ValueError("Explicit executable required")
        if len(subprocess.list2cmdline(launch.argv)) > COMMAND_LINE_CHARS:
            raise ValueError("Command line too long; the handler must pass its prompt on stdin")
        atomic_write(folder / PACKET_FILE, canonical(encode(packet)))
        # The handler this attempt runs with: its provenance, whatever changes later.
        atomic_write(folder / HANDLER_FILE, canonical(encode(self.manifest(packet.step))))
        if launch.input:
            atomic_write(folder / INPUT_FILE, launch.input)
        plan = Plan(
            str(folder),
            packet.workspace,
            launch.argv,
            launch.cwd,
            tuple(sorted(launch.environment)),
            INPUT_FILE if launch.input else "",
        )
        return packet, plan
