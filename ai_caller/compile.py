"""Turn an agent definition into Pramaan request bodies. Does not call the network."""

from __future__ import annotations

from ai_caller.model import Agent, Squad, Tool, listed_voice


def tool_body(tool: Tool) -> dict:
    """Body for voice.tool.create.

    PRA-38 is a function tool with an endpoint id on origin/main 9e9932a1.
    The id is we_ and 32 hex digits. It is not live-verified.
    """
    body: dict = {
        "name": tool.name,
        "kind": tool.kind,
        "description": tool.description,
        "parameters": [
            {
                "name": item.name,
                "type": item.type,
                "description": item.description.strip() or f"Argument {item.name}.",
                "required": item.required,
            }
            for item in tool.parameters
        ],
        "assistants": list(tool.assistants),
        "timeoutSeconds": tool.timeout_seconds,
    }
    if tool.code:
        body["kind"] = "code"
        body["code"] = tool.code
    elif tool.endpoint:
        body["kind"] = "function"
        body["endpoint"] = tool.endpoint
    return body


def assistant_body(agent: Agent) -> dict:
    """Body for voice.assistant.create.

    knowledgeBaseRef is sent when the agent already names a base. voice is sent
    only when it is one of the listed keys. voiceRef stays off.
    """
    body: dict = {
        "name": agent.name,
        "prompt": agent.prompt,
        "language": agent.language,
        "tier": "standard",
        "maxDurationSeconds": agent.max_duration_seconds,
        "recording": agent.recording,
        "tools": [tool.name for tool in agent.tools],
        "backgroundSound": "off",
        "waitSeconds": agent.wait_seconds,
        "interruptionWords": agent.interruption_words,
        "endCallPhrases": list(agent.end_call_phrases),
    }
    if agent.greeting:
        body["greeting"] = agent.greeting
    if agent.knowledge_base_ref:
        body["knowledgeBaseRef"] = agent.knowledge_base_ref
    voice = listed_voice(agent.voice)
    if voice:
        body["voice"] = voice
    return body


def knowledge_body(agent: Agent) -> dict | None:
    if not agent.knowledge_text:
        return None
    return {"name": agent.name, "text": agent.knowledge_text}


def squad_body(squad: Squad) -> dict:
    return {
        "name": squad.name,
        "members": [
            {"assistant": name, "handoffTo": list(targets)} for name, targets in squad.members
        ],
    }


def plan(agent: Agent) -> list[tuple[str, dict]]:
    """Ordered capability calls that publish one agent."""
    steps: list[tuple[str, dict]] = [("voice.tool.create", tool_body(tool)) for tool in agent.tools]
    knowledge = knowledge_body(agent)
    if knowledge and not agent.knowledge_base_ref:
        steps.append(("voice.knowledge-base.create", knowledge))
    steps.append(("voice.assistant.create", assistant_body(agent)))
    return steps
