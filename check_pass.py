import os, hashlib, json, re
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Error as PWError
import requests

load_dotenv()

# --------- Config uit env / secrets ----------
TEST_PING_WHEN_NO_DAYS = os.getenv("TEST_PING_WHEN_NO_DAYS", "false").lower() == "true"
AUTO_BOOK = os.getenv("AUTO_BOOK", "false").lower() == "true"   # zet op true als je wilt auto-boeken
PASS_USER = os.getenv("PASS_USER", "")
PASS_PASS = os.getenv("PASS_PASS", "")
BASE_URL  = os.getenv("BASE_URL", "")
CALENDAR_URL = os.getenv("CALENDAR_URL", "")
CALENDAR_SELECTOR = os.getenv("CALENDAR_SELECTOR", "")
HEADLESS = os.getenv("HEADLESS", "")

# primair (jij)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
NOTIFY_PREFIX      = os.getenv("NOTIFY_PREFIX", "")

# vriend (extra ontvanger)
FRIEND_TELEGRAM_BOT_TOKEN = os.getenv("FRIEND_TELEGRAM_BOT_TOKEN")
FRIEND_TELEGRAM_CHAT_ID   = os.getenv("FRIEND_TELEGRAM_CHAT_ID")
FRIEND_NOTIFY_PREFIX      = os.getenv("FRIEND_NOTIFY_PREFIX", "")

STATE_FILE = Path("state.json")

# --------- Helpers ----------
def has_availability(text: str) -> bool:
    """
    True als er wél dagen beschikbaar zijn (dus de 'geen dagen' tekst ontbreekt).
    Check NL/EN varianten.
    """
    pattern = r"(geen\s+dagen\s+gevonden|geen\s+dagen\s+beschikbaar|no\s+days\s+found)"
    return not re.search(pattern, text, flags=re.I)

def _send_telegram(token: str, chat_id: str, msg: str) -> bool:
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=10
        )
        return True
    except Exception:
        return False

def notify_both(msg: str):
    """Stuur dezelfde melding naar jou én (indien ingesteld) naar je vriend."""
    any_sent = False
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        m = f"{NOTIFY_PREFIX}{msg}" if NOTIFY_PREFIX else msg
        any_sent = _send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, m) or any_sent
    if FRIEND_TELEGRAM_BOT_TOKEN and FRIEND_TELEGRAM_CHAT_ID:
        m2 = f"{FRIEND_NOTIFY_PREFIX}{msg}" if FRIEND_NOTIFY_PREFIX else msg
        any_sent = _send_telegram(FRIEND_TELEGRAM_BOT_TOKEN, FRIEND_TELEGRAM_CHAT_ID, m2) or any_sent
    if not any_sent:
        print(msg)

def notify_self(msg: str):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        m = f"{NOTIFY_PREFIX}{msg}" if NOTIFY_PREFIX else msg
        if not _send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, m):
            print(msg)
    else:
        print(msg)

def notify_friend(msg: str):
    if FRIEND_TELEGRAM_BOT_TOKEN and FRIEND_TELEGRAM_CHAT_ID:
        m2 = f"{FRIEND_NOTIFY_PREFIX}{msg}" if FRIEND_NOTIFY_PREFIX else msg
        if not _send_telegram(FRIEND_TELEGRAM_BOT_TOKEN, FRIEND_TELEGRAM_CHAT_ID, m2):
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
    if len(context.pages) > 0:
        page = context.pages[-1]
    page.wait_for_selector("body", timeout=20000)
    page.wait_for_load_state("networkidle", timeout=20000)
    try:
        return extract_calendar_text(page)
    except PWError:
        if len(context.pages) > 0:
            page = context.pages[-1]
            page.wait_for_selector("body", timeout=20000)
            page.wait_for_load_state("networkidle", timeout=20000)
            return extract_calendar_text(page)
        raise

# ---------- Schedule-button zoeken & boeken ----------
def find_schedule_buttons_sorted(page):
    """
    Vind alle 'Schedule' knoppen/links en sorteer op schermpositie (Y-waarde) -> bovenste eerst.
    We richten ons op de donkerblauwe 'Schedule'-knoppen door op tekst te filteren.
    """
    # brede selectors met nadruk op tekst 'Schedule'
    candidates = [
        'button:has-text("Schedule")',
        'a:has-text("Schedule")',
        '[role="button"]:has-text("Schedule")',
        'input[type="button"][value*="Schedule"]',
        'input[type="submit"][value*="Schedule"]'
    ]
    locs = []
    for sel in candidates:
        try:
            loc = page.locator(sel)
            count = loc.count()
            for i in range(min(count, 50)):
                locs.append(loc.nth(i))
        except Exception:
            continue

    # sorteer op top-positie (y); soms geeft bounding_box None als element offscreen is
    def y_of(l):
        try:
            box = l.bounding_box()
            return box["y"] if box else 1e9
        except Exception:
            return 1e9

    locs_sorted = sorted(locs, key=y_of)
    return locs_sorted

def auto_book_top_schedule(page):
    """
    Klik de visueel bovenste 'Schedule'-knop, bevestig indien nodig,
    en check heuristisch of de boeking gelukt is.
    """
    btns = find_schedule_buttons_sorted(page)
    if not btns:
        return {"success": False, "reason": "no-schedule-buttons"}

    # klik de bovenste
    btns[0].click(timeout=7000)

    # mogelijke bevestiging
    try_click_any(page, [
        'button:has-text("Confirm")', 'button:has-text("Bevestig")',
        'button:has-text("Bevestigen")', 'button:has-text("Yes")', 'button:has-text("Ja")',
        'input[type="submit"]'
    ], timeout_ms=4000)

    page.wait_for_load_state("networkidle", timeout=20000)

    # heuristische check op succeswoorden
    try:
        body = page.inner_text("body").lower()
    except Exception:
        body = ""
    success_words = ["geboekt", "gepland", "bevestigd", "booked", "scheduled", "confirmed"]
    ok = any(w in body for w in success_words)
    return {"success": ok, "reason": "ok" if ok else "no-success-text"}

# --------- Hoofdflow ----------
def run_check():
    if not (PASS_USER and PASS_PASS and BASE_URL):
        raise SystemExit("Vul PASS_USER, PASS_PASS en BASE_URL in .env / secrets in.")

    state = load_state()
    already_booked = bool(state.get("booked", False))
    booking_result = None  # vullen we alleen bij poging tot boeken

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
                try_click_any(page, [
                    'button:has-text("Schedule")', 'a:has-text("Schedule")',
                    'a[href*="/testRounds/"]'
                ], timeout_ms=6000)
                page.wait_for_load_state("domcontentloaded", timeout=30000)

            if len(context.pages) > 0:
                page = context.pages[-1]

            # Data lezen
            text = read_calendar_resilient(context, page)

            # Auto-book: klik BOVENSTE 'Schedule'-knop als er beschikbaarheid is
            if AUTO_BOOK and not already_booked and has_availability(text):
                booking_result = auto_book_top_schedule(page)

        except (PWError, Exception) as e:
            try:
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

    was_available = bool(state.get("available", False))
    state["last_seen"] = datetime.now().isoformat(timespec="seconds")
    state["available"] = available_now
    state["calendar_hash"] = calc_hash(text)  # handig voor debug

    # Als er net is geboekt, markeer dat en meld verschillend aan jou vs vriend
    if booking_result and booking_result.get("success"):
        state["booked"] = True
        save_state(state)
        notify_self("✅ Slot automatisch GEBOEKT via bovenste 'Schedule'-knop. Check PASS ter bevestiging.")
        notify_friend("🎉 Er zijn plekken beschikbaar op PASS.")
        return

    save_state(state)

    # Testmodus: ping sturen als er GEEN dagen zijn (om Telegram te testen)
    if TEST_PING_WHEN_NO_DAYS and not available_now:
        notify_both("🧪 Testping: 'Geen dagen gevonden.' is zichtbaar — melding werkt ✅")

    # Normale meldlogica:
    # - Alleen bij overgang naar beschikbaar
    # - Als auto-book mislukte, stuur jij een waarschuwing; vriend krijgt 'spots available'
    if available_now and not was_available:
        if AUTO_BOOK and not already_booked:
            if booking_result and not booking_result.get("success"):
                notify_self(f"⚠️ Dagen gevonden maar automatisch boeken mislukte (reden: {booking_result.get('reason')}). Boek handmatig.")
                notify_friend("🎉 Er zijn plekken beschikbaar op PASS.")
            elif booking_result is None:
                notify_self("⚠️ Dagen gevonden, maar geen 'Schedule'-knop gedetecteerd. Boek handmatig.")
                notify_friend("🎉 Er zijn plekken beschikbaar op PASS.")
            # als booking_result success was, zijn we al gereturned
        else:
            # geen auto-book: gewoon beide melden dat er plekken zijn
            notify_both("🎉 Er zijn NU dagen beschikbaar op PASS. Snel inloggen en plannen!")
    else:
        print("[INFO] Geen nieuwe beschikbaarheid.")

if __name__ == "__main__":
    run_check()

