"""Headless check of the console. No visible browser."""

from __future__ import annotations

import json
import os
import threading

from playwright.sync_api import sync_playwright

from ai_caller.pramaan import FakePramaan
from ai_caller.server import make_server
from ai_caller.sign_in import Gate


def _word(*parts: str) -> str:
    return "".join(parts)


FORBIDDEN = (
    _word("live", "kit"),
    _word("open", "ai"),
    _word("twi", "lio"),
    _word("deep", "gram"),
    _word("anth", "ropic"),
    _word("carte", "sia"),
    _word("eleven", "labs"),
    _word("kr", "isp"),
)


def main() -> None:
    os.environ["PRAMAAN_FAKE"] = "1"
    os.environ["PRAMAAN_CALL_OPEN"] = "1"
    os.environ.pop("PRAMAAN_API_KEY", None)
    server = make_server(port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1142, "height": 732})
            page.goto(base)
            fonts = page.evaluate(
                """async () => {
                  await document.fonts.load('400 28px Fraunces');
                  await document.fonts.load('500 14px Geist');
                  await document.fonts.load('400 12px \"JetBrains Mono\"');
                  await document.fonts.ready;
                  return {
                    fraunces: document.fonts.check('400 28px Fraunces'),
                    geist: document.fonts.check('500 14px Geist'),
                    mono: document.fonts.check('400 12px \"JetBrains Mono\"'),
                    tab: document.querySelectorAll('.tab[aria-current="page"]').length
                  };
                }"""
            )
            if not fonts["fraunces"] or not fonts["geist"] or not fonts["mono"]:
                raise SystemExit(f"Design fonts did not load: {fonts}")
            if fonts["tab"] != 1:
                raise SystemExit("Expected one current tab.")

            page.get_by_role("button", name="Save agent").click()
            page.get_by_role("heading", name="reception").wait_for()
            page.locator(".right").get_by_text("Voice standard").wait_for()
            page.get_by_role("button", name="Edit").click()
            page.locator("#prompt").fill("You answer the phone. State the hours.")
            page.locator("#greeting").fill("Hello from the desk.")
            page.get_by_role("button", name="Save agent").click()
            page.locator(".right").get_by_text("State the hours.").wait_for()
            page.locator(".right").get_by_text("Hello from the desk.").wait_for()
            page.locator("#voice").select_option("female-1")
            page.get_by_role("button", name="Save agent").click()
            page.locator(".right").get_by_text("Voice Female 1").wait_for()
            _thread(page, 1)

            def _retired(route):
                if route.request.method != "GET" or not route.request.url.rstrip("/").endswith("/api/agents"):
                    route.fallback()
                    return
                response = route.fetch()
                data = response.json()
                for item in data.get("items") or []:
                    item["voice"] = "retired"
                route.fulfill(
                    status=response.status,
                    content_type="application/json",
                    body=json.dumps(data),
                )

            page.route("**/api/agents", _retired)
            page.get_by_role("button", name="Agents").click()
            page.get_by_text(
                "This voice is no longer offered. The agent still runs. Pick a listed voice to change it."
            ).wait_for()
            page.unroute("**/api/agents")
            _thread(page, 1)

            page.get_by_role("button", name="Knowledge").click()
            page.get_by_role("button", name="Review this document").click()
            page.get_by_text("Nothing is indexed until you confirm.").wait_for()
            page.get_by_role("button", name="Index this document").click()
            page.locator(".right").get_by_role("heading", name="hours").wait_for()
            page.get_by_role("button", name="Open").click()
            page.get_by_text("Document text is not returned.").wait_for()
            _thread(page, 1)

            page.get_by_role("button", name="Agents").click()
            page.locator("#kb").select_option(label="hours")
            page.locator("#voice").select_option("female-1")
            page.get_by_role("button", name="Save agent").click()
            page.locator(".right").get_by_text("Knowledge hours").wait_for()
            page.locator(".right").get_by_text("Voice Female 1").wait_for()
            _thread(page, 1)

            page.get_by_role("button", name="Knowledge").click()
            seen_delete = {}

            def _accept_delete(dialog) -> None:
                seen_delete["message"] = dialog.message
                dialog.accept()

            page.once("dialog", _accept_delete)
            page.get_by_role("button", name="Delete").click()
            page.locator(".right").get_by_text("No documents yet.").wait_for()
            if "storage" not in seen_delete.get("message", "").lower():
                raise SystemExit("Knowledge delete did not ask for confirmation.")
            _thread(page, 1)

            page.get_by_role("button", name="Tools").click()
            page.get_by_role("button", name="Save tool").click()
            page.get_by_role("heading", name="lookup").wait_for()
            page.get_by_role("button", name="Delete").click()
            page.locator(".right").get_by_text("No tools yet.").wait_for()
            _thread(page, 1)

            page.get_by_role("button", name="Calls").click()
            page.locator("#caller-pick").wait_for()
            page.get_by_text("A call lasts 10 to 600 seconds. The default is 120.").wait_for()
            page.get_by_role("button", name="Review this call").click()
            page.get_by_text("Nothing is dialed until you confirm.").wait_for()
            page.get_by_role("button", name="Place the call").click()
            page.locator(".right").get_by_text("practice transcript").wait_for()
            listed = page.locator(".right").get_by_role("button", name="End call")
            listed.wait_for()
            if listed.is_disabled():
                raise SystemExit("End call was disabled while the call was still open.")
            page.get_by_role("button", name="Open").click()
            page.get_by_text("The caller was greeted.").wait_for()
            page.locator("audio").wait_for()
            ref = page.locator(".right h2").inner_text()
            seen: dict[str, str] = {}

            def _accept(dialog) -> None:
                seen["message"] = dialog.message
                dialog.accept()

            page.once("dialog", _accept)
            page.get_by_role("button", name="End call").click()
            if "disconnected" not in seen.get("message", ""):
                raise SystemExit(f"End confirmation was {seen.get('message')!r}")
            page.get_by_text("This call has already ended.").wait_for()
            if not page.get_by_role("button", name="End call").is_disabled():
                raise SystemExit("End call stayed enabled after the call ended.")
            _thread(page, 1)

            page.get_by_role("button", name="Calls").click()
            page.locator("#call-status").wait_for()
            page.locator("#call-status").select_option("in_progress")
            page.locator(".right").get_by_text("No calls yet.").wait_for()
            page.locator("#call-status").select_option("cancelled")
            page.locator(".right").get_by_role("heading", name=ref).wait_for()
            _thread(page, 1)

            page.get_by_role("button", name="Campaigns").click()
            page.locator("#campaign-caller").wait_for()
            page.get_by_role("button", name="Review this campaign").click()
            page.get_by_text("A row that never connects costs nothing.").wait_for()
            page.get_by_text("Nothing is dialed until you confirm.").wait_for()
            page.get_by_role("button", name="Start the campaign").click()
            page.locator(".right").get_by_text("Delivery notice").wait_for()
            page.locator(".right").get_by_text("a · in_progress").wait_for()
            _thread(page, 1)

            page.get_by_role("button", name="Opt-outs").click()
            page.locator("#address").fill("+14155550109")
            page.get_by_role("button", name="Add to the list").click()
            page.locator(".right").get_by_text("+14155550109").wait_for()
            page.get_by_role("button", name="Calls").click()
            page.locator("#to").fill("+14155550109")
            page.get_by_role("button", name="Review this call").click()
            page.get_by_text("do-not-call").wait_for()
            if page.locator(".right").get_by_text("+14155550109").count():
                raise SystemExit("An opted-out number was placed.")
            _thread(page, 1)

            page.get_by_role("button", name="Numbers").click()
            page.locator("#caller-number").fill("+14155550198")
            page.get_by_role("button", name="Use this number as caller ID for agent calls").click()
            page.get_by_text("Nothing changes until you confirm.").wait_for()
            page.get_by_role("button", name="Use this caller ID").click()
            page.locator("#caller-err").get_by_text("This is not available yet.").wait_for()
            page.get_by_role("button", name="Disconnect this caller ID").click()
            page.get_by_text("Agent calls will stop using this number.").wait_for()
            page.get_by_role("button", name="Confirm disconnect").click()
            page.locator("#caller-err").get_by_text("This is not available yet.").wait_for()
            _thread(page, 1)
            page.get_by_role("button", name="Use an included number").click()
            page.locator(".right").get_by_text("+14155550199").wait_for()
            page.get_by_text("not yet live").wait_for()
            page.get_by_role("button", name="Search numbers").click()
            page.get_by_text("1.15 USD").wait_for()
            page.get_by_role("button", name="Buy this number").click()
            page.locator(".right").get_by_text("+14155550100").wait_for()
            _thread(page, 1)

            page.get_by_role("button", name="Settings").click()
            page.get_by_text("one project per organisation").wait_for()
            _thread(page, 1)

            page.get_by_role("button", name="Dark").click()
            color = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
            if color != "rgb(15, 15, 16)":
                raise SystemExit(f"Dark background was {color}")
            _wide(page, "1142")
            page.set_viewport_size({"width": 400, "height": 800})
            _wide(page, "400")
            page.set_viewport_size({"width": 1142, "height": 732})
            page.get_by_role("button", name="Light").click()
            light = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
            if light != "rgb(237, 237, 237)":
                raise SystemExit(f"Light background was {light}")

            text = page.locator("body").inner_text().lower()
            for word in FORBIDDEN:
                if word in text:
                    raise SystemExit(f"Page text contains {word}")
            browser.close()
        _off_pages()
        _cut_summary()
        _sign_in_gate()
    finally:
        os.environ.pop("PRAMAAN_CALL_OPEN", None)
        server.shutdown()
        server.server_close()
    print("browser check passed")


def _off_pages() -> None:
    """Voice and phone answer not available yet. The rest of the console still works."""
    os.environ["PRAMAAN_FAKE"] = "1"
    os.environ["PRAMAAN_LIVE_OFF"] = "1"
    os.environ.pop("PRAMAAN_API_KEY", None)
    server = make_server(port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    sentence = "This is not available yet."
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1142, "height": 732})
            problems: list[str] = []
            page.on("pageerror", lambda exc: problems.append(str(exc)))
            page.goto(base)
            page.get_by_role("heading", name="Save an agent").wait_for()
            page.locator("#agent-err").get_by_text(sentence).wait_for()
            page.get_by_role("button", name="Save agent").click()
            page.locator("#agent-err").get_by_text(sentence).wait_for()
            page.locator(".right").get_by_text("No agents yet.").wait_for()
            if page.get_by_role("heading", name="reception").count():
                raise SystemExit("A switched-off agent was saved.")

            page.get_by_role("button", name="Tools").click()
            page.get_by_role("heading", name="Save a tool").wait_for()
            page.get_by_role("button", name="Save tool").click()
            page.locator("#tool-err").get_by_text(sentence).wait_for()
            page.locator(".right").get_by_text("No tools yet.").wait_for()

            page.get_by_role("button", name="Knowledge").click()
            page.get_by_role("heading", name="Index a document").wait_for()
            page.locator("#kb-err").get_by_text(sentence).wait_for()
            page.locator(".right").get_by_text("No documents yet.").wait_for()

            page.get_by_role("button", name="Numbers").click()
            page.get_by_role("heading", name="Find a number").wait_for()
            page.locator("#number-err").get_by_text(sentence).wait_for()
            page.get_by_role("button", name="Search numbers").click()
            page.locator("#number-err").get_by_text(sentence).wait_for()
            page.get_by_text("Inbound attach is not yet live.").wait_for()

            page.get_by_role("button", name="Calls", exact=True).click()
            page.get_by_role("heading", name="Place a call").wait_for()
            page.get_by_role("button", name="Review this call").click()
            page.locator("#call-err").get_by_text(sentence).wait_for()
            if page.get_by_role("button", name="Place the call").count():
                raise SystemExit("A switched-off call offered a confirm step.")
            page.locator(".right").get_by_text("No calls yet.").wait_for()

            page.get_by_role("button", name="Campaigns").click()
            page.get_by_role("heading", name="Start a campaign").wait_for()
            page.locator("#camp-err").get_by_text(sentence).wait_for()
            page.locator(".right").get_by_text("No campaigns yet.").wait_for()

            page.get_by_role("button", name="Opt-outs").click()
            page.get_by_role("heading", name="Do not call").wait_for()
            page.get_by_role("button", name="Add to the list").click()
            page.locator(".right").get_by_text("+14155550101").wait_for()

            page.get_by_role("button", name="Settings").click()
            page.get_by_role("heading", name="Settings").wait_for()
            page.get_by_role("heading", name="Limits").wait_for()
            page.get_by_text("10 to 600").wait_for()

            text = page.locator("body").inner_text()
            if "Traceback" in text:
                raise SystemExit("The page showed a stack trace.")
            lowered = text.lower()
            for word in FORBIDDEN:
                if word in lowered:
                    raise SystemExit(f"Page text contains {word}")
            if problems:
                raise SystemExit(f"Page error: {problems}")
            browser.close()
    finally:
        os.environ.pop("PRAMAAN_LIVE_OFF", None)
        server.shutdown()
        server.server_close()


def _cut_summary() -> None:
    """A summary that stops at the limit stays on the finished call and can be tried again."""
    os.environ["PRAMAAN_FAKE"] = "1"
    os.environ["PRAMAAN_SUMMARY_CUT"] = "1"
    os.environ.pop("PRAMAAN_API_KEY", None)
    os.environ.pop("PRAMAAN_CALL_OPEN", None)
    server = make_server(port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    sentence = "This summary was cut off. Try again."
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1142, "height": 732})
            problems: list[str] = []
            page.on("pageerror", lambda exc: problems.append(str(exc)))
            page.goto(base)
            page.get_by_role("button", name="Save agent").click()
            page.get_by_role("heading", name="reception").wait_for()
            page.get_by_role("button", name="Calls", exact=True).click()
            page.get_by_role("button", name="Review this call").click()
            page.get_by_role("button", name="Place the call").click()
            page.get_by_role("button", name="Open").click()
            page.get_by_text(sentence).wait_for()
            page.get_by_role("button", name="Try the summary again").click()
            page.get_by_text(sentence).wait_for()
            text = page.locator("body").inner_text()
            if "Traceback" in text:
                raise SystemExit("The page showed a stack trace.")
            lowered = text.lower()
            for word in FORBIDDEN:
                if word in lowered:
                    raise SystemExit(f"Page text contains {word}")
            if problems:
                raise SystemExit(f"Page error: {problems}")
            browser.close()
    finally:
        os.environ.pop("PRAMAAN_SUMMARY_CUT", None)
        server.shutdown()
        server.server_close()


def _sign_in_gate() -> None:
    """A ready gate sends the home page to sign-in. The script is not a live round trip."""
    os.environ["PRAMAAN_FAKE"] = "1"
    os.environ.pop("PRAMAAN_API_KEY", None)
    os.environ.pop("PRAMAAN_LIVE_OFF", None)
    os.environ.pop("PRAMAAN_SUMMARY_CUT", None)
    os.environ.pop("PRAMAAN_CALL_OPEN", None)
    gate = Gate("https://sign-in.example.test", "http://localhost:9")
    fake = FakePramaan()
    server = make_server(port=0, client=fake, gate=gate)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1142, "height": 732})
            problems: list[str] = []
            page.on("pageerror", lambda exc: problems.append(str(exc)))
            page.goto(base)
            page.get_by_role("heading", name="Sign in").wait_for()
            page.get_by_role("button", name="Continue").wait_for()
            page.get_by_label("Email").wait_for()
            page.get_by_label("Password").wait_for()
            _wide(page, "1142")
            blocked = page.evaluate("async () => (await fetch('/api/agents')).status")
            if blocked != 401:
                raise SystemExit(f"Expected the console to require sign-in, got {blocked}")
            page.set_viewport_size({"width": 390, "height": 844})
            page.get_by_role("button", name="Continue").wait_for()
            _wide(page, "390")
            text = page.locator("body").inner_text().lower()
            for word in ("google", "neon", "better auth"):
                if word in text:
                    raise SystemExit(f"Sign-in page contains {word}")
            secret = gate.open_session("user_1", "ada@example.com")
            page.context.add_cookies(
                [{"name": "ai_caller_session", "value": secret, "url": base}]
            )
            page.set_viewport_size({"width": 1142, "height": 732})
            page.goto(base)
            page.get_by_role("heading", name="Save an agent").wait_for()
            page.get_by_role("button", name="Settings").click()
            page.get_by_role("button", name="Delete this account").wait_for()
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Delete this account").click()
            page.get_by_role("heading", name="Sign in").wait_for()
            blocked = page.evaluate("async () => (await fetch('/api/agents')).status")
            if blocked != 401:
                raise SystemExit(f"Expected the deleted session to end, got {blocked}")
            removed = [item for item in fake.calls if item["id"] == "auth.user.delete"]
            if not removed or removed[-1]["body"] != {"userId": "user_1", "confirm": True}:
                raise SystemExit("Account delete did not send the confirmed user.")
            if problems:
                raise SystemExit(f"Page error: {problems}")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


def _wide(page, label: str) -> None:
    wide = page.evaluate(
        "() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
    )
    if wide:
        raise SystemExit(f"The page scrolls sideways at {label}px.")


def _thread(page, count: int) -> None:
    found = page.locator("[data-thread]").count()
    if found != count:
        raise SystemExit(f"Expected {count} thread control, found {found}")
    in_right = page.locator(".right [data-thread], .right .btn-thread").count()
    if in_right:
        raise SystemExit("The right pane holds the thread control.")


if __name__ == "__main__":
    main()
