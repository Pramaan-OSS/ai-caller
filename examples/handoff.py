"""Desk agent hands the call to billing. A person-to-person bridge is not available."""

from ai_caller.model import Agent
from ai_caller.pramaan import client_from_env
from ai_caller.service import Service
from ai_caller.workflows import handoff


def main() -> None:
    service = Service(client_from_env())
    result = handoff(
        service,
        Agent(name="desk", prompt="Greet the caller and pass billing questions on."),
        Agent(name="billing", prompt="Help with a bill. Be brief."),
        "front",
    )
    print(result["name"])


if __name__ == "__main__":
    main()
