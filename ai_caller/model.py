"""Agent definitions. These compile to a Pramaan voice assistant. They do not open a call."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
PHONE = re.compile(r"^\+[1-9]\d{7,14}$")
LANGS = {"en", "es", "fr", "de", "it", "pt", "hi", "ja", "zh"}
VOICE_KEYS = (
    "default",
    "female-1",
    "female-2",
    "female-3",
    "male-1",
    "male-2",
    "male-3",
)


def listed_voice(value: str | None) -> str | None:
    if value in VOICE_KEYS:
        return value
    return None
TOOL_KINDS = {
    "end_call",
    "keypad",
    "voicemail",
    "handoff",
    "transfer",
    "code",
    "function",
    "api_request",
    "mcp",
}


class DefinitionError(ValueError):
    pass


def check_name(value: str) -> str:
    if not isinstance(value, str) or not NAME.match(value) or len(value) > 40:
        raise DefinitionError(
            "Use a name up to 40 characters: a letter, then letters, digits, underscores or hyphens."
        )
    return value


def check_phone(value: str) -> str:
    if not isinstance(value, str) or not PHONE.match(value):
        raise DefinitionError("Use a phone number in international form, such as +14155550100.")
    return value


def check_duration(seconds: int) -> int:
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 10 or seconds > 600:
        raise DefinitionError("Call length must be between 10 and 600 seconds.")
    return seconds


@dataclass
class Parameter:
    name: str
    type: str
    description: str
    required: bool = False

    def __post_init__(self) -> None:
        if self.type not in {"string", "number", "boolean"}:
            raise DefinitionError("A parameter type is string, number or boolean.")
        if not NAME.match(self.name):
            raise DefinitionError("A parameter name starts with a letter.")


@dataclass
class Tool:
    name: str
    description: str
    kind: str = "function"
    parameters: list[Parameter] = field(default_factory=list)
    assistants: list[str] = field(default_factory=list)
    code: str | None = None
    endpoint: str | None = None
    timeout_seconds: int = 10

    def __post_init__(self) -> None:
        check_name(self.name)
        if self.code and self.endpoint:
            raise DefinitionError("Choose hosted code or an app endpoint.")
        if self.code:
            if len(self.code) > 50000:
                raise DefinitionError("Hosted code must be at most 50,000 characters.")
            self.kind = "code"
        elif self.endpoint:
            if not isinstance(self.endpoint, str) or not re.fullmatch(r"we_[a-f0-9]{32}", self.endpoint):
                raise DefinitionError("Use a webhook endpoint id: we_ and 32 hex digits.")
            self.kind = "function"
        if self.kind not in TOOL_KINDS:
            raise DefinitionError("That tool kind is not supported.")
        if len(self.parameters) > 20:
            raise DefinitionError("A tool has at most 20 parameters.")
        if (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or not 1 <= self.timeout_seconds <= 30
        ):
            raise DefinitionError("Tool timeout is 1 to 30 seconds.")


@dataclass
class Agent:
    """The kept shape from the source: instructions, a greeting, and tools."""

    name: str
    prompt: str
    greeting: str | None = None
    language: str = "en"
    tools: list[Tool] = field(default_factory=list)
    max_duration_seconds: int = 120
    recording: bool = False
    knowledge_text: str | None = None
    knowledge_base_ref: str | None = None
    voice: str | None = None
    wait_seconds: float = 0.4
    interruption_words: int = 2
    end_call_phrases: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        check_name(self.name)
        if not self.prompt or len(self.prompt) > 10000:
            raise DefinitionError("Write a prompt of at most 10,000 characters.")
        if self.language not in LANGS:
            raise DefinitionError("Pick a supported language.")
        check_duration(self.max_duration_seconds)
        if self.greeting is not None and len(self.greeting) > 1000:
            raise DefinitionError("The greeting must be at most 1,000 characters.")
        if not 0 <= self.wait_seconds <= 5:
            raise DefinitionError("The pause before speaking is between 0 and 5 seconds.")
        if not 0 <= self.interruption_words <= 10:
            raise DefinitionError("Interruption words are between 0 and 10.")


@dataclass
class Squad:
    name: str
    members: list[tuple[str, list[str]]]

    def __post_init__(self) -> None:
        check_name(self.name)
        if not 1 <= len(self.members) <= 10:
            raise DefinitionError("A squad has 1 to 10 members. The first member starts the call.")
        seen = [name for name, _targets in self.members]
        if len(set(seen)) != len(seen):
            raise DefinitionError("Each assistant appears once in a squad.")
        for name, targets in self.members:
            check_name(name)
            for target in targets:
                check_name(target)
