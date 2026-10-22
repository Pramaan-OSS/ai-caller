"""One HTTP client for Pramaan. Tests use FakePramaan and never call the network."""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from ai_caller.model import PHONE, VOICE_KEYS

DEFAULT_BASE_URL = "https://api.pramaan.dev"

# voice.tool.create on origin/main 9e9932a1 accepts endpoint. Not live-verified.
TOOL_ENDPOINT_ON_MAIN = True

READS = {
    "voice.assistant.get",
    "voice.assistants.list",
    "voice.tool.get",
    "voice.tools.list",
    "voice.squad.get",
    "voice.squads.list",
    "voice.call.get",
    "voice.call.get-details",
    "voice.call.legs.list",
    "voice.call.recording.get",
    "voice.calls.list",
    "voice.campaign.get",
    "voice.campaigns.list",
    "voice.campaign.calls.list",
    "voice.number.get",
    "voice.numbers.list",
    "voice.sip-trunk.get",
    "voice.sip-trunks.list",
    "voice.browser-session.events",
    "phone.numbers.search",
    "phone.numbers.list",
    "phone.number.get",
    "audio.voices.list",
    "audio.voice.get",
    "audio.voice-catalog.list",
    "audio.transcribe",
    "voice.knowledge-base.get",
    "voice.knowledge-bases.list",
    "messaging.opt-out.check",
    "messaging.opt-outs.list",
    "calendar.events.list",
    "llm.chat",
    "auth.token.verify",
    "auth.user.get",
}


class PramaanError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ApprovalRequired(PramaanError):
    def __init__(self, plan: str) -> None:
        self.plan = plan
        super().__init__("approval_required", "Review and approve this plan within 15 minutes.")


class OptOut(PramaanError):
    def __init__(self) -> None:
        super().__init__("opt_out", "This number is on the do-not-call list.")


class Unsupported(PramaanError):
    def __init__(self, message: str) -> None:
        super().__init__("unsupported", message)


class OutcomeUnavailable(PramaanError):
    """502 upstream_unavailable. The caller should use the capability path."""

    def __init__(self) -> None:
        super().__init__("upstream_unavailable", "This outcome is not available.")


class OutcomeLater(PramaanError):
    """429. Try again later. Do not fall back, because nothing was started."""

    def __init__(self, message: str = "Try again later.") -> None:
        super().__init__("rate_limited", message)


class OutcomeCheck(PramaanError):
    """A step effect is unknown. Show it and do not retry on your own."""

    def __init__(self) -> None:
        super().__init__("effect_unknown", "Check this step before you try again.")


class ReplyLimit(PramaanError):
    """400 invalid_request naming maxTokens. The same limit will not succeed. Do not retry it."""

    def __init__(self) -> None:
        super().__init__(
            "invalid_request",
            "The reply limit is too small for an answer. Raise it and try again.",
        )
        self.retryable = False


class ReplyCutOff(PramaanError):
    """finishReason length on a structured or tool reply. The partial text is not a result."""

    def __init__(self) -> None:
        super().__init__("incomplete", "This reply was cut off. Try again.")
        self.retryable = True


class ModelReply:
    """Finished text, or free text that stopped because it hit the reply limit."""

    def __init__(self, text: str, cut_off: bool) -> None:
        self.text = text
        self.cut_off = cut_off


def read_model_reply(payload: Any, *, structured: bool) -> ModelReply:
    """A cut-off structured or tool reply fails. Free text may be continued once by the caller."""
    if not isinstance(payload, dict):
        raise ReplyCutOff()
    tool_calls = payload.get("toolCalls") or payload.get("tool_calls") or []
    has_tools = isinstance(tool_calls, list) and len(tool_calls) > 0
    text = payload.get("text") if isinstance(payload.get("text"), str) else ""
    cut_off = payload.get("finishReason") == "length"
    if cut_off and (structured or has_tools):
        raise ReplyCutOff()
    return ModelReply(text, cut_off)


# Default reply budget. The platform still refuses anything under 64.
REPLY_LIMIT = 256


# A 429 while starting a run: 25 open long runs per organisation. Not a vendor limit.
OPEN_RUNS_LATER = "Twenty-five long runs are already open. Try again later."
# The platform answer for a group that is switched off. Not an outcome fallback.
NOT_AVAILABLE = "This is not available yet."
# A run still open this long ends failed with upstream_timeout.
RUN_LIMIT_SECONDS = 35 * 60

# The platform refuses any other keyword on llm.chat and llm.complete schemas.
SCHEMA_REFUSED = "Schema must use the supported bounded JSON Schema subset."
_SCHEMA_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_SCHEMA_ANNOTATIONS = {"title", "description", "default", "examples"}
_SCHEMA_BOUNDS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
}
_SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "anyOf",
    "$defs",
    "$ref",
    *_SCHEMA_ANNOTATIONS,
    *_SCHEMA_BOUNDS,
}
# $schema and format are refused. Strip them. $id and $comment do not change which values match.
_SCHEMA_STRIP = {"$schema", "$id", "$comment", "format"}
_PARAM_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_TOOL_PARAM_KEYS = {"name", "type", "description", "required"}


class SchemaError(PramaanError):
    """A schema cannot be sent. The message names the tool and the path."""

    def __init__(self, name: str, path: str = "/", keyword: str = "") -> None:
        where = path or "/"
        if keyword and not where.endswith("/" + keyword):
            where = f"{where} ({keyword})"
        super().__init__("invalid_request", f"{name} at {where}: {SCHEMA_REFUSED}")


class _SchemaRejected(Exception):
    def __init__(self, path: str = "/", keyword: str = "") -> None:
        self.path = path or "/"
        self.keyword = keyword
        super().__init__(path)


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _is_json(value: Any, depth: int = 0, seen: set[int] | None = None) -> bool:
    if depth > 32:
        return False
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if not isinstance(value, (dict, list)):
        return False
    seen = seen or set()
    marker = id(value)
    if marker in seen:
        return False
    seen.add(marker)
    children = value.values() if isinstance(value, dict) else value
    valid = all(_is_json(item, depth + 1, seen) for item in children)
    seen.remove(marker)
    return valid


def _child_paths(node: dict, path: str) -> list[tuple[Any, str]]:
    found: list[tuple[Any, str]] = []
    properties = node.get("properties") if _is_record(node.get("properties")) else {}
    for name, child in properties.items():
        found.append((child, f"{path}/properties/{name}"))
    defs = node.get("$defs") if _is_record(node.get("$defs")) else {}
    for name, child in defs.items():
        found.append((child, f"{path}/$defs/{name}"))
    if "items" in node:
        found.append((node["items"], f"{path}/items"))
    if _is_record(node.get("additionalProperties")):
        found.append((node["additionalProperties"], f"{path}/additionalProperties"))
    if isinstance(node.get("anyOf"), list):
        for index, child in enumerate(node["anyOf"]):
            found.append((child, f"{path}/anyOf/{index}"))
    return found


def _resolve_ref(root: dict, ref: str, path: str) -> dict:
    if not isinstance(ref, str) or not ref.startswith("#/") or re.search(r"~(?![01])", ref):
        raise _SchemaRejected(path, "$ref")
    node: Any = root
    for raw in ref[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if not _is_record(node) or key not in node:
            raise _SchemaRejected(path, "$ref")
        node = node[key]
    if not _is_record(node):
        raise _SchemaRejected(path, "$ref")
    return node


def _normalise_node(node: Any, path: str = "/") -> Any:
    if isinstance(node, list):
        return [_normalise_node(item, f"{path}/{index}") for index, item in enumerate(node)]
    if not _is_record(node):
        return node
    kept = {key: value for key, value in node.items() if key not in _SCHEMA_STRIP}
    for wrapper in ("allOf", "oneOf"):
        branch = kept.get(wrapper)
        others = set(kept) - {wrapper, "title", "description", "default", "examples"}
        if isinstance(branch, list) and len(branch) == 1 and not others:
            child = _normalise_node(branch[0], f"{path}/{wrapper}/0")
            if _is_record(child):
                for annotation in ("title", "description", "default", "examples"):
                    if annotation in kept and annotation not in child:
                        child[annotation] = kept[annotation]
                return child
            raise _SchemaRejected(f"{path}/{wrapper}/0", wrapper)
    out: dict[str, Any] = {}
    for key, value in kept.items():
        if key in {"properties", "$defs"} and _is_record(value):
            out[key] = {
                name: _normalise_node(child, f"{path}/{key}/{name}") for name, child in value.items()
            }
        elif key == "items" or (key == "additionalProperties" and _is_record(value)):
            out[key] = _normalise_node(value, f"{path}/{key}")
        elif key == "anyOf" and isinstance(value, list):
            out[key] = [_normalise_node(child, f"{path}/anyOf/{index}") for index, child in enumerate(value)]
        else:
            out[key] = value
    return out


def _validate_schema(value: Any) -> None:
    if not _is_record(value) or not _is_json(value):
        raise _SchemaRejected("/", "type")
    if len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()) > 65536:
        raise _SchemaRejected("/", "size")
    nodes: set[int] = set()

    def walk(node: Any, depth: int, path: str) -> None:
        if not _is_record(node):
            raise _SchemaRejected(path, "type")
        if depth > 16:
            raise _SchemaRejected(path, "depth")
        if len(nodes) >= 256:
            raise _SchemaRejected(path, "nodes")
        nodes.add(id(node))
        for key, item in node.items():
            if key not in _SCHEMA_KEYS:
                raise _SchemaRejected(f"{path}/{key}", key)
            if key == "type":
                types = item if isinstance(item, list) else [item]
                if (
                    not types
                    or len(set(types)) != len(types)
                    or any(not isinstance(kind, str) or kind not in _SCHEMA_TYPES for kind in types)
                ):
                    raise _SchemaRejected(f"{path}/type", "type")
            if key in {"properties", "$defs"} and not _is_record(item):
                raise _SchemaRejected(f"{path}/{key}", key)
            if key == "required" and (
                not isinstance(item, list)
                or any(not isinstance(name, str) for name in item)
                or len(set(item)) != len(item)
            ):
                raise _SchemaRejected(f"{path}/required", "required")
            if key == "additionalProperties" and not isinstance(item, bool) and not _is_record(item):
                raise _SchemaRejected(f"{path}/additionalProperties", "additionalProperties")
            if key == "items" and not _is_record(item):
                raise _SchemaRejected(f"{path}/items", "items")
            if key == "anyOf" and (not isinstance(item, list) or not item or len(item) > 16):
                raise _SchemaRejected(f"{path}/anyOf", "anyOf")
            if key == "enum" and (not isinstance(item, list) or not item or len(item) > 256):
                raise _SchemaRejected(f"{path}/enum", "enum")
            if key in {"title", "description"} and not isinstance(item, str):
                raise _SchemaRejected(f"{path}/{key}", key)
            if key == "$ref" and (
                not isinstance(item, str) or not item.startswith("#/") or re.search(r"~(?![01])", item)
            ):
                raise _SchemaRejected(f"{path}/$ref", "$ref")
            if key == "examples" and not isinstance(item, list):
                raise _SchemaRejected(f"{path}/examples", "examples")
            if key in _SCHEMA_BOUNDS and (
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(item)
                or (
                    ("Length" in key or "Items" in key)
                    and (isinstance(item, bool) or not isinstance(item, int) or item < 0)
                )
            ):
                raise _SchemaRejected(f"{path}/{key}", key)
        for child, child_path in _child_paths(node, path):
            walk(child, depth + 1, child_path)

    walk(value, 0, "")
    expanded = 0

    def refs(node: dict, stack: set[int], depth: int, path: str) -> None:
        nonlocal expanded
        marker = id(node)
        expanded += 1
        if marker in stack or depth > 32 or expanded > 4096:
            raise _SchemaRejected(path or "/", "$ref")
        next_stack = set(stack)
        next_stack.add(marker)
        if isinstance(node.get("$ref"), str):
            target = _resolve_ref(value, node["$ref"], f"{path}/$ref")
            if id(target) not in nodes:
                raise _SchemaRejected(f"{path}/$ref", "$ref")
            refs(target, next_stack, depth + 1, f"{path}/$ref")
        for child, child_path in _child_paths(node, path):
            if _is_record(child):
                refs(child, next_stack, depth + 1, child_path)
            else:
                raise _SchemaRejected(child_path, "type")

    refs(value, set(), 0, "")


def bounded_schema(value: Any, name: str, *, root_object: bool = False) -> dict:
    """Copy a schema into the supported subset. The failure names the schema and the path."""
    try:
        if not _is_record(value):
            raise _SchemaRejected("/", "type")
        normalised = _normalise_node(value, "")
        _validate_schema(normalised)
    except _SchemaRejected as refused:
        raise SchemaError(name, refused.path, refused.keyword) from None
    if root_object and normalised.get("type") != "object":
        raise SchemaError(name, "/type", "type")
    if root_object and "anyOf" in normalised:
        raise SchemaError(name, "/anyOf", "anyOf")
    return normalised


def check_tool_parameters(parameters: Any, name: str) -> list:
    """voice.tool.create takes at most 20 scalar arguments, not a JSON Schema document."""
    if not isinstance(parameters, list) or len(parameters) > 20:
        raise PramaanError("invalid_request", f"{name}: at most 20 scalar arguments.")
    seen: set[str] = set()
    cleaned = []
    for index, item in enumerate(parameters):
        if not _is_record(item) or set(item) - _TOOL_PARAM_KEYS:
            raise PramaanError("invalid_request", f"{name}: at most 20 scalar arguments.")
        param_name = item.get("name")
        param_type = item.get("type")
        description = item.get("description", "")
        required = item.get("required", False)
        if (
            not isinstance(param_name, str)
            or _PARAM_NAME.fullmatch(param_name) is None
            or len(param_name) > 64
            or param_name in seen
            or param_type not in {"string", "number", "boolean"}
            or not isinstance(description, str)
            or not description.strip()
            or len(description) > 500
            or not isinstance(required, bool)
        ):
            raise PramaanError(
                "invalid_request",
                f"{name}: parameters.{index}.description must not be empty."
                if isinstance(description, str) and not description.strip()
                else f"{name}: at most 20 scalar arguments.",
            )
        seen.add(param_name)
        cleaned.append(
            {
                "name": param_name,
                "type": param_type,
                "description": description,
                "required": required,
            }
        )
    return cleaned


def _check_chat_body(capability_id: str, payload: dict) -> None:
    """llm.chat and llm.complete take messages. maxTokens, when sent, is at least 64."""
    messages = payload.get("messages")
    if "prompt" in payload or not isinstance(messages, list) or not messages:
        raise PramaanError("invalid_request", f"{capability_id} takes messages.")
    if "maxTokens" not in payload:
        return
    tokens = payload["maxTokens"]
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 64:
        raise PramaanError("invalid_request", f"{capability_id} needs maxTokens of 64 or more.")


def prepare_capability_body(capability_id: str, body: dict | None) -> dict:
    """Normalise every schema this client can send. A schema that cannot be expressed is refused."""
    payload = dict(body or {})
    if capability_id in {"voice.assistant.delete", "voice.tool.delete"} and payload.get("confirm") is not True:
        raise PramaanError("invalid_request", "Confirm before deleting this configuration.")
    if capability_id in {"llm.chat", "llm.complete"}:
        _check_chat_body(capability_id, payload)
    if capability_id in {"llm.chat", "llm.complete"} and payload.get("jsonSchema") is not None:
        payload["jsonSchema"] = bounded_schema(
            payload["jsonSchema"], f"{capability_id} jsonSchema", root_object=True
        )
    if capability_id == "llm.chat" and payload.get("tools"):
        tools = []
        for tool in payload["tools"]:
            if not _is_record(tool):
                raise SchemaError("llm.chat tool parameters")
            copied = dict(tool)
            if "parameters" in copied:
                label = str(copied.get("name") or "parameters")
                copied["parameters"] = bounded_schema(
                    copied["parameters"], f"llm.chat tool {label}", root_object=True
                )
            tools.append(copied)
        payload["tools"] = tools
    if capability_id == "document.extract" and payload.get("schema") is not None:
        schema = bounded_schema(payload["schema"], "document.extract schema")
        properties = schema.get("properties")
        encoded = json.dumps(schema, separators=(",", ":"), ensure_ascii=False)
        if schema.get("type") not in {None, "object"} or not _is_record(properties) or len(encoded) > 8000:
            where = "/type" if schema.get("type") not in {None, "object"} else "/properties"
            if _is_record(properties) and len(encoded) > 8000:
                where = "/"
            raise SchemaError("document.extract schema", where, "schema")
        payload["schema"] = schema
    if capability_id in {"voice.tool.create", "voice.tool.update"}:
        if "parameters" in payload:
            payload["parameters"] = check_tool_parameters(
                payload["parameters"], f"{capability_id} parameters"
            )
        patch = payload.get("patch")
        if _is_record(patch) and "parameters" in patch:
            patched = dict(patch)
            patched["parameters"] = check_tool_parameters(
                patched["parameters"], f"{capability_id} parameters"
            )
            payload["patch"] = patched
    return payload


def prepare_outcome_body(outcome_id: str, body: dict | None) -> dict:
    """Outcome inputs are call or number fields. A schema on that body still has to fit the subset."""
    payload = dict(body or {})

    def fold(source: dict, label: str) -> dict:
        copied = dict(source)
        for key in ("schema", "jsonSchema"):
            if copied.get(key) is not None:
                copied[key] = bounded_schema(copied[key], f"{label} {key}")
        return copied

    payload = fold(payload, outcome_id)
    incoming = payload.get("input")
    if _is_record(incoming):
        payload["input"] = fold(incoming, outcome_id)
    return payload


# voice.agent.call publishes a $3 maximum hold. Not live-verified.
CALL_HOLD_MICRO = 3_000_000
# A connected bulk row. A row that never connects is 0. Not a live price.
CONNECTED_ROW_MICRO = 100_000
# phone.number.setup shows a reported monthly rate. The fake figure is not a live price.
SETUP_MONTHLY_USD = 1.15


def _outcome_rows() -> list[dict]:
    return [
        {
            "id": "voice.agent.call",
            "effect_class": "external_irreversible",
            "mode": "async",
            "price_max_micro": CALL_HOLD_MICRO,
            "requires_approval": True,
            "personal_data": True,
            "available": True,
        },
        {
            "id": "phone.number.setup",
            "effect_class": "external_irreversible",
            "mode": "async",
            "price_max_micro": 0,
            "requires_approval": True,
            "personal_data": True,
            "available": True,
        },
    ]


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


KEY_MESSAGE = "Add PRAMAAN_API_KEY to .env"
IDEMPOTENCY_REQUIRED = "Idempotency-Key header is required for this capability."


def startup_notice(exc: PramaanError, unavailable: str) -> str:
    """The line to print. A missing idempotency key is not reported as unavailable.

    A spent key is rotated once before this runs, so a conflict here is a
    platform answer that did not come up either. It is reported the same way.
    """
    if exc.code in {"upstream_unavailable", "conflict"}:
        return unavailable
    if str(exc) == IDEMPOTENCY_REQUIRED:
        return IDEMPOTENCY_REQUIRED
    raise exc


class PramaanClient:
    """Plain HTTP. Actions send Idempotency-Key. Async jobs are polled until they finish."""

    def __init__(self, api_key: str, base_url: str | None = None, opener: Any = None) -> None:
        self.api_key = api_key or ""
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._opener = opener or urllib.request.urlopen
        self.monotonic = time.monotonic
        self.sleep = time.sleep

    def call(self, capability_id: str, body: dict | None = None, *, idempotency_key: str | None = None) -> Any:
        action = capability_id not in READS
        if action and not idempotency_key:
            idempotency_key = new_idempotency_key()
        payload_body = prepare_capability_body(capability_id, body)
        if capability_id in {"voice.tool.create", "voice.tool.update"} and not TOOL_ENDPOINT_ON_MAIN:
            payload_body.pop("endpoint", None)
        status, payload = self._request(
            "POST",
            f"/v1/capabilities/{capability_id}",
            payload_body,
            idempotency_key if action else None,
        )
        if status == 202:
            job_id = str((payload or {}).get("job_id") or "")
            if not job_id:
                raise PramaanError("internal", "The job did not return an id.")
            return self._wait(job_id)
        return payload

    def _wait(self, job_id: str) -> Any:
        deadline = self.monotonic() + RUN_LIMIT_SECONDS
        while True:
            _status, payload = self._request("GET", f"/v1/jobs/{job_id}", None, None)
            payload = payload or {}
            state = str(payload.get("status") or "")
            if state == "done":
                return payload.get("result")
            if state in {"failed", "cancelled", "error"}:
                _finished_job(payload)
            if self.monotonic() >= deadline:
                raise PramaanError("upstream_timeout", "This run took too long. Try again later.")
            self.sleep(2)

    def cancel_job(self, job_id: str) -> Any:
        """POST /v1/jobs/{id}/cancel. Stops new steps. Does not retry the run."""
        _status, payload = self._request(
            "POST", f"/v1/jobs/{job_id}/cancel", None, new_idempotency_key()
        )
        return payload

    def outcomes_list(self) -> Any:
        _status, payload = self._request("GET", "/v1/outcomes", None, None)
        return payload

    def outcomes_get(self, outcome_id: str) -> Any:
        _status, payload = self._request("GET", f"/v1/outcomes/{outcome_id}", None, None)
        return payload

    def outcomes_estimate(self, outcome_id: str, body: dict, *, idempotency_key: str | None = None) -> Any:
        body = prepare_outcome_body(outcome_id, body)
        status, payload = self._request(
            "POST", f"/v1/outcomes/{outcome_id}/estimate", body, idempotency_key
        )
        if status == 202:
            return self._wait(str((payload or {}).get("job_id") or ""))
        return payload

    def outcomes_run(
        self,
        outcome_id: str,
        body: dict,
        *,
        plan: str | None,
        idempotency_key: str,
    ) -> Any:
        body = prepare_outcome_body(outcome_id, body)
        current = self.outcomes_get(outcome_id)
        if not isinstance(current, dict) or current.get("available") is not True:
            raise OutcomeUnavailable()
        extra = {"Pramaan-Plan": plan} if plan else None
        status, payload = self._request(
            "POST", f"/v1/outcomes/{outcome_id}", body, idempotency_key, extra
        )
        if status == 202:
            job_id = str((payload or {}).get("job_id") or "")
            if not job_id:
                raise PramaanError("internal", "The job did not return an id.")
            return self._wait(job_id)
        return payload

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None,
        idempotency_key: str | None,
        extra: dict | None = None,
    ) -> tuple[int, Any]:
        if not self.api_key:
            raise PramaanError("unauthorized", KEY_MESSAGE)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if idempotency_key:
            req.add_header("Idempotency-Key", idempotency_key)
        for name, value in (extra or {}).items():
            if value:
                req.add_header(name, value)
        try:
            with self._opener(req, timeout=30) as res:
                raw = res.read().decode()
                return res.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            exc.close()
            if _not_available_message(_error_text(raw)):
                raise PramaanError("upstream_unavailable", NOT_AVAILABLE) from None
            if exc.code == 429:
                if method == "POST" and path.startswith("/v1/outcomes/") and "/estimate" not in path:
                    raise OutcomeLater(OPEN_RUNS_LATER) from None
                raise OutcomeLater() from None
            if exc.code == 502:
                raise OutcomeUnavailable() from None
            if exc.code == 400 and _max_tokens_configuration(raw):
                raise ReplyLimit() from None
            code = "failed"
            try:
                parsed = json.loads(raw) if raw else {}
                err = parsed.get("error") if isinstance(parsed, dict) else None
                if isinstance(err, dict) and err.get("reason") == "approval_required" and err.get("plan"):
                    raise ApprovalRequired(str(err["plan"])) from None
                if isinstance(err, dict) and err.get("code"):
                    code = str(err["code"])
                elif isinstance(parsed, dict) and parsed.get("code"):
                    code = str(parsed["code"])
            except json.JSONDecodeError:
                code = "failed"
            message = "The request failed."
            if code == "invalid_request" and _error_text(raw) == IDEMPOTENCY_REQUIRED:
                message = IDEMPOTENCY_REQUIRED
            raise PramaanError(code, message) from None


class FakePramaan:
    """In-memory stand-in. Records every call. Repeats a result for the same Idempotency-Key."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._idem: dict[str, tuple[str, str, Any]] = {}
        self.assistants: dict[str, dict] = {}
        self.tools: dict[str, dict] = {}
        self.squads: dict[str, dict] = {}
        self.knowledge: dict[str, dict] = {}
        self.voice_calls: dict[str, dict] = {}
        self.campaigns: dict[str, dict] = {}
        self.numbers: list[dict] = []
        self.opt_outs: set[str] = set()
        self.events: list[dict] = []
        self.outcome_state = "ready"
        self.hold_open = os.environ.get("PRAMAAN_CALL_OPEN") == "1"
        self._plans: dict[str, tuple[str, str]] = {}
        self._outcome_idem: dict[str, tuple[str, str, Any]] = {}
        self.jobs: dict[str, dict] = {}
        self.caller_numbers = os.environ.get("PRAMAAN_CALLER_NUMBERS") == "1"
        self.live_off = os.environ.get("PRAMAAN_LIVE_OFF") == "1"
        self.model_replies: list = []
        self.summary_cut = os.environ.get("PRAMAAN_SUMMARY_CUT") == "1"
        self.imported: dict[str, dict] = {}
        # Scripted run reports, by outcome id. Reproduces what the platform answers.
        self.run_results: dict[str, dict] = {}
        self.database_mode = "down"
        self.database_url = ""
        self.database_direct = ""
        self._database_given = False
        self.sign_in_mode = "down"
        self.sign_in_auth_url = "https://sign-in.example.test"
        self.sign_in_jwks_url = "https://sign-in.example.test/jwks"
        self.sign_in_domains: list[str] = []
        self.sign_in_tokens: dict[str, dict] = {}
        self.sign_in_users: dict[str, dict] = {}

    def call(self, capability_id: str, body: dict | None = None, *, idempotency_key: str | None = None) -> Any:
        body = prepare_capability_body(capability_id, body)
        action = capability_id not in READS
        if action and not idempotency_key:
            raise PramaanError("invalid_request", IDEMPOTENCY_REQUIRED)
        if action:
            previous = self._idem.get(idempotency_key)
            fingerprint = json.dumps(body, sort_keys=True)
            if previous:
                if previous[0] != capability_id or previous[1] != fingerprint:
                    raise PramaanError("conflict", "This idempotency key was already used.")
                return previous[2]
        self.calls.append({"id": capability_id, "body": body, "idempotency_key": idempotency_key})
        if self.live_off and capability_not_live(capability_id):
            raise PramaanError("upstream_unavailable", NOT_AVAILABLE)
        result = self._handle(capability_id, body)
        if action and idempotency_key:
            self._idem[idempotency_key] = (capability_id, json.dumps(body, sort_keys=True), result)
        return result

    def _scripted_model(self) -> Any:
        item = self.model_replies.pop(0)
        if item == "limit":
            raise ReplyLimit()
        if isinstance(item, dict):
            return item
        raise PramaanError("failed", "The model reply script is empty.")

    def _handle(self, capability_id: str, body: dict) -> Any:
        if capability_id in {"llm.chat", "llm.complete"} and self.model_replies:
            return self._scripted_model()
        if capability_id == "voice.tool.create":
            self.tools[body["name"]] = body
            return body
        if capability_id == "voice.tools.list":
            return {"items": list(self.tools.values())}
        if capability_id == "voice.assistant.create":
            _check_voice(body.get("voice"))
            self.assistants[body["name"]] = body
            return body
        if capability_id == "voice.assistant.get":
            return self.assistants[body["name"]]
        if capability_id == "voice.assistants.list":
            return {"items": list(self.assistants.values())}
        if capability_id == "voice.assistant.delete":
            self.assistants.pop(body["name"], None)
            return {"deleted": True}
        if capability_id == "voice.knowledge-base.create":
            sources = [key for key in ("text", "fileId", "urls") if body.get(key)]
            if len(sources) != 1:
                raise PramaanError("invalid_request", "Send exactly one text, fileId or urls.")
            if any(item.get("name") == body.get("name") for item in self.knowledge.values()):
                raise PramaanError("conflict", "That document base name is already used.")
            ref = "kb_" + uuid.uuid4().hex[:8]
            record = {"name": body["name"], "status": "ready", "sourceCount": 1, "ref": ref}
            self.knowledge[ref] = record
            return record
        if capability_id == "voice.knowledge-base.get":
            found = self.knowledge.get(body.get("ref") or "")
            if not found:
                raise PramaanError("not_found", "That document base is not saved.")
            return {"name": found["name"], "status": found["status"], "sourceCount": found["sourceCount"]}
        if capability_id == "voice.knowledge-bases.list":
            return {
                "results": [
                    {
                        "name": item["name"],
                        "status": item["status"],
                        "sourceCount": item["sourceCount"],
                        "ref": item["ref"],
                    }
                    for item in self.knowledge.values()
                ]
            }
        if capability_id == "voice.knowledge-base.delete":
            if body.get("confirm") is not True:
                raise PramaanError("invalid_request", "Confirm before this document base is deleted.")
            ref = body.get("ref")
            if not isinstance(ref, str) or ref not in self.knowledge:
                raise PramaanError("not_found", "That document base is not saved.")
            self.knowledge.pop(ref)
            return {"deleted": True}
        if capability_id == "voice.squad.create":
            self.squads[body["name"]] = body
            return body
        if capability_id == "voice.squads.list":
            return {"items": list(self.squads.values())}
        if capability_id == "messaging.opt-out.add":
            self.opt_outs.add(body["address"])
            return {"optOutId": "opt_1", "addressHint": body["address"][:3] + "…" + body["address"][-2:]}
        if capability_id == "messaging.opt-out.check":
            return {"blocked": body["address"] in self.opt_outs}
        if capability_id == "messaging.opt-outs.list":
            return {"items": [{"addressHint": item} for item in sorted(self.opt_outs)]}
        if capability_id == "voice.call.start-agent":
            ref = "call_" + uuid.uuid4().hex[:8]
            open_call = self.hold_open
            record = {
                "ref": ref,
                "status": "in_progress" if open_call else "completed",
                "endReason": None if open_call else "agent_ended",
                "durationSeconds": 0 if open_call else 12,
                "transcript": "Hello. This is a practice transcript.",
                "transcriptTruncated": False,
                "assistant": body.get("assistant"),
                "squad": body.get("squad"),
                "to": body.get("to"),
                "from": body.get("from"),
            }
            self.voice_calls[ref] = record
            return {
                "status": record["status"],
                "endReason": record["endReason"],
                "durationSeconds": record["durationSeconds"],
                "transcript": record["transcript"],
                "transcriptTruncated": False,
                "ref": ref,
            }
        if capability_id == "voice.calls.list":
            return {"items": list(self.voice_calls.values())}
        if capability_id == "voice.call.get":
            return self.voice_calls[body["ref"]]
        if capability_id == "voice.call.end":
            found = self.voice_calls.get(body["ref"])
            if found:
                found["status"] = "cancelled"
                found["endReason"] = "cancelled"
            return {"status": "cancelled"}
        if capability_id == "phone.numbers.search":
            return {
                "results": [
                    {"phoneNumber": "+14155550100", "channels": ["voice"], "locality": "San Francisco"}
                ],
                "country": body.get("country", "US"),
            }
        if capability_id == "phone.number.purchase":
            if body.get("confirm") is not True:
                raise PramaanError("invalid_request", "Confirm the purchase.")
            number = body.get("phoneNumber") or "+14155550100"
            row = {"phoneNumber": number, "status": "active"}
            self.numbers.append(row)
            return {"ref": "purchase_1", "phoneNumber": number, "status": "active"}
        if capability_id == "phone.numbers.list":
            return {
                "results": [
                    {
                        "phoneNumber": row["phoneNumber"],
                        "status": row.get("status", "active"),
                        "channels": ["voice"],
                    }
                    for row in self.numbers
                    if "phoneNumber" in row
                ]
            }
        if capability_id == "voice.number.import":
            return self._import_caller(body)
        if capability_id == "voice.number.disconnect":
            return self._disconnect_caller(body)
        if capability_id == "voice.number.create":
            row = {
                "number": "+14155550199",
                "name": body.get("name"),
                "type": "telephone",
                "inboundEnabled": False,
                "outboundEnabled": False,
                "status": "active",
            }
            self.numbers.append(row)
            return row
        if capability_id == "voice.number.update":
            patch = body.get("patch") or {}
            return {
                "number": body["number"],
                "type": "telephone",
                "inboundEnabled": "assistant" in patch or "squad" in patch,
                "outboundEnabled": False,
                "status": "active",
                **({ "assistant": patch["assistant"] } if "assistant" in patch else {}),
                **({ "squad": patch["squad"] } if "squad" in patch else {}),
            }
        if capability_id == "voice.numbers.list":
            return {"items": [n for n in self.numbers if "number" in n]}
        if capability_id == "voice.call.start-agent-bulk":
            return self._bulk_agent_calls(body)
        if capability_id == "voice.campaign.create":
            ref = "campaign_1"
            self.campaigns[ref] = {"ref": ref, "state": "running", "name": body.get("name"), "rows": body.get("rows", [])}
            return {"ref": ref, "state": "running"}
        if capability_id == "voice.campaigns.list":
            return {"items": list(self.campaigns.values())}
        if capability_id == "voice.browser-session.create":
            return {
                "connectUrl": "wss://sessions.invalid/socket",
                "credential": "practice-credential",
                "expiresAt": "2099-01-01T00:00:00Z",
                "number": body.get("from"),
                "ref": "session_1",
            }
        if capability_id == "audio.voices.list":
            return {"items": [{"ref": "voice_1", "name": "Default"}]}
        if capability_id == "llm.chat":
            blob = json.dumps(body.get("messages") or [])
            if self.summary_cut and "Summarise" in blob:
                return {"text": "The caller was", "finishReason": "length"}
            if "Summarise" in blob:
                return {"text": "The caller was greeted."}
            return {"text": json.dumps({"passed": True, "reason": "The caller was greeted."})}
        if capability_id == "voice.call.get-details":
            found = self.voice_calls.get(body["ref"])
            if found is None:
                raise PramaanError("not_found", "That call is not saved.")
            return {
                "status": found.get("status"),
                "connectedSeconds": found.get("durationSeconds", 0),
                "startedAt": "2026-10-12T15:00:00Z",
                "endedAt": "2026-10-12T15:00:12Z",
                "direction": "outbound",
            }
        if capability_id == "voice.call.legs.list":
            return {"results": [{"position": 1, "status": "completed", "connectedSeconds": 12, "answeredBy": "unknown"}]}
        if capability_id == "voice.call.recording.get":
            return {"recording": {"id": "file_practice"}, "durationSeconds": 12}
        if capability_id == "voice.assistant.update":
            _check_voice((body.get("patch") or {}).get("voice"))
            current = self.assistants.get(body["name"], {"name": body["name"]})
            current.update(body.get("patch") or {})
            self.assistants[body["name"]] = current
            return current
        if capability_id == "voice.assistant.delete":
            self.assistants.pop(body["name"], None)
            return {"deleted": True}
        if capability_id == "voice.tool.delete":
            self.tools.pop(body["name"], None)
            return {"deleted": True}
        if capability_id == "voice.tool.update":
            current = self.tools.get(body["name"], {"name": body["name"]})
            current.update(body.get("patch") or {})
            self.tools[body["name"]] = current
            return current
        if capability_id == "voice.squad.delete":
            self.squads.pop(body["name"], None)
            return {"deleted": True}
        if capability_id == "messaging.opt-out.remove":
            if body.get("confirm") is not True:
                raise PramaanError("invalid_request", "Confirm the removal.")
            self.opt_outs.discard(body["address"])
            return {"removed": True}
        if capability_id == "voice.campaign.pause":
            row = self.campaigns[body["ref"]]
            row["state"] = "paused"
            return {"state": "paused"}
        if capability_id == "voice.campaign.resume":
            row = self.campaigns[body["ref"]]
            row["state"] = "running"
            return {"state": "running"}
        if capability_id == "voice.campaign.finish":
            row = self.campaigns[body["ref"]]
            row["state"] = "finished"
            return {"state": "finished"}
        if capability_id == "voice.campaign.get":
            return self.campaigns[body["ref"]]
        if capability_id == "voice.campaign.calls.list":
            row = self.campaigns.get(body["ref"], {})
            return {"items": list(row.get("rows") or [])}
        if capability_id == "phone.number.availability.check":
            return {"available": True, "country": body.get("country", "US"), "type": body.get("type", "local")}
        if capability_id == "phone.number.purchase-status":
            return {"ref": body.get("ref"), "status": "active", "phoneNumber": "+14155550100", "monthlyPriceUsd": SETUP_MONTHLY_USD}
        if capability_id == "phone.number.identity.requirements.get":
            return {"country": body.get("country", "US"), "type": body.get("type", "local"), "fields": []}
        if capability_id == "phone.number.voice.configure":
            return {"phoneNumber": body.get("phoneNumber"), "enabled": True}
        if capability_id == "calendar.event.create":
            self.events.append(body)
            return {"ref": "event_1"}
        if capability_id == "audio.transcribe":
            return {"text": "Practice transcript from audio.", "segments": []}
        if capability_id == "db.postgres.provision":
            return self._database_provision()
        if capability_id == "auth.backend.provision":
            return self._sign_in_provision()
        if capability_id == "auth.backend.domains.set":
            return self._sign_in_domains(body)
        if capability_id == "auth.token.verify":
            return self._sign_in_verify(body)
        if capability_id == "auth.user.get":
            return self._sign_in_user(body)
        if capability_id == "auth.user.delete":
            return self._sign_in_delete(body)
        raise PramaanError("unsupported", "This capability is not used by the fake client.")

    def _database_provision(self) -> dict:
        if self.database_mode == "down":
            raise PramaanError("upstream_unavailable", NOT_AVAILABLE)
        if not self._database_given:
            self._database_given = True
            return {
                "status": "ready",
                "created": True,
                "database": "app",
                "role": "app_owner",
                "connectionString": self.database_url,
                "directConnectionString": self.database_direct or self.database_url,
            }
        return {"status": "ready", "created": False, "database": "app", "role": "app_owner"}

    def _sign_in_provision(self) -> dict:
        if self.sign_in_mode != "ready":
            raise PramaanError("upstream_unavailable", NOT_AVAILABLE)
        return {
            "status": "ready",
            "authUrl": self.sign_in_auth_url,
            "jwksUrl": self.sign_in_jwks_url,
        }

    def _sign_in_domains(self, body: dict) -> dict:
        domains = body.get("domains") if isinstance(body, dict) else None
        if not isinstance(domains, list) or not domains or len(domains) > 20:
            raise PramaanError("invalid_request", "Send the public address.")
        cleaned = []
        for item in domains:
            if not isinstance(item, str) or not item.startswith("https://"):
                raise PramaanError("invalid_request", "Send a public https address.")
            cleaned.append(item)
        self.sign_in_domains = cleaned
        return {"allowedDomains": cleaned}

    def _sign_in_verify(self, body: dict) -> dict:
        token = body.get("token") if isinstance(body, dict) else None
        row = self.sign_in_tokens.get(token) if isinstance(token, str) else None
        if not isinstance(row, dict) or row.get("valid") is not True or not row.get("userId"):
            return {"valid": False}
        return {
            "valid": True,
            "userId": row["userId"],
            "sessionId": row.get("sessionId") or "sess_1",
            "orgId": row.get("orgId"),
            "expiresAt": row.get("expiresAt"),
        }

    def _sign_in_user(self, body: dict) -> dict:
        user_id = body.get("userId") if isinstance(body, dict) else ""
        user = self.sign_in_users.get(user_id) if isinstance(user_id, str) else None
        if not isinstance(user, dict):
            raise PramaanError("not_found", "No such user.")
        result = {"userId": user_id}
        if isinstance(user.get("email"), str):
            result["email"] = user["email"]
        return result

    def _sign_in_delete(self, body: dict) -> dict:
        if not isinstance(body, dict) or body.get("confirm") is not True:
            raise PramaanError("invalid_request", "Confirm before this account is deleted.")
        user_id = body.get("userId")
        if not isinstance(user_id, str) or not user_id:
            raise PramaanError("invalid_request", "Send the account to delete.")
        self.sign_in_users.pop(user_id, None)
        self.sign_in_tokens = {
            token: row for token, row in self.sign_in_tokens.items() if row.get("userId") != user_id
        }
        return {"deleted": True}

    def _bulk_agent_calls(self, body: dict) -> dict:
        rows = list(body.get("rows") or [])
        if not rows or len(rows) > 100:
            raise PramaanError("invalid_request", "A campaign includes 1 to 100 people.")
        results = [self._bulk_row(row) for row in rows]
        failed = sum(1 for row in results if row.get("error"))
        found = sum(1 for row in results if row.get("found") and not row.get("error"))
        return {
            "results": results,
            "summary": {
                "total": len(results),
                "found": found,
                "notFound": len(results) - found - failed,
                "failed": failed,
            },
            "costMicro": sum(int(row.get("costMicro") or 0) for row in results),
        }

    def _bulk_row(self, row: dict) -> dict:
        row_id = str(row.get("id") or "")
        if row.get("squad") or row.get("scheduleAt") or not row.get("assistant"):
            message = (
                "Schedules and squads are not supported in a campaign."
                if row.get("squad") or row.get("scheduleAt")
                else "Send exactly one assistant or squad name."
            )
            return {
                "id": row_id,
                "found": False,
                "costMicro": 0,
                "error": {"code": "invalid_request", "message": message},
            }
        if row_id == "away":
            return {
                "id": row_id,
                "found": True,
                "status": "no_answer",
                "endReason": "no_answer",
                "durationSeconds": 0,
                "transcript": "",
                "transcriptTruncated": False,
                "costMicro": 0,
                "ref": "call_" + uuid.uuid4().hex[:8],
            }
        placed = self._handle(
            "voice.call.start-agent",
            {
                "to": row.get("to"),
                "from": row.get("from"),
                "assistant": row.get("assistant"),
                "variables": row.get("variables") or {},
                "maxDurationSeconds": row.get("maxDurationSeconds") or 120,
                "recording": bool(row.get("recording")),
            },
        )
        transcript = str(placed.get("transcript") or "")
        status = placed.get("status")
        duration = int(placed.get("durationSeconds") or 0)
        connected = status not in {"no_answer", "busy", "failed"} and duration > 0
        return {
            "id": row_id,
            "found": True,
            "status": status,
            "endReason": placed.get("endReason"),
            "durationSeconds": duration,
            "transcript": transcript[:10000],
            "transcriptTruncated": len(transcript) > 10000 or bool(placed.get("transcriptTruncated")),
            "ref": placed.get("ref"),
            "costMicro": CONNECTED_ROW_MICRO if connected else 0,
        }

    def outcomes_list(self) -> dict:
        return {"results": [self._summary(row) for row in _outcome_rows()]}

    def outcomes_get(self, outcome_id: str) -> dict:
        for row in _outcome_rows():
            if row["id"] == outcome_id:
                shown = self._summary(row)
                shown["steps"] = _outcome_steps(outcome_id)
                return shown
        raise PramaanError("failed", "That outcome is not in the catalogue.")

    def _summary(self, row: dict) -> dict:
        shown = dict(row)
        if self.outcome_state == "hidden":
            shown["available"] = False
        return shown

    def outcomes_estimate(self, outcome_id: str, body: dict, *, idempotency_key: str | None = None) -> dict:
        body = prepare_outcome_body(outcome_id, body)
        if self.live_off and outcome_not_live(outcome_id):
            self.calls.append(
                {"id": "outcome.estimate", "outcome": outcome_id, "body": body, "idempotency_key": idempotency_key}
            )
            raise PramaanError("upstream_unavailable", NOT_AVAILABLE)
        current = self.outcomes_get(outcome_id)
        if self.outcome_state == "down":
            raise OutcomeUnavailable()
        if self.outcome_state == "later":
            raise OutcomeLater()
        plan = "plan_" + uuid.uuid4().hex[:12]
        fingerprint = json.dumps(body, sort_keys=True)
        self._plans[plan] = (outcome_id, fingerprint)
        estimate = {
            "plan": plan,
            "price_max_micro": current.get("price_max_micro"),
            "can_run": current.get("available") is True,
            "requires_approval": True,
            "needs": [],
            "steps": current.get("steps"),
        }
        if outcome_id == "phone.number.setup":
            estimate["reportedMonthlyUsd"] = SETUP_MONTHLY_USD
        self.calls.append({"id": "outcome.estimate", "outcome": outcome_id, "body": body, "idempotency_key": idempotency_key})
        return estimate

    def outcomes_run(
        self,
        outcome_id: str,
        body: dict,
        *,
        plan: str | None,
        idempotency_key: str,
    ) -> Any:
        body = prepare_outcome_body(outcome_id, body)
        if self.live_off and outcome_not_live(outcome_id):
            self._note_run(outcome_id, body, plan, idempotency_key)
            raise PramaanError("upstream_unavailable", NOT_AVAILABLE)
        if self.summary_cut and outcome_id == "voice.agent.call":
            raise OutcomeUnavailable()
        if self.outcome_state == "down":
            raise OutcomeUnavailable()
        if self.outcome_state == "later":
            self._note_run(outcome_id, body, plan, idempotency_key)
            raise OutcomeLater(OPEN_RUNS_LATER)
        if self.outcome_state == "unknown":
            self._note_run(outcome_id, body, plan, idempotency_key)
            raise OutcomeCheck()
        if self.outcome_state == "timeout":
            self._note_run(outcome_id, body, plan, idempotency_key)
            raise PramaanError("upstream_timeout", "This run took too long. Try again later.")
        current = self.outcomes_get(outcome_id)
        if current.get("available") is not True:
            raise OutcomeUnavailable()
        if not plan or plan not in self._plans or self._plans[plan][0] != outcome_id:
            raise PramaanError("approval_required", "Estimate this outcome and confirm it.")
        if self._plans[plan][1] != json.dumps(body, sort_keys=True):
            raise PramaanError("conflict", "Estimate again. The input changed.")
        previous = self._outcome_idem.get(idempotency_key)
        fingerprint = outcome_id + json.dumps(body, sort_keys=True)
        if previous:
            if previous[0] != fingerprint:
                raise PramaanError("conflict", "This idempotency key was already used.")
            return previous[2]
        report = self._run_outcome(outcome_id, body)
        self._outcome_idem[idempotency_key] = (fingerprint, plan, report)
        self._note_run(outcome_id, body, plan, idempotency_key)
        return report

    def _import_caller(self, body: dict) -> dict:
        if body.get("confirm") is not True:
            raise PramaanError("invalid_request", "Confirm before this number is used for agent calls.")
        number = str(body.get("number") or "")
        if not PHONE.match(number):
            raise PramaanError("invalid_request", "Use a phone number in international form, such as +14155550100.")
        if not self.caller_numbers:
            raise PramaanError("upstream_unavailable", NOT_AVAILABLE)
        if number in self.imported:
            raise PramaanError("conflict", "This number is already connected.")
        assistant = body.get("assistant")
        if assistant is not None and assistant not in self.assistants:
            raise PramaanError("not_found", "That agent is not saved.")
        row = {
            "number": number,
            "type": "telephone",
            "inboundEnabled": assistant is not None,
            "outboundEnabled": True,
            "status": "active",
        }
        if body.get("name"):
            row["name"] = body["name"]
        if assistant is not None:
            row["assistant"] = assistant
        self.imported[number] = row
        return row

    def _disconnect_caller(self, body: dict) -> dict:
        if body.get("confirm") is not True:
            raise PramaanError("invalid_request", "Confirm before this caller ID is disconnected.")
        number = str(body.get("number") or "")
        if not PHONE.match(number):
            raise PramaanError("invalid_request", "Use a phone number in international form, such as +14155550100.")
        if not self.caller_numbers:
            raise PramaanError("upstream_unavailable", NOT_AVAILABLE)
        if number not in self.imported:
            raise PramaanError("not_found", "That caller ID is not connected.")
        self.imported.pop(number)
        return {"disconnected": True}

    def _note_run(self, outcome_id: str, body: dict, plan: str | None, idempotency_key: str) -> None:
        self.calls.append(
            {
                "id": "outcome.run",
                "outcome": outcome_id,
                "body": body,
                "plan": plan,
                "idempotency_key": idempotency_key,
            }
        )

    def cancel_job(self, job_id: str) -> dict:
        self.calls.append({"id": "job.cancel", "body": {"job_id": job_id}, "idempotency_key": None})
        job = self.jobs.get(job_id) or {"job_id": job_id, "status": "running", "steps": []}
        job["status"] = "cancelled"
        self.jobs[job_id] = job
        return {"status": "cancelled", "result": {"steps": list(job.get("steps") or [])}}

    def _run_outcome(self, outcome_id: str, body: dict) -> dict:
        scripted = self.run_results.get(outcome_id)
        if scripted is not None:
            return scripted
        incoming = body.get("input") if isinstance(body.get("input"), dict) else body
        if outcome_id == "voice.agent.call":
            placed = self._handle(
                "voice.call.start-agent",
                {
                    "to": incoming.get("to"),
                    "from": incoming.get("from"),
                    "assistant": incoming.get("assistant"),
                    "squad": incoming.get("squad"),
                    "variables": incoming.get("variables") or {},
                    "maxDurationSeconds": incoming.get("maxDurationSeconds") or 120,
                    "recording": bool(incoming.get("recording")),
                },
            )
            self.calls.append({"id": "voice.call.start-agent", "body": incoming, "idempotency_key": None, "via": "voice.agent.call"})
            return {
                "status": "done",
                "completeness": "complete",
                "result": {
                    "status": placed["status"],
                    "endedReason": placed.get("endReason"),
                    "durationSeconds": placed.get("durationSeconds"),
                    "transcript": placed.get("transcript"),
                    "ref": placed.get("ref"),
                    "summary": [{"text": "The caller was greeted.", "lines": [1]}],
                    "charge": "The charge appears on the platform bill.",
                },
                "steps": _outcome_steps(outcome_id),
                "cost": {"hold_micro": CALL_HOLD_MICRO, "charged_micro": 0},
            }
        if outcome_id == "phone.number.setup":
            number = incoming.get("phoneNumber") or "+14155550100"
            purchased = self._handle(
                "phone.number.purchase",
                {
                    "country": incoming.get("country") or "US",
                    "type": incoming.get("type") or "local",
                    "phoneNumber": number,
                    "confirm": True,
                },
            )
            return {
                "status": "done",
                "completeness": "complete",
                "result": {
                    "status": "active",
                    "number": purchased["phoneNumber"],
                    "country": incoming.get("country") or "US",
                    "ref": purchased["ref"],
                    "voiceEnabled": True,
                    "monthlyPriceUsd": SETUP_MONTHLY_USD,
                    "stillNeeded": [],
                },
                "steps": _outcome_steps(outcome_id),
                "cost": {"hold_micro": 0, "charged_micro": 0},
            }
        raise PramaanError("failed", "That outcome is not in the catalogue.")


def capability_not_live(capability_id: str) -> bool:
    """Groups the live server answers as not available yet. email.send estimates stay open."""
    if capability_id in {
        "document.convert",
        "signature.digest.sign",
        "signature.timestamp.create",
    }:
        return True
    if capability_id.startswith("email.") and capability_id != "email.send":
        return True
    return capability_id.startswith(
        (
            "voice.",
            "phone.",
            "audio.",
            "calendar.",
            "law.",
            "mailbox.",
            "domains.registration.",
            "identity.verification.",
        )
    )


def outcome_not_live(outcome_id: str) -> bool:
    """Outcomes this template runs that sit on a group which is not available yet."""
    return outcome_id in {"voice.agent.call", "phone.number.setup"}


def _not_available_message(text: str) -> bool:
    folded = " ".join(text.lower().split()).rstrip(".")
    return "not available yet" in folded or "not yet available" in folded


def _max_tokens_configuration(raw: str) -> bool:
    """A 400 whose message names maxTokens. An empty cut-off is not charged and will not succeed again."""
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return False
    err = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(err, dict) or str(err.get("code") or "") != "invalid_request":
        return False
    return "maxTokens" in str(err.get("message") or "")


def _error_text(raw: str) -> str:
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return ""
    err = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or "")
    return ""


def _check_voice(value: Any) -> None:
    if value is not None and value not in VOICE_KEYS:
        raise PramaanError("invalid_request", "Pick a listed voice.")


def _finished_job(payload: dict) -> None:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    steps = result.get("steps") if isinstance(result.get("steps"), list) else []
    if any(isinstance(step, dict) and step.get("effect") == "unknown" for step in steps):
        raise OutcomeCheck()
    err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = str(err.get("code") or "failed")
    if code == "upstream_timeout":
        raise PramaanError("upstream_timeout", "This run took too long. Try again later.")
    raise PramaanError(code, "The job failed.")


def _outcome_steps(outcome_id: str) -> list[dict]:
    if outcome_id == "voice.agent.call":
        names = [
            "voice.numbers.list",
            "voice.assistants.list",
            "voice.call.start-agent",
            "llm.complete",
        ]
    else:
        names = [
            "phone.numbers.search",
            "phone.number.availability.check",
            "phone.number.purchase",
            "phone.number.identity.requirements.get",
            "phone.number.purchase-status",
            "phone.number.voice.configure",
        ]
    return [{"id": name, "capability": name, "status": "pending", "effect": "none"} for name in names]


def client_from_env() -> PramaanClient | FakePramaan:
    if os.environ.get("PRAMAAN_FAKE") == "1":
        return FakePramaan()
    key = os.environ.get("PRAMAAN_API_KEY", "")
    return PramaanClient(key, os.environ.get("PRAMAAN_BASE_URL"))
