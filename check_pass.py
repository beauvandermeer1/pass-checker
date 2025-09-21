import os, hashlib, json, re
from playwright.sync_api import Error as PWError, TimeoutError
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import requests

load_dotenv()

TEST_PING_WHEN_NO_DAYS = os.getenv("TEST_PING_WHEN_NO_DAYS", "false").lower() == "true"
PASS_USER = os.getenv("PASS_USER", "")
PASS_PASS = os.getenv("PASS_PASS", "")
BASE_URL  = os.getenv("BASE_URL", "")
CALENDAR_URL = os.getenv("CALENDAR_URL", "")
CALENDAR_SELECTOR = os.getenv("CALENDAR_SELECTOR", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

STATE_FILE = Path("state.json")

def has_availability(text: str) -> bool:
    """
    True als er wél dagen beschikbaar zijn (dus de 'geen dagen' tekst ontbreekt).
    We checken op NL/EN varianten voor de zekerheid.
    """
    pattern = r"(geen\s+dagen\s+gevonden|geen\s+dagen\s+beschikbaar|no\s+days\s+found)"
    return not re.search(pattern, text, flags=re.I)

def notify(msg: str):
    tok = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": msg}, timeout=10)
        except Exception:
            print(msg)
    else:
        print(msg)

def normalize(text: str) -> str:
    t = re.sub(r"\s+", " ", text)
    t = re.sub(r"\b\d{1,2}[:.]\d{2}(:\d{2})?\b", "", t)         # tijden
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", t)                 # 2025-09-18
    t = re.sub(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", "", t)     # 18/09/2025
    return t.strip()

def calc_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def first_present(page, selectors, timeout_ms=7000):
    for sel in selectors:
        try:
            page.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)
            return sel
        except Exception:
            pass
    return None

def try_click_any(page, selectors, timeout_ms=3000):
    for sel in selectors:
        try:
            page.locator(sel).first.click(timeout=timeout_ms)
            return sel
        except Exception:
            pass
    return None

def login(page):
    user_candidates = [
        'input[name="username"]', '#username', 'input[name="email"]',
        '#email', 'input[type="email"]', 'input[type="text"]'
    ]
    pass_candidates = ['input[name="password"]', '#password', 'input[type="password"]']
    submit_candidates = [
        'button[type="submit"]', 'input[type="submit"]',
        'button:has-text("Inloggen")', 'button:has-text("Login")', 'button:has-text("Aanmelden")'
    ]

    u_sel = first_present(page, user_candidates)
    p_sel = first_present(page, pass_candidates)
    if not u_sel or not p_sel:
        raise RuntimeError("Kon loginvelden niet vinden. Pas selectors aan.")

    page.locator(u_sel).first.fill(PASS_USER)
    page.locator(p_sel).first.fill(PASS_PASS)
    if not try_click_any(page, submit_candidates):
        page.locator(p_sel).first.press("Enter")

def extract_calendar_text(page):
    if CALENDAR_SELECTOR:
        page.wait_for_selector(CALENDAR_SELECTOR, timeout=15000)
        return page.locator(CALENDAR_SELECTOR).first.inner_text()

    fallbacks = [
        '#selection-calendar', '.availability-table', '[data-test*=avail]',
        '[class*=availability]', '[class*=calendar]', '[role="table"]', 'table'
    ]
    sel = first_present(page, fallbacks, timeout_ms=6000)
    if sel:
        return page.locator(sel).first.inner_text()
    return page.inner_text("body")

def read_calendar_resilient(context, page):
    """Lees de kalendertekst en herstel als het tabblad onderweg wisselt/sluit."""
    # pak altijd de laatste/actieve tab
    if len(context.pages) > 0:
        page = context.pages[-1]
    page.wait_for_selector("body", timeout=20000)
    page.wait_for_load_state("networkidle", timeout=20000)
    try:
        return extract_calendar_text(page)
    except PWError:
        # opnieuw proberen met het (mogelijk) nieuwe tabblad
        if len(context.pages) > 0:
            page = context.pages[-1]
            page.wait_for_selector("body", timeout=20000)
            page.wait_for_load_state("networkidle", timeout=20000)
            return extract_calendar_text(page)
        raise

def run_check():
    if not (PASS_USER and PASS_PASS and BASE_URL):
        raise SystemExit("Vul PASS_USER, PASS_PASS en BASE_URL in .env in.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()
        try:
            # Login
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=45000)
            login(page)
            page.wait_for_load_state("networkidle", timeout=30000)

            # Naar schedule/kalender
            if CALENDAR_URL:
                page.goto(CALENDAR_URL, wait_until="domcontentloaded", timeout=45000)
            else:
                clicked = try_click_any(page, [
                    'button:has-text("Schedule")', 'a:has-text("Schedule")',
                    'a[href*="/testRounds/"]'
                ], timeout_ms=6000)
                # domcontentloaded is vaak sneller beschikbaar dan networkidle
                page.wait_for_load_state("domcontentloaded", timeout=30000)

            # sommige acties openen/verwisselen het tabblad – pak altijd de laatste
            if len(context.pages) > 0:
                page = context.pages[-1]

            # Data lezen met herstel bij tab-swap
            text = read_calendar_resilient(context, page)

        except (PWError, Exception) as e:
            try:
                # Probeer nog een snapshot te bewaren voor debug
                if len(context.pages) > 0:
                    p_last = context.pages[-1]
                    p_last.screenshot(path="error_screenshot.png", full_page=True)
                    Path("last_page.html").write_text(p_last.content())
            except Exception:
                pass
            raise RuntimeError(
                f"Fout tijdens ophalen: {e}\nKijk naar 'error_screenshot.png' en 'last_page.html'."
            ) from e
        finally:
            context.close()
            browser.close()

        # ---- Beschikbaarheidslogica op tekst ----
    available_now = has_availability(text)
	
    state = load_state()
    was_available = bool(state.get("available", False))

    # Sla altijd op wanneer we voor het laatst keken + (optioneel) hash voor debug
    state["last_seen"] = datetime.now().isoformat(timespec="seconds")
    state["available"] = available_now
    state["calendar_hash"] = calc_hash(text)  # puur handig voor debug/geschiedenis
    save_state(state)

    # TESTMODUS: stuur een ping als er GEEN dagen zijn (om Telegram te testen)
    if TEST_PING_WHEN_NO_DAYS and not available_now:
        notify("🧪 Testping: 'Geen dagen gevonden.' is zichtbaar — melding werkt ✅")

    if available_now and not was_available:
        # Overgang: van 'geen dagen' -> 'wel dagen' => ping!
        notify("🎉 Er zijn NU dagen beschikbaar op PASS. Snel inloggen en plannen!")
    else:
        print("[INFO] Geen nieuwe beschikbaarheid.")


if __name__ == "__main__":
    run_check()
