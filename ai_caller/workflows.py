"""Workflows kept from the source, pointed at Pramaan.

A handoff between agents is a squad. Bridging a second person into the call is not available.
"""

from __future__ import annotations

from ai_caller.model import Agent, Squad, Tool
from ai_caller.pramaan import Unsupported
from ai_caller.service import Service


def handoff(service: Service, starter: Agent, specialist: Agent, squad_name: str) -> dict:
    """The first agent can hand the call to the second. The first member starts."""
    starter.tools.append(
        Tool(
            name="pass_call",
            description="Hand this call to the specialist.",
            kind="transfer",
            assistants=[specialist.name],
        )
    )
    service.publish(starter)
    service.publish(specialist)
    return service.publish_squad(
        Squad(squad_name, [(starter.name, [specialist.name]), (specialist.name, [])])
    )


def warm_transfer_to_person(number: str) -> None:
    """The source dialled a person through a carrier API.

    voice.call.control only mutes or isolates. There is no capability that adds a second person.
    """
    raise Unsupported(
        "A second person cannot be bridged into the call. Hand off to another agent instead."
    )
