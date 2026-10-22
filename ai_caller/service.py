"""Publish agents and place calls. Every action goes through the one client."""

from __future__ import annotations

import os
import copy
import struct
import time
import uuid
from typing import Any

from ai_caller.compile import plan, squad_body
from ai_caller.csv_rows import parse_recipients
from ai_caller.model import Agent, Squad, check_duration, check_name, check_phone, listed_voice
from ai_caller.pramaan import (
    ApprovalRequired,
    CALL_HOLD_MICRO,
    FakePramaan,
    NOT_AVAILABLE,
    OptOut,
    OutcomeLater,
    OutcomeUnavailable,
    REPLY_LIMIT,
    PramaanClient,
    PramaanError,
    read_model_reply,
    new_idempotency_key,
)
from ai_caller.store import Store, agent_record

Client = PramaanClient | FakePramaan

LIMITS = {
    "callSeconds": "10 to 600. The default is 120.",
    "campaignRows": 100,
    "toolParameters": 20,
    "argumentCharacters": 1000,
    "hostedCodeCharacters": 50000,
    "toolRequestsPerCall": 60,
    "transcriptCharacters": 100000,
}


def _items(payload: Any) -> list:
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return payload["items"]
    return []


def _basis(ground: str) -> dict:
    return {"ground": ground}


def not_placed_reason(result: Any) -> str:
    """A run report that says nothing was placed is a failure, not a success.

    The live platform answers `status: not_placed` with `stillNeeded` items when
    a run finished without taking effect. The reasons are plain sentences and
    are shown to the operator as they came.
    """
    if not isinstance(result, dict):
        return ""
    status = str(result.get("status") or "").lower()
    still = [item for item in result.get("stillNeeded") or [] if isinstance(item, dict)]
    if status and status != "not_placed" and not still:
        return ""
    if not status and not still:
        return ""
    parts = [str(item.get("detail") or "").strip() for item in still]
    parts = [part for part in parts if part]
    if not parts:
        return "This was not placed. Nothing changed."
    return "This was not placed. " + " ".join(parts)


def dollars(micro: int | None) -> str:
    if micro is None:
        return ""
    return f"${micro / 1_000_000:.2f}"


def silent_wav() -> bytes:
    rate = 8000
    frames = 800
    data = b"\x00\x00" * frames
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(data),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        rate,
        rate * 2,
        2,
        16,
        b"data",
        len(data),
    ) + data


class Service:
    def __init__(self, client: Client, store: Store | None = None) -> None:
        self.client = client
        self._number_reviews = {}
        self.store = store or Store(os.environ.get("AI_CALLER_DB") or ":memory:", os.environ.get("AI_CALLER_OWNER") or "local")

    def publish(self, agent: Agent, squads: list[str] | None = None) -> dict:
        results = []
        ref = agent.knowledge_base_ref
        for capability_id, body in plan(agent):
            payload = dict(body)
            if capability_id == "voice.assistant.create" and ref:
                payload["knowledgeBaseRef"] = ref
            result = self.client.call(capability_id, payload, idempotency_key=new_idempotency_key())
            if capability_id == "voice.knowledge-base.create" and isinstance(result, dict) and result.get("ref"):
                ref = str(result["ref"])
                self.store.save_knowledge(
                    {
                        "name": result.get("name") or agent.name,
                        "status": result.get("status") or "ready",
                        "sourceCount": result.get("sourceCount") or 1,
                        "ref": ref,
                    }
                )
            results.append({"capability": capability_id, "result": result})
        agent.knowledge_base_ref = ref
        self._save_agent(agent, squads)
        return {"name": agent.name, "steps": results}

    def _save_agent(self, agent: Agent, squads: list[str] | None) -> dict:
        record = agent_record(agent, squads)
        if agent.knowledge_base_ref:
            record["knowledgeBaseRef"] = agent.knowledge_base_ref
            known = self.store.get_knowledge(agent.knowledge_base_ref)
            if known:
                record["knowledgeName"] = known.get("name")
        self.store.save_agent(record)
        return record

    def update_agent(self, agent: Agent, squads: list[str] | None = None) -> dict:
        if self.store.get_agent(agent.name) is None:
            raise KeyError("That agent is not saved.")
        result = self.client.call(
            "voice.assistant.update",
            {
                "name": agent.name,
                "patch": {
                    "prompt": agent.prompt,
                    "language": agent.language,
                    "maxDurationSeconds": agent.max_duration_seconds,
                    **({"greeting": agent.greeting} if agent.greeting else {}),
                    **(
                        {"knowledgeBaseRef": agent.knowledge_base_ref}
                        if agent.knowledge_base_ref
                        else {}
                    ),
                    **({"voice": listed_voice(agent.voice)} if listed_voice(agent.voice) else {}),
                },
            },
            idempotency_key=new_idempotency_key(),
        )
        self._save_agent(agent, squads)
        return {"name": agent.name, "result": result}

    def delete_agent(self, name: str) -> dict:
        check_name(name)
        if self.store.get_agent(name) is None:
            raise KeyError("That agent is not saved.")
        result = self.client.call(
            "voice.assistant.delete", {"name": name, "confirm": True}, idempotency_key=new_idempotency_key()
        )
        self.store.delete_agent(name)
        return result

    def publish_squad(self, squad: Squad) -> dict:
        result = self.client.call(
            "voice.squad.create", squad_body(squad), idempotency_key=new_idempotency_key()
        )
        members = [name for name, _targets in squad.members]
        self.store.save_squad({"name": squad.name, "members": members})
        for name in members:
            current = self.store.get_agent(name)
            if current:
                names = list(current.get("squads") or [])
                if squad.name not in names:
                    names.append(squad.name)
                current["squads"] = names
                self.store.save_agent(current)
        return result

    def save_tool(self, body: dict) -> dict:
        from ai_caller.model import Parameter, Tool
        from ai_caller.compile import tool_body

        params = [
            Parameter(
                name=item["name"],
                type=item.get("type", "string"),
                description=item.get("description", ""),
                required=bool(item.get("required", False)),
            )
            for item in body.get("parameters") or []
        ]
        tool = Tool(
            name=body["name"],
            description=body.get("description") or "A tool.",
            kind=body.get("kind") or "function",
            parameters=params,
            code=body.get("code") or None,
            endpoint=body.get("endpoint") or None,
            timeout_seconds=int(body.get("timeoutSeconds") or 10),
        )
        payload = tool_body(tool)
        capability_id = "voice.tool.update" if self.store.get_tool(tool.name) else "voice.tool.create"
        if capability_id == "voice.tool.update":
            sent = {"name": tool.name, "patch": {key: value for key, value in payload.items() if key != "name"}}
        else:
            sent = payload
        result = self.client.call(capability_id, sent, idempotency_key=new_idempotency_key())
        record = {
            "name": tool.name,
            "description": tool.description,
            "kind": tool.kind,
            "code": tool.code,
            "endpoint": tool.endpoint,
            "parameters": payload["parameters"],
            "timeoutSeconds": tool.timeout_seconds,
        }
        self.store.save_tool(record)
        return {"name": tool.name, "result": result}

    def delete_tool(self, name: str) -> dict:
        check_name(name)
        if self.store.get_tool(name) is None:
            raise KeyError("That tool is not saved.")
        result = self.client.call("voice.tool.delete", {"name": name, "confirm": True}, idempotency_key=new_idempotency_key())
        self.store.delete_tool(name)
        return result

    def test_tool(self, name: str, arguments: dict | None) -> dict:
        """Check the argument shape against the fake. Hosted code stays on the platform."""
        if self.store.get_tool(name) is None:
            raise KeyError("That tool is not saved.")
        values = arguments or {}
        if not isinstance(values, dict):
            raise ValueError("Arguments must be a set of named values.")
        if len(values) > 20:
            raise ValueError("A tool has at most 20 arguments.")
        for value in values.values():
            if isinstance(value, bool):
                continue
            if isinstance(value, str):
                if len(value) > 1000:
                    raise ValueError("An argument is too long.")
                continue
            if isinstance(value, (int, float)):
                continue
            raise ValueError("An argument must be text, a number, or yes or no.")
        return {"result": "Received."}

    def list_agents(self) -> list:
        return self.store.list_agents()

    def list_tools(self) -> list:
        return self.store.list_tools()

    def list_squads(self) -> list:
        return self.store.list_squads()

    def list_calls(self, *, status: str | None = None, assistant: str | None = None, q: str | None = None) -> list:
        return self.store.list_calls(status=status, assistant=assistant, q=q)

    def list_campaigns(self) -> list:
        saved = self.store.list_campaigns()
        return saved or _items(self.client.call("voice.campaigns.list", {"limit": 25}))

    def list_numbers(self) -> dict:
        """Rented numbers stay listed while the included-numbers part is switched off."""
        purchased = self.client.call("phone.numbers.list", {})
        rows: list = []
        if isinstance(purchased, dict):
            rows.extend(purchased.get("results") or [])
        note = ""
        try:
            rows.extend(_items(self.client.call("voice.numbers.list", {"limit": 25})))
        except PramaanError as exc:
            if exc.code != "upstream_unavailable":
                raise
            note = NOT_AVAILABLE
        return {"items": rows, "includedNote": note}

    def search_numbers(self, country: str, area_code: str | None = None) -> dict:
        body: dict = {"country": country, "type": "local", "limit": 20}
        if area_code:
            body["areaCode"] = area_code
        found = self.client.call("phone.numbers.search", body)
        if isinstance(found, dict):
            found.pop("monthlyPriceUsd", None)
            for row in found.get("results") or []:
                if isinstance(row, dict):
                    row.pop("monthlyPriceUsd", None)
        return found

    def estimate_number_setup(
        self,
        country: str,
        number_type: str = "local",
        phone_number: str | None = None,
        area_code: str | None = None,
    ) -> dict:
        incoming: dict = {"country": country, "type": number_type, "wantsSms": False}
        if phone_number:
            check_phone(phone_number)
            incoming["phoneNumber"] = phone_number
        if area_code:
            incoming["areaCode"] = area_code
        body = {"input": incoming, "options": {}, "basis": _basis("contract")}
        try:
            estimate = self.client.outcomes_estimate(
                "phone.number.setup", body, idempotency_key=new_idempotency_key()
            )
        except OutcomeUnavailable:
            return {
                "fallback": True,
                "capability": "phone.number.purchase",
                "body": body,
                "sentence": self._buy_sentence(phone_number or "the first matching number", country, None),
            }
        if estimate.get("plan"):
            self._number_reviews[estimate["plan"]] = (time.monotonic(), copy.deepcopy(body))
        rate = estimate.get("reportedMonthlyUsd")
        rate_text = None if rate is None else f"{rate:.2f}"
        return {
            "fallback": False,
            "plan": estimate.get("plan"),
            "approvalExpiresInSeconds": 900,
            "body": body,
            "priceMaxMicro": estimate.get("price_max_micro"),
            **({"priceTypicalMicro": estimate["price_typical_micro"]} if estimate.get("price_typical_micro") is not None else {}),
            "reportedMonthlyUsd": rate,
            "sentence": self._buy_sentence(phone_number or "the first matching number", country, rate_text),
        }

    def confirm_number_setup(self, estimate: dict) -> dict:
        if not estimate.get("fallback") and estimate.get("plan"):
            saved = self._number_reviews.get(estimate["plan"])
            if not saved or time.monotonic() - saved[0] >= 900 or saved[1] != estimate.get("body"):
                raise PramaanError("approval_expired", "Search again and review a fresh plan before buying.")
            try:
                report = self.client.outcomes_run(
                    "phone.number.setup",
                    estimate["body"],
                    plan=estimate["plan"],
                    idempotency_key=new_idempotency_key(),
                )
                result = report.get("result") if isinstance(report, dict) else None
                result = result if isinstance(result, dict) else report
                reason = not_placed_reason(result)
                if reason:
                    raise PramaanError("failed", reason)
                self._number_reviews.pop(estimate["plan"], None)
                return result
            except ApprovalRequired as exc:
                self._number_reviews.pop(estimate["plan"], None)
                self._number_reviews[exc.plan] = (time.monotonic(), copy.deepcopy(estimate["body"]))
                return {"approvalRequired": True, "estimate": {**estimate, "plan": exc.plan, "approvalExpiresInSeconds": 900}}
            except OutcomeUnavailable:
                pass
        country = estimate["body"]["input"]["country"]
        number_type = estimate["body"]["input"].get("type") or "local"
        phone = estimate["body"]["input"].get("phoneNumber")
        payload: dict = {"country": country, "type": number_type, "confirm": True}
        if phone:
            payload["phoneNumber"] = phone
        return self.client.call("phone.number.purchase", payload, idempotency_key=new_idempotency_key())

    def buy_number(self, country: str, number_type: str = "local") -> dict:
        return self.client.call(
            "phone.number.purchase",
            {"country": country, "type": number_type, "confirm": True},
            idempotency_key=new_idempotency_key(),
        )

    def import_caller_number(
        self,
        number: str,
        *,
        name: str | None = None,
        assistant: str | None = None,
        confirm: bool = False,
    ) -> dict:
        check_phone(number)
        if confirm is not True:
            raise ValueError("Confirm before this number is used for agent calls.")
        if name is not None:
            name = name.strip()
            if not name:
                name = None
            elif len(name) > 40:
                raise ValueError("The display name must be at most 40 characters.")
        if assistant:
            check_name(assistant)
        body: dict = {"number": number, "confirm": True}
        if name:
            body["name"] = name
        if assistant:
            body["assistant"] = assistant
        result = self.client.call("voice.number.import", body, idempotency_key=new_idempotency_key())
        saved = dict(result) if isinstance(result, dict) else {"number": number}
        saved["number"] = number
        self.store.save_caller(saved)
        return saved

    def disconnect_caller_number(self, number: str, *, confirm: bool = False) -> dict:
        check_phone(number)
        if confirm is not True:
            raise ValueError("Confirm before this caller ID is disconnected.")
        result = self.client.call(
            "voice.number.disconnect",
            {"number": number, "confirm": True},
            idempotency_key=new_idempotency_key(),
        )
        self.store.delete_caller(number)
        return result if isinstance(result, dict) else {"disconnected": True}

    def list_caller_numbers(self) -> list:
        return self.store.list_callers()

    def create_included_number(self, area_code: str | None = None, name: str | None = None) -> dict:
        body: dict = {"numberType": "included"}
        if area_code:
            body["areaCode"] = area_code
        if name:
            body["name"] = name
        return self.client.call("voice.number.create", body, idempotency_key=new_idempotency_key())

    def opt_out(self, address: str) -> dict:
        check_phone(address)
        result = self.client.call(
            "messaging.opt-out.add",
            {"address": address, "channel": "voice"},
            idempotency_key=new_idempotency_key(),
        )
        self.store.add_opt_out(address)
        return result

    def remove_opt_out(self, address: str) -> dict:
        check_phone(address)
        result = self.client.call(
            "messaging.opt-out.remove",
            {"address": address, "channel": "voice", "confirm": True},
            idempotency_key=new_idempotency_key(),
        )
        self.store.remove_opt_out(address)
        return result

    def list_opt_outs(self) -> list:
        return self.store.list_opt_outs()

    def _blocked(self, address: str) -> bool:
        if self.store.opted_out(address):
            return True
        blocked = self.client.call("messaging.opt-out.check", {"address": address, "channel": "voice"})
        return isinstance(blocked, dict) and blocked.get("blocked") is True

    def estimate_agent_call(
        self,
        *,
        to: str,
        from_number: str,
        assistant: str | None = None,
        squad: str | None = None,
        max_duration_seconds: int = 120,
        variables: dict | None = None,
        schedule_at: str | None = None,
    ) -> dict:
        self._check_call(to, from_number, assistant, squad, max_duration_seconds)
        if self._blocked(to):
            raise OptOut()
        sentence = self._call_sentence(to, from_number, assistant or squad or "", max_duration_seconds, schedule_at)
        capability_body = self._capability_call_body(
            to, from_number, assistant, squad, max_duration_seconds, variables, schedule_at
        )
        if schedule_at:
            return {
                "fallback": True,
                "capability": "voice.call.start-agent",
                "body": capability_body,
                "sentence": sentence + " A scheduled call uses the direct action.",
            }
        outcome_body = {
            "input": {
                "to": to,
                "from": from_number,
                "variables": variables or {},
                "maxDurationSeconds": max_duration_seconds,
                "recording": False,
                **({"assistant": assistant} if assistant else {"squad": squad}),
            },
            "options": {},
            "basis": _basis("legitimate_interest"),
        }
        try:
            estimate = self.client.outcomes_estimate(
                "voice.agent.call", outcome_body, idempotency_key=new_idempotency_key()
            )
        except OutcomeUnavailable:
            return {
                "fallback": True,
                "capability": "voice.call.start-agent",
                "body": capability_body,
                "sentence": sentence,
            }
        hold = estimate.get("price_max_micro")
        if isinstance(hold, int):
            sentence += f" At most {dollars(hold)} is set aside."
        return {
            "fallback": False,
            "plan": estimate.get("plan"),
            "body": outcome_body,
            "capabilityBody": capability_body,
            "priceMaxMicro": hold,
            **({"priceTypicalMicro": estimate["price_typical_micro"]} if estimate.get("price_typical_micro") is not None else {}),
            "sentence": sentence,
        }

    def place_agent_call(
        self,
        *,
        to: str,
        from_number: str,
        assistant: str | None = None,
        squad: str | None = None,
        max_duration_seconds: int = 120,
        variables: dict | None = None,
        schedule_at: str | None = None,
        plan: str | None = None,
        outcome_body: dict | None = None,
    ) -> dict:
        self._check_call(to, from_number, assistant, squad, max_duration_seconds)
        if self._blocked(to):
            raise OptOut()
        placed = None
        try:
            if plan and outcome_body and not schedule_at:
                try:
                    report = self.client.outcomes_run(
                        "voice.agent.call",
                        outcome_body,
                        plan=plan,
                        idempotency_key=new_idempotency_key(),
                    )
                    placed = dict(report.get("result") or {})
                    reason = not_placed_reason(placed)
                    if reason:
                        raise PramaanError("failed", reason)
                    summary_rows = placed.get("summary") or []
                    if summary_rows and isinstance(summary_rows[0], dict):
                        placed["summary"] = summary_rows[0].get("text") or ""
                    placed["outcome"] = "voice.agent.call"
                except OutcomeUnavailable:
                    placed = None
            if placed is None:
                placed = dict(
                    self.client.call(
                        "voice.call.start-agent",
                        self._capability_call_body(
                            to, from_number, assistant, squad, max_duration_seconds, variables, schedule_at
                        ),
                        idempotency_key=new_idempotency_key(),
                    )
                )
                summary, incomplete = self._summarise(str(placed.get("transcript") or ""))
                placed["summary"] = summary
                placed["summaryIncomplete"] = incomplete
                placed["outcome"] = "voice.call.start-agent"
        except PramaanError as exc:
            source = placed if isinstance(placed, dict) else None
            open_status = source.get("status") if source else None
            retryable = bool(getattr(exc, "retryable", True))
            if open_status in {"in_progress", "dialing", "queued", "ringing"}:
                self.store.save_call(
                    {
                        "ref": source.get("ref") or ("failed_" + uuid.uuid4().hex[:8]),
                        "to": to,
                        "from": from_number,
                        "assistant": assistant,
                        "squad": squad,
                        "variables": variables or {},
                        "status": open_status,
                        "failureReason": str(exc),
                        "retryable": retryable,
                        "endReason": None,
                        "durationSeconds": source.get("durationSeconds"),
                        "transcript": source.get("transcript") or "",
                        "summary": "",
                        "outcome": source.get("outcome"),
                        "scheduleAt": schedule_at,
                        "timeline": [{"status": open_status, "sentence": str(exc)}],
                    }
                )
            else:
                self._save_failed_call(
                    to=to,
                    from_number=from_number,
                    assistant=assistant,
                    squad=squad,
                    variables=variables,
                    schedule_at=schedule_at,
                    reason=str(exc),
                    placed=source,
                    retryable=retryable,
                )
            raise
        status = placed.get("status")
        if not status:
            self._save_failed_call(
                to=to,
                from_number=from_number,
                assistant=assistant,
                squad=squad,
                variables=variables,
                schedule_at=schedule_at,
                reason="The call did not return a status.",
                placed=placed,
            )
            raise PramaanError("failed", "The call did not return a status.")
        record = {
            "ref": placed.get("ref"),
            "to": to,
            "from": from_number,
            "assistant": assistant,
            "squad": squad,
            "variables": variables or {},
            "status": status,
            "endReason": placed.get("endReason") or placed.get("endedReason"),
            "durationSeconds": placed.get("durationSeconds"),
            "transcript": placed.get("transcript") or "",
            "summary": placed.get("summary") or "",
            "summaryIncomplete": bool(placed.get("summaryIncomplete")),
            "outcome": placed.get("outcome"),
            "scheduleAt": schedule_at,
            "timeline": [{"status": status, "sentence": "The call was placed."}],
        }
        if record["ref"]:
            self.store.save_call(record)
        return record

    def _save_failed_call(
        self,
        *,
        to: str,
        from_number: str,
        assistant: str | None,
        squad: str | None,
        variables: dict | None,
        schedule_at: str | None,
        reason: str,
        placed: dict | None,
        retryable: bool = True,
    ) -> dict:
        """A failed model or capability call stays failed, with the reason, and can be tried again."""
        source = placed or {}
        record = {
            "ref": source.get("ref") or ("failed_" + uuid.uuid4().hex[:8]),
            "to": to,
            "from": from_number,
            "assistant": assistant,
            "squad": squad,
            "variables": variables or {},
            "status": "failed",
            "failureReason": reason,
            "retryable": retryable,
            "endReason": None,
            "durationSeconds": source.get("durationSeconds"),
            "transcript": source.get("transcript") or "",
            "summary": "",
            "outcome": source.get("outcome"),
            "scheduleAt": schedule_at,
            "timeline": [{"status": "failed", "sentence": reason}],
        }
        self.store.save_call(record)
        return record

    def call_detail(self, ref: str) -> dict | None:
        row = self.store.get_call(ref)
        if row is None:
            return None
        shown = dict(row)
        shown["timeline"] = list(row.get("timeline") or [])
        if row.get("status") == "failed":
            shown["details"] = {}
            shown["legs"] = []
            return shown
        details = self.client.call("voice.call.get-details", {"ref": ref})
        legs = self.client.call("voice.call.legs.list", {"ref": ref})
        shown["details"] = details if isinstance(details, dict) else {}
        shown["legs"] = legs.get("results") if isinstance(legs, dict) else []
        shown["timeline"] = list(row.get("timeline") or [])
        if isinstance(details, dict) and details.get("status"):
            shown["timeline"] = shown["timeline"] + [
                {"status": details["status"], "sentence": "The latest status was read."}
            ]
        return shown

    def recording(self, ref: str) -> bytes:
        if self.store.get_call(ref) is None:
            raise KeyError("That call is not saved.")
        self.client.call("voice.call.recording.get", {"ref": ref, "format": "wav", "channels": "single"})
        return silent_wav()

    def end_call(self, ref: str) -> dict:
        current = self.store.get_call(ref)
        if current is None:
            raise KeyError("That call is not saved.")
        if current.get("status") == "failed":
            raise ValueError("This call failed. You can place it again.")
        if current.get("status") == "cancelled":
            raise ValueError("This call has already ended.")
        result = self.client.call("voice.call.end", {"ref": ref}, idempotency_key=new_idempotency_key())
        current = self.store.get_call(ref) or {}
        current["status"] = "cancelled"
        current["endReason"] = "cancelled"
        timeline = list(current.get("timeline") or [])
        timeline.append({"status": "cancelled", "sentence": "The call was ended."})
        current["timeline"] = timeline
        self.store.save_call(current)
        return result

    def attach_inbound(
        self,
        number: str,
        *,
        assistant: str | None = None,
        squad: str | None = None,
        detach: bool = False,
    ) -> dict:
        """Bind an agent to a US inbound number. Not yet live: inbound billing is not enabled."""
        check_phone(number)
        chosen = (assistant is not None) + (squad is not None) + (detach is True)
        if chosen != 1:
            raise ValueError("Attach one assistant or one squad, or detach.")
        patch: dict = {}
        if assistant:
            patch["assistant"] = assistant
        elif squad:
            patch["squad"] = squad
        else:
            patch["detach"] = True
        return self.client.call(
            "voice.number.update",
            {"number": number, "patch": patch},
            idempotency_key=new_idempotency_key(),
        )

    def create_knowledge(self, name: str, text: str) -> dict:
        check_name(name)
        if not isinstance(text, str) or not text or len(text) > 100_000:
            raise ValueError("Write document text of at most 100,000 characters.")
        result = self.client.call(
            "voice.knowledge-base.create",
            {"name": name, "text": text},
            idempotency_key=new_idempotency_key(),
        )
        saved = {
            "name": result.get("name") or name,
            "status": result.get("status") or "ready",
            "sourceCount": result.get("sourceCount") or 1,
            "ref": result.get("ref"),
        }
        self.store.save_knowledge(saved)
        return saved

    def list_knowledge(self) -> list:
        saved = self.store.list_knowledge()
        if saved:
            return saved
        found = self.client.call("voice.knowledge-bases.list", {"limit": 25})
        return list(found.get("results") or []) if isinstance(found, dict) else []

    def get_knowledge(self, ref: str) -> dict:
        current = self.store.get_knowledge(ref)
        if current is None:
            raise KeyError("That document base is not saved.")
        fresh = self.client.call("voice.knowledge-base.get", {"ref": ref})
        if isinstance(fresh, dict):
            for key in ("name", "status", "sourceCount"):
                if key in fresh:
                    current[key] = fresh[key]
            self.store.save_knowledge(current)
        return {
            "name": current.get("name"),
            "status": current.get("status"),
            "sourceCount": current.get("sourceCount"),
            "ref": current.get("ref"),
        }

    def delete_knowledge(self, ref: str, *, confirm: bool = False) -> dict:
        if confirm is not True:
            raise ValueError("Confirm before this document base is deleted.")
        if not isinstance(ref, str) or not ref or "/" in ref or len(ref) > 1200:
            raise ValueError("Send the document base to delete.")
        if self.store.get_knowledge(ref) is None:
            raise KeyError("That document base is not saved.")
        result = self.client.call(
            "voice.knowledge-base.delete",
            {"ref": ref, "confirm": True},
            idempotency_key=new_idempotency_key(),
        )
        if not isinstance(result, dict) or result.get("deleted") is not True:
            raise PramaanError("failed", "The request failed.")
        self.store.delete_knowledge(ref)
        for agent in self.store.list_agents():
            if agent.get("knowledgeBaseRef") == ref:
                agent.pop("knowledgeBaseRef", None)
                agent.pop("knowledgeName", None)
                self.store.save_agent(agent)
        return {"deleted": True}

    def start_agent_campaign(
        self,
        name: str,
        from_number: str,
        assistant: str,
        rows: list[dict],
        max_duration_seconds: int = 120,
    ) -> dict:
        """Place one assistant call per person. A spoken message with no assistant stays on start_campaign."""
        check_phone(from_number)
        check_name(assistant)
        check_duration(int(max_duration_seconds))
        if not name or len(name) > 100:
            raise ValueError("Use a campaign name of at most 100 characters.")
        if not rows or len(rows) > 100:
            raise ValueError("A campaign includes 1 to 100 people.")
        seen: set[str] = set()
        prepared: list[dict] = []
        blocked: dict[str, dict] = {}
        order: list[str] = []
        for row in rows:
            check_phone(row["to"])
            row_id = str(row.get("id") or "")
            if not row_id or len(row_id) > 128:
                raise ValueError("Each row needs an id of at most 128 characters.")
            if row_id in seen:
                raise ValueError("Row ids must be unique.")
            seen.add(row_id)
            order.append(row_id)
            if self._blocked(row["to"]):
                blocked[row_id] = {
                    "id": row_id,
                    "to": row["to"],
                    "found": False,
                    "costMicro": 0,
                    "error": {
                        "code": "recipient_opted_out",
                        "message": "This recipient has opted out of these messages.",
                    },
                }
            else:
                prepared.append(
                    {
                        "id": row_id,
                        "to": row["to"],
                        "from": from_number,
                        "assistant": assistant,
                        "maxDurationSeconds": int(max_duration_seconds),
                    }
                )
        answered: dict[str, dict] = {}
        if prepared:
            try:
                raw = self.client.call(
                    "voice.call.start-agent-bulk",
                    {"rows": prepared},
                    idempotency_key=new_idempotency_key(),
                )
            except PramaanError as exc:
                self._save_failed_campaign(name, from_number, str(exc), assistant=assistant, path="agent")
                raise
            by_id = {item["id"]: item for item in (raw.get("results") or []) if isinstance(item, dict)}
            phones = {item["id"]: item["to"] for item in prepared}
            for row_id, phone in phones.items():
                answer = dict(
                    by_id.get(row_id)
                    or {
                        "id": row_id,
                        "found": False,
                        "costMicro": 0,
                        "error": {
                            "code": "upstream_unavailable",
                            "message": "This row could not be answered.",
                        },
                    }
                )
                answer["to"] = phone
                answered[row_id] = answer
        ordered = [blocked.get(row_id) or answered[row_id] for row_id in order]
        record = {
            "ref": "bulk_" + uuid.uuid4().hex[:8],
            "name": name,
            "from": from_number,
            "assistant": assistant,
            "path": "agent",
            "state": "completed",
            "rows": ordered,
            "costMicro": sum(int(item.get("costMicro") or 0) for item in ordered),
        }
        self.store.save_campaign(record)
        return record

    def start_campaign(self, name: str, from_number: str, message: str, rows: list[dict]) -> dict:
        check_phone(from_number)
        if not name or len(name) > 100:
            raise ValueError("Use a campaign name of at most 100 characters.")
        if not rows or len(rows) > 100:
            raise ValueError("A campaign includes 1 to 100 people.")
        if len(message) > 1000 or not message:
            raise ValueError("Write a message of at most 1,000 characters.")
        kept = []
        for row in rows:
            check_phone(row["to"])
            if self._blocked(row["to"]):
                raise OptOut()
            kept.append({"id": str(row.get("id") or len(kept) + 1), "to": row["to"], "state": "queued"})
        try:
            result = self.client.call(
                "voice.campaign.create",
                {
                    "name": name,
                    "from": from_number,
                    "message": message,
                    "rows": [{"id": row["id"], "to": row["to"]} for row in kept],
                    "maxDurationSeconds": 120,
                    "retries": 0,
                },
                idempotency_key=new_idempotency_key(),
            )
        except PramaanError as exc:
            self._save_failed_campaign(name, from_number, str(exc), message=message, rows=kept)
            raise
        ref = result.get("ref") if isinstance(result, dict) else None
        record = {
            "ref": ref,
            "name": name,
            "from": from_number,
            "message": message,
            "state": (result or {}).get("state") or "running",
            "rows": kept,
        }
        if ref:
            self.store.save_campaign(record)
        return record

    def rows_from_csv(self, text: str) -> list[dict]:
        return parse_recipients(text)

    def _save_failed_campaign(self, name: str, from_number: str, reason: str, **extra: Any) -> dict:
        record = {
            "ref": "failed_" + uuid.uuid4().hex[:8],
            "name": name,
            "from": from_number,
            "state": "failed",
            "failureReason": reason,
            "retryable": True,
            "rows": [],
        }
        record.update(extra)
        self.store.save_campaign(record)
        return record

    def _campaign_action(self, ref: str, capability_id: str, state: str) -> dict:
        current = self.store.get_campaign(ref)
        if current is None:
            raise KeyError("That campaign is not saved.")
        if current.get("path") == "agent":
            raise ValueError("This campaign is a set of agent calls. It cannot be paused.")
        result = self.client.call(capability_id, {"ref": ref}, idempotency_key=new_idempotency_key())
        current["state"] = state
        if state == "finished":
            for row in current.get("rows") or []:
                row["state"] = "finished"
        self.store.save_campaign(current)
        return result if isinstance(result, dict) else {"state": state}

    def pause_campaign(self, ref: str) -> dict:
        return self._campaign_action(ref, "voice.campaign.pause", "paused")

    def resume_campaign(self, ref: str) -> dict:
        return self._campaign_action(ref, "voice.campaign.resume", "running")

    def finish_campaign(self, ref: str) -> dict:
        return self._campaign_action(ref, "voice.campaign.finish", "finished")

    def settings_view(self) -> dict:
        saved = self.store.settings()
        return {
            "webhookEndpoint": saved.get("webhookEndpoint") or "",
            "secretSet": bool(os.environ.get("PRAMAAN_WEBHOOK_SECRET")),
            "keySet": bool(os.environ.get("PRAMAAN_API_KEY")),
            "needsKey": os.environ.get("PRAMAAN_FAKE") != "1" and not os.environ.get("PRAMAAN_API_KEY"),
            "limits": LIMITS,
            "liveChecks": "Use one project per organisation until the lab defect is fixed.",
        }

    def save_settings(self, webhook_endpoint: str) -> dict:
        import re

        if webhook_endpoint and not re.fullmatch(r"we_[a-f0-9]{32}", webhook_endpoint):
            raise ValueError("Use a webhook endpoint id: we_ and 32 hex digits.")
        self.store.save_settings(webhook_endpoint)
        return self.settings_view()

    def open_browser_session(self, agent: str, from_number: str, origin: str) -> dict:
        check_phone(from_number)
        return self.client.call(
            "voice.browser-session.create",
            {"agent": agent, "from": from_number, "origin": origin, "lifetimeSeconds": 900},
            idempotency_key=new_idempotency_key(),
        )

    def judge(self, transcript: str, question: str) -> dict:
        from ai_caller.evaluate import judge_transcript

        return judge_transcript(self.client, transcript, question)

    def _check_call(self, to, from_number, assistant, squad, max_duration_seconds) -> None:
        check_phone(to)
        check_phone(from_number)
        check_duration(max_duration_seconds)
        if (assistant is None) == (squad is None):
            raise ValueError("Name either an assistant or a squad.")

    def _capability_call_body(self, to, from_number, assistant, squad, max_duration_seconds, variables, schedule_at):
        body: dict = {
            "to": to,
            "from": from_number,
            "variables": variables or {},
            "maxDurationSeconds": max_duration_seconds,
            "tier": "standard",
            "recording": False,
        }
        if assistant:
            body["assistant"] = assistant
        else:
            body["squad"] = squad
        if schedule_at:
            body["scheduleAt"] = schedule_at
        return body

    def retry_summary(self, ref: str) -> dict:
        """Run the summary again. Does not place another call."""
        row = self.store.get_call(ref)
        if row is None:
            raise KeyError("That call is not saved.")
        if not row.get("summaryIncomplete"):
            return {"summary": row.get("summary") or "", "summaryIncomplete": False}
        text, incomplete = self._summarise(str(row.get("transcript") or ""))
        row["summary"] = text
        row["summaryIncomplete"] = incomplete
        self.store.save_call(row)
        return {"summary": text, "summaryIncomplete": incomplete}

    def _chat_text(self, messages: list) -> Any:
        payload = self.client.call(
            "llm.chat",
            {"messages": messages, "maxTokens": REPLY_LIMIT},
        )
        return read_model_reply(payload, structured=False)

    def _summarise(self, transcript: str) -> tuple[str, bool]:
        """One sentence. A cut-off reply is continued once, then shown as incomplete."""
        messages = [
            {
                "role": "user",
                "content": "Summarise the call in one sentence.\n\n" + transcript[:6000],
            }
        ]
        first = self._chat_text(messages)
        if not first.cut_off:
            return first.text, False
        if not first.text:
            return "This summary was cut off. Try again.", True
        second = self._chat_text(
            messages
            + [
                {"role": "assistant", "content": first.text},
                {
                    "role": "user",
                    "content": "Continue from where the sentence stopped. Do not start over.",
                },
            ]
        )
        if second.cut_off:
            return "This summary was cut off. Try again.", True
        return (first.text + second.text).strip(), False

    def _call_sentence(self, to: str, from_number: str, who: str, seconds: int, schedule_at: str | None) -> str:
        when = f" at {schedule_at}" if schedule_at else ""
        return (
            f"This places a call to {to} from {from_number} using {who}{when}. "
            f"It can last {seconds} seconds. Nothing is dialed until you confirm."
        )

    def _buy_sentence(self, number: str, country: str, rate: str | None) -> str:
        price = (
            f" Reported monthly rate: {rate} USD. That is not a guarantee. The charge is set when buying."
            if rate
            else " No monthly rate was reported. The charge is set when buying."
        )
        return (
            f"This rents {number} in {country}.{price} "
            "Nothing is bought until you confirm."
        )
