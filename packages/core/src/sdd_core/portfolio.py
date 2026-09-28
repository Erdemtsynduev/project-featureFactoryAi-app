"""Approved ticket manifests are deterministic inputs, never inferred from status text."""

from dataclasses import dataclass

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
    emitted: list[Ticket] = []
    while tickets:
        done = {ticket.id for ticket in emitted}
        ready = sorted(
            (ticket for ticket in tickets.values() if set(ticket.depends_on) <= done),
            key=lambda ticket: ticket.id,
        )
        if not ready:
            raise ValueError("Cyclic or missing ticket dependency")
        for ticket in ready:
            emitted.append(ticket)
            del tickets[ticket.id]
    return tuple(emitted)


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
