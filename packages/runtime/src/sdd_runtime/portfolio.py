"""Idempotent admission of approved ticket manifests into ordinary dependent runs."""

from pathlib import Path

from sdd_core.codec import canonical, encode, object_json
from sdd_core.portfolio import Portfolio, accepted_requirements, ordered

from sdd_runtime.application import ApplicationEngine


class PortfolioService:
    def __init__(self, engine: ApplicationEngine) -> None:
        self.engine = engine

    def admit(
        self, portfolio: Portfolio, definition: str, workspaces: dict[str, Path], now: float
    ) -> tuple[str, ...]:
        tickets = ordered(portfolio)
        if set(workspaces) != {ticket.id for ticket in tickets}:
            raise ValueError("Exactly one workspace per approved ticket is required")
        request = canonical(
            {
                "manifest": encode(portfolio),
                "definition": definition,
                "workspaces": {
                    key: str(value.resolve(strict=True)) for key, value in workspaces.items()
                },
            }
        )
        with self.engine.store.unit() as db:
            db.bind_portfolio(portfolio.id, request)
        for ticket in tickets:
            root = workspaces[ticket.id]
            self.engine.create(
                ticket.id,
                definition,
                root,
                ticket.context,
                self.engine.project.revision(str(root)),
                now,
                ticket.depends_on,
            )
        return tuple(ticket.id for ticket in tickets)

    def accepted(self, portfolio: Portfolio) -> tuple[str, ...]:
        with self.engine.store.unit() as db:
            document = db.portfolio(portfolio.id)
        if document is None or object_json(document)["manifest"] != object_json(
            canonical(encode(portfolio))
        ):
            raise ValueError("Portfolio differs from admitted revision")
        runs = tuple(self.engine.store.get(ticket.id) for ticket in portfolio.tickets)
        return accepted_requirements(portfolio, runs)
