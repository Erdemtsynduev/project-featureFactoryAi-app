"""Approved ticket manifests are deterministic inputs, never inferred from status text."""

from dataclasses import dataclass

from sdd_core.graph import dependency_layers
from sdd_core.models import Run


@dataclass(frozen=True)
class Ticket:
    id: str
    requirement_ids: tuple[str, ...]
    depends_on: tuple[str, ...]
    context: str


@dataclass(frozen=True)
class Portfolio:
    id: str
    requirement_ids: tuple[str, ...]
    tickets: tuple[Ticket, ...]


def ordered(portfolio: Portfolio) -> tuple[Ticket, ...]:
    tickets = {ticket.id: ticket for ticket in portfolio.tickets}
    if not tickets or len(tickets) != len(portfolio.tickets):
        raise ValueError("Empty or duplicate tickets")
    required = set(portfolio.requirement_ids)
    covered = {identifier for ticket in portfolio.tickets for identifier in ticket.requirement_ids}
    if not required or covered != required:
        raise ValueError("Ticket coverage must match approved requirements")
    layers = dependency_layers(
        {ticket.id: ticket.depends_on for ticket in portfolio.tickets},
        "Cyclic or missing ticket dependency",
    )
    return tuple(tickets[identifier] for layer in layers for identifier in sorted(layer))


def accepted_requirements(portfolio: Portfolio, runs: tuple[Run, ...]) -> tuple[str, ...]:
    ordered(portfolio)
    states = {run.id: run for run in runs}
    return tuple(
        identifier
        for identifier in portfolio.requirement_ids
        if all(
            ticket.id in states and states[ticket.id].status == "accepted"
            for ticket in portfolio.tickets
            if identifier in ticket.requirement_ids
        )
    )
