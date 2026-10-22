"""A reception agent, compiled and then used to place one practice call."""

from ai_caller.model import Agent, Tool
from ai_caller.pramaan import client_from_env
from ai_caller.service import Service


def build() -> Agent:
    return Agent(
        name="reception",
        prompt="You answer the phone for the office. Be brief. Do not invent facts.",
        greeting="Hello, how can I help?",
        tools=[Tool(name="finish", description="End the call when the person is done.", kind="end_call")],
        knowledge_text="The office opens at nine and closes at five.",
    )


def main() -> None:
    service = Service(client_from_env())
    print(service.publish(build())["name"])


if __name__ == "__main__":
    main()
