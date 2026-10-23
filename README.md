[![CI](https://github.com/Pramaan-OSS/ai-caller/actions/workflows/ci.yml/badge.svg)](https://github.com/Pramaan-OSS/ai-caller/actions/workflows/ci.yml)

# AI Caller

Phone agents for your own business. You write the prompt and the tools. Pramaan places the call, speaks, and hears. This app does not run a media server.

## Quick start

Python 3.11 or newer is the only install.

```bash
cp .env.example .env
```

Paste `PRAMAAN_API_KEY` into `.env`, then:

```bash
./start
```

Open http://localhost:8787

The database is provided through Pramaan in test mode. Until it is available, records stay in `data/ai-caller.db` on this machine. Managed sign-in is prepared the same way. Both setup calls reuse an idempotency key saved on this machine. On this machine the console stays open until it is available, and a local address is not registered. If a setup call is refused, the console prints that reason. Open the localhost address above so the callback is accepted. Buying a number, placing a call, and starting a campaign spend money and ring real phones. Do that only when you mean to. The tests never do.

```bash
python -m unittest discover -s tests
```

Examples, against the fake client:

```bash
PRAMAAN_FAKE=1 python examples/receptionist.py
PRAMAAN_FAKE=1 python examples/handoff.py
PRAMAAN_FAKE=1 python examples/campaign.py
```

## What a call does

The console saves agents, tools, document bases, numbers, calls, campaigns, and opt-outs on this machine. A campaign with an agent places one call per person. A document base can be attached to an agent. Both are on the catalogue and are not live-verified. The lab API is not deployed yet. A call or a number rental shows an estimate and waits for confirmation. When the outcome is available, a call uses `voice.agent.call` and a number setup uses `phone.number.setup`. Otherwise the app uses `voice.call.start-agent` or `phone.number.purchase`. A long run can be cancelled. If twenty-five long runs are already open, try later. A run still open after 35 minutes ends as failed. A step whose result cannot be recovered is shown as check before retrying, and this app does not retry it. A number on the do-not-call list is refused first. The API key stays on the server.

A call lasts 10 to 600 seconds. The default is 120. The console shows that limit. A speaking voice is one of default, female-1, female-2, female-3, male-1, male-2, or male-3. Leave it unset for the standard voice. A saved voice that is no longer offered still runs, and the console says to pick a listed voice before changing it. A second person cannot be bridged into the call. Live speech from this app, and live noise reduction, are not available. A tool is hosted code, or a function tool that names a registered endpoint id. Signed callbacks arrive at `/tools/callback`. That field is on the catalogue and is not live-verified. Attaching an agent to an inbound number is not yet live. A number you already rent can be used as the caller ID for agent calls. While that action is switched off, the console says this is not available yet. Disconnecting it leaves the rental in place.

## Built on

[LiveKit Agents](https://github.com/livekit/agents), Apache-2.0. The original licence text is in `LICENSE-ORIGINAL` and `NOTICE`. This template is AGPL-3.0.
