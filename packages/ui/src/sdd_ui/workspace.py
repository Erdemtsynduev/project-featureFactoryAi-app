"""Console catalog: agent discovery and measured usage over the factory catalog."""

from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from sdd_core.catalog import CatalogRecords
from sdd_core.codec import (
    canonical,
    encode,
    mapping,
    object_json,
    result_load,
    sequence,
    text,
)
from sdd_core.models import Workflow
from sdd_factory.catalog import ProjectCatalog
from sdd_providers.catalog import AgentAdapter, adapters
from sdd_runtime.discovery import InstallationProbe, authenticate, discover
from sdd_usage.pricing import cost


class WorkspaceCatalog(ProjectCatalog):
    """The factory's catalog plus what only the operator console needs: agent CLI
    discovery and measured usage."""

    def __init__(
        self,
        records: CatalogRecords,
        database: Path,
        workflow: Callable[[str], Workflow],
    ) -> None:
        """`database` is the control database, kept outside every project workspace."""
        super().__init__(records, database)
        self.workflow = workflow
        self.discovery: list[dict[str, object]] = []
        self.probes: dict[str, tuple[InstallationProbe, ...]] = {}
        self.subscription: dict[str, object] | None = None
        saved = records.preference("discovery")
        if saved is not None:
            # The last discovery survives restarts; connecting still needs the same paths.
            for raw in sequence(object_json(saved).get("items", [])):
                item = mapping(raw)
                self.discovery.append(dict(item))
                self.probes[text(item.get("adapter"), "adapter")] = tuple(
                    InstallationProbe(
                        tuple(text(a, "argv") for a in sequence(mapping(c).get("argv"))),
                        mapping(c).get("version")
                        if isinstance(mapping(c).get("version"), str)
                        else None,  # type: ignore[arg-type]
                        mapping(c).get("error")
                        if isinstance(mapping(c).get("error"), str)
                        else None,  # type: ignore[arg-type]
                    )
                    for c in sequence(item.get("candidates", []))
                )

    def discover(self) -> list[dict[str, object]]:
        """Installations plus native login state; bounded probes, no model requests.

        Called without the service lock: it touches no engine state.
        """

        def inspect(
            adapter: AgentAdapter,
        ) -> tuple[str, tuple[InstallationProbe, ...], dict[str, object]]:
            result = discover(adapter)
            item: dict[str, object] = encode(result)
            if result.selected is not None:
                login = authenticate(adapter, result.selected)
                item["authentication"] = login.status
                item["authentication_detail"] = login.detail
            item["suggested_models"] = list(adapter.suggested_models)
            item["model_catalog"] = adapter.model_arguments is not None
            return adapter.id, result.candidates, item

        with ThreadPoolExecutor(max_workers=4) as pool:
            found = list(pool.map(inspect, adapters().values()))
        self.probes = {identifier: candidates for identifier, candidates, _ in found}
        self.discovery = [item for _, _, item in found]
        self.records.save_preference("discovery", canonical({"items": self.discovery}))
        return self.discovery

    def usage(self, models: dict[str, str] | None = None) -> dict[str, object]:
        """Calls, tokens and API-equivalent cost per agent, per task and per day.

        Cost uses public rate cards (sdd-usage); subscription plans are billed
        separately, and models without public rates stay unpriced.
        """
        models = models or {}
        by_handler: dict[str, dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "tokens": 0, "active": 0, "unknown": 0, "usd": 0.0, "unpriced": 0}
        )
        per_run: dict[str, float] = defaultdict(float)
        daily: dict[str, int] = defaultdict(int)
        daily_usd: dict[str, float] = defaultdict(float)
        workflows: dict[str, Workflow] = {}
        for call in self.records.agent_calls():
            if call.workflow_digest not in workflows:
                workflows[call.workflow_digest] = self.workflow(call.workflow_digest)
            step = workflows[call.workflow_digest].step(call.step)
            key = step.profile if step.profile != "default" else step.handler
            bucket = by_handler[key]
            bucket["calls"] += 1
            bucket["active"] += int(call.status in ("running", "pending", "uncertain"))
            if not call.result:
                continue
            result = result_load(call.result)
            usage = result.usage
            bucket["unknown"] += int(usage.input_tokens is None or usage.output_tokens is None)
            bucket["tokens"] += (usage.input_tokens or 0) + (usage.output_tokens or 0)
            facts = object_json(result.data or "{}")
            day = (
                datetime.fromtimestamp(call.started, UTC).date().isoformat()
                if call.started is not None
                else datetime.now(UTC).date().isoformat()
            )
            model = facts.get("model") if isinstance(facts.get("model"), str) else models.get(key)
            priced = cost(
                str(model) if model else None,
                day,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read,
                usage.cache_write,
            )
            if priced.usd is None:
                bucket["unpriced"] += 1
                continue
            bucket["usd"] += priced.usd
            per_run[call.run_id] += priced.usd
            daily_usd[day] += priced.usd
        for day, count in self.records.daily_dispatches(14):
            daily[day] = count
        return {
            "by_handler": {
                k: {n: round(v, 4) for n, v in b.items()} for k, b in by_handler.items()
            },
            "daily_dispatches": dict(sorted(daily.items())),
            "daily_usd": {k: round(v, 4) for k, v in sorted(daily_usd.items())},
            "per_run_usd": {k: round(v, 4) for k, v in per_run.items()},
            "total_usd": round(sum(per_run.values()), 4),
            "subscription": self.subscription,
        }
