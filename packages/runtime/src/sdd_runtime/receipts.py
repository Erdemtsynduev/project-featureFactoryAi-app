"""An ended attempt's result, decided once: the receipt is written before it applies."""

from collections.abc import Callable
from pathlib import Path

from sdd_core.codec import decode, object_json, result_json, result_load
from sdd_core.execution import ExecutionRequest
from sdd_core.models import Result
from sdd_core.sdk import Packet, Registry, handler_key

from sdd_runtime.application import ApplicationEngine
from sdd_runtime.files import atomic_write
from sdd_runtime.launcher import Launcher
from sdd_runtime.supervisor import Plan

RECEIPT_FILE = "receipt.json"


class Receipts:
    def __init__(
        self,
        engine: ApplicationEngine,
        registry: Registry,
        launcher: Launcher,
        owner: Callable[[], str],
    ) -> None:
        """`owner()` names the supervisor whose requests these are."""
        self.engine, self.registry, self.launcher, self.owner = engine, registry, launcher, owner
        # Packets of attempts this process submitted; a restart rebuilds them.
        self.packets: dict[str, Packet] = {}

    def request(self, attempt: str) -> ExecutionRequest | None:
        """The durable request the store holds for one of this supervisor's attempts."""
        with self.engine.store.unit() as db:
            row = db.execution(attempt)
        if row is None or row[0] != self.owner():
            return None
        return decode(ExecutionRequest, object_json(row[1]))

    def collect(self, request: ExecutionRequest, plan: Plan, exit_code: int) -> Result:
        """The handler's result for a quiescent attempt, bound to the owned revision.

        The receipt is written before the result is applied, so a restart reapplies
        the same result instead of asking the handler (or the model) again.
        """
        packet = self.packets.pop(request.id, None)
        if packet is None:
            with self.engine.store.unit() as db:
                run_id = db.effect(request.id).run_id
            self.launcher.bind(run_id)
            packet = self.launcher.packet(run_id)
        current = self.engine.observe(packet.run_id)
        receipt = Path(plan.folder) / RECEIPT_FILE
        if receipt.exists():
            result = result_load(receipt.read_text(encoding="utf-8"))
            if result.revision != current:
                raise ValueError("Workspace changed after completion")
        else:
            result = self._handler_result(packet, exit_code, current)
        self.engine.workspace.verify(result, packet.workspace, current)
        atomic_write(receipt, result_json(result))
        return result

    def _handler_result(self, packet: Packet, exit_code: int, current: str) -> Result:
        try:
            return self.registry.get(handler_key(packet.step)).collect(packet, exit_code, current)
        except ValueError as error:
            # The attempt is quiescent: bad protocol is a product-independent blocker,
            # not a reason to invoke the model again and lose the primary diagnosis.
            return Result(
                packet.attempt.id,
                packet.attempt.generation,
                "blocked",
                "Provider protocol: " + str(error),
                current,
            )
