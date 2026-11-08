"""Click through every button of the deployed app with a real browser.

Usage: python ui_check.py <base-url> [reply-base-url]

  python e2e/ui_check.py https://python-ai-backend-test-task.vercel.app
  python e2e/ui_check.py http://localhost:8000 http://api:8000   # docker compose

reply-base-url is how the worker reaches the mock; inside docker compose that
is http://api:8000, because "localhost" in the worker container is the worker.
"""

import json
import sys
import time
import urllib.request

from playwright.sync_api import Page, expect, sync_playwright

BASE = sys.argv[1].rstrip("/")
REPLY_BASE = (sys.argv[2] if len(sys.argv) > 2 else BASE).rstrip("/")
RUN = str(int(time.time()))[-5:]
results: list[tuple[str, bool, str]] = []
console_errors: list[str] = []


def step(name: str):
    def wrap(fn):
        try:
            fn()
            results.append((name, True, ""))
            print(f"PASS  {name}", flush=True)
        except Exception as exc:  # noqa: BLE001
            results.append((name, False, str(exc).splitlines()[0][:200]))
            print(f"FAIL  {name}: {str(exc).splitlines()[0][:200]}", flush=True)
        return fn
    return wrap


def post_webhook(ticket_id: str, message: str, reply_ticket: str | None = None) -> dict:
    body = json.dumps({
        "event_id": f"evt_ui_{ticket_id}_{time.time_ns()}", "ticket_id": ticket_id,
        "customer_email": "qa@example.com", "message": message,
        "reply_url": f"{REPLY_BASE}/mock/helpdesk/tickets/{reply_ticket or ticket_id}/replies",
    }).encode()
    req = urllib.request.Request(f"{BASE}/webhooks/helpdesk", data=body,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def card(page: Page, ticket_id: str):
    return page.locator(f"#ticket-{ticket_id}")


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and console_errors.append(m.text))
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    page.set_default_timeout(20_000)

    t_ok, t_rej, t_fail = f"T-OK{RUN}", f"T-RJ{RUN}", f"T-FL{RUN}"

    @step("root redirects to inbox; header nav links work")
    def _():
        page.goto(BASE + "/")
        expect(page).to_have_url(BASE + "/operator")
        for label, path, heading in [("Helpdesk mock", "/mock/helpdesk", "Helpdesk mock"),
                                     ("Help Center", "/kb", "Lumenboard Help Center"),
                                     ("Inbox", "/operator", "Support inbox")]:
            page.get_by_role("link", name=label, exact=True).click()
            expect(page).to_have_url(BASE + path)
            expect(page.get_by_role("heading", level=1)).to_have_text(heading)
        page.get_by_role("link", name="API", exact=True).click()
        expect(page).to_have_url(BASE + "/docs")
        expect(page.locator(".swagger-ui")).to_be_visible()

    @step("mock: example chip fills message, Send webhook opens ticket")
    def _():
        page.goto(BASE + "/mock/helpdesk")
        page.get_by_role("button", name="How do I invite a new member to my workspace?").click()
        expect(page.locator("#message")).to_have_value("How do I invite a new member to my workspace?")
        page.locator("input[name=ticket_id]").fill(t_ok)
        page.get_by_role("button", name="Send webhook").click()
        expect(page).to_have_url(BASE + "/operator")
        expect(card(page, t_ok)).to_be_visible()

    @step("inbox: card polls itself until the draft is ready (no reload)")
    def _():
        expect(card(page, t_ok).get_by_role("button", name="Approve & send")).to_be_visible(timeout=90_000)
        expect(card(page, t_ok)).to_contain_text("Needs review")

    @step("inbox: knowledge-base chip opens the Help Center article")
    def _():
        chip = card(page, t_ok).get_by_role("link", name="Inviting team members")
        with ctx.expect_page() as new:
            chip.click()
        article = new.value
        article.wait_for_load_state()
        expect(article.get_by_role("heading", level=1)).to_have_text("Inviting team members")
        article.close()

    @step("inbox: edit draft, Approve & send updates card in place and delivers")
    def _():
        url_before = page.url
        box = card(page, t_ok).locator("textarea[name=message]")
        box.fill(f"Hi! Settings > Members > Invite. ({RUN})")
        card(page, t_ok).get_by_role("button", name="Approve & send").click()
        expect(card(page, t_ok)).not_to_contain_text("Approve & send")
        assert page.url == url_before, "page navigated instead of htmx swap"
        expect(card(page, t_ok)).to_contain_text("Delivered to customer", timeout=120_000)
        expect(card(page, t_ok)).to_contain_text("Sent")

    @step("mock: customer received the edited reply exactly once")
    def _():
        with urllib.request.urlopen(f"{BASE}/mock/helpdesk/tickets/{t_ok}/replies", timeout=30) as r:
            replies = json.loads(r.read())
        assert len(replies) == 1, replies
        assert replies[0]["message"] == f"Hi! Settings > Members > Invite. ({RUN})", replies[0]
        page.goto(BASE + "/mock/helpdesk")
        expect(page.get_by_text(f"Hi! Settings > Members > Invite. ({RUN})")).to_have_count(1)

    @step("inbox: new-ticket banner appears and loads the ticket")
    def _():
        page.goto(BASE + "/operator")
        post_webhook(t_rej, "I want to switch to a different plan")
        banner = page.get_by_role("link", name="new ticket")
        expect(banner).to_be_visible(timeout=20_000)
        banner.click()
        expect(card(page, t_rej)).to_be_visible()

    @step("inbox: Reject asks for confirmation and marks the ticket rejected")
    def _():
        expect(card(page, t_rej).get_by_role("button", name="Reject")).to_be_visible(timeout=90_000)
        page.once("dialog", lambda d: d.accept())
        card(page, t_rej).get_by_role("button", name="Reject").click()
        expect(card(page, t_rej)).to_contain_text("Rejected")
        expect(card(page, t_rej).get_by_role("button", name="Approve & send")).to_have_count(0)

    @step("inbox: failed delivery shows Retry delivery, which re-queues it")
    def _():
        post_webhook(t_fail, "Where do I get an API key?", reply_ticket="MISMATCH")  # helpdesk answers 400
        page.goto(BASE + "/operator")
        expect(card(page, t_fail).get_by_role("button", name="Approve & send")).to_be_visible(timeout=90_000)
        card(page, t_fail).get_by_role("button", name="Approve & send").click()
        expect(card(page, t_fail)).to_contain_text("Delivery failed", timeout=120_000)
        expect(card(page, t_fail)).to_contain_text("Send failed")
        card(page, t_fail).get_by_role("button", name="Retry delivery").click()
        expect(card(page, t_fail)).to_contain_text("Delivery failed", timeout=120_000)
        expect(card(page, t_fail)).to_contain_text("1 attempt")

    @step("inbox: filter tabs and stats")
    def _():
        page.goto(BASE + "/operator")
        for label, key, ticket in [("Sent", "sent", t_ok), ("Rejected", "rejected", t_rej),
                                   ("Failed", "failed", t_fail)]:
            page.locator("#stats").get_by_role("link", name=label).first.click()
            expect(page).to_have_url(f"{BASE}/operator?filter={key}")
            expect(card(page, ticket)).to_be_visible()
        page.locator("#stats").get_by_role("link", name="Needs review").first.click()
        expect(card(page, t_ok)).to_have_count(0)
        page.locator("#stats").get_by_role("link", name="All").first.click()
        expect(card(page, t_ok)).to_be_visible()

    @step("inbox: Refresh keeps the current filter")
    def _():
        page.goto(BASE + "/operator?filter=sent")
        page.get_by_role("link", name="Refresh").click()
        expect(page).to_have_url(BASE + "/operator?filter=sent")

    @step("help center: sidebar navigation between articles")
    def _():
        page.goto(BASE + "/kb")
        page.get_by_role("link", name="Plans and billing").first.click()
        expect(page).to_have_url(BASE + "/kb/billing")
        page.get_by_role("link", name="API keys").first.click()
        expect(page.get_by_role("heading", level=1)).to_have_text("API keys")

    @step("mobile layout renders without horizontal scroll")
    def _():
        m = browser.new_page(viewport={"width": 390, "height": 844})
        for path in ["/operator", "/mock/helpdesk", "/kb/export-pdf"]:
            m.goto(BASE + path)
            width = m.evaluate("document.documentElement.scrollWidth")
            assert width <= 392, f"{path} scrollWidth={width}"
        m.close()

    @step("no JavaScript errors in the console")
    def _():
        assert not console_errors, console_errors[:3]

    browser.close()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
