import os, hashlib, json, re
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Error as PWError
import requests

load_dotenv()

# ---------- Config uit env / secrets ----------
TEST_PING_WHEN_NO_DAYS = os.getenv("TEST_PING_WHEN_NO_DAYS", "false").lower() == "true"
AUTO_BOOK = os.getenv("AUTO_BOOK", "false").lower() == "true"
BOOKING_TEST_MODE = os.getenv("BOOKING_TEST_MODE", "none").lower()   # 'none' | 'inject' | 'dry-run'

# Login / navigatie
PASS_USER = os.getenv("PASS_USER") or os.getenv("PASS_GEBRUIKER", "")
PASS_PASS = os.getenv("PASS_PASS") or os.getenv("PASS_WACHTWOORD", "")
BASE_URL  = os.getenv("BASE_URL", "")
CALENDAR_URL = os.getenv("CALENDAR_URL", "")
CALENDAR_SELECTOR = os.getenv("CALENDAR_SELECTOR", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

# Telegram (jij)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
NOTIFY_PREFIX      = os.getenv("NOTIFY_PREFIX", "")

# Telegram (vriend)
FRIEND_TELEGRAM_BOT_TOKEN = os.getenv("FRIEND_TELEGRAM_BOT_TOKEN")
FRIEND_TELEGRAM_CHAT_ID   = os.getenv("FRIEND_TELEGRAM_CHAT_ID")
FRIEND_NOTIFY_PREFIX      = os.getenv("FRIEND_NOTIFY_PREFIX", "")

STATE_FILE = Path("state.json")

# ---------- Helpers ----------
def has_availability(text: str) -> bool:
    """
    True als er wél dagen beschikbaar zijn (dus 'geen dagen' tekst ontbreekt).
    Herkent NL/EN varianten.
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

# ---------- Test: nep 'Schedule' knop injecteren ----------
def inject_fake_schedule_button(page):
    js = """
    (() => {
      const btn = document.createElement('button');
      btn.textContent = 'Schedule';
      btn.style.backgroundColor = '#0a3871';  // donkerblauw
      btn.style.color = 'white';
      btn.style.padding = '10px 16px';
      btn.style.borderRadius = '6px';
      btn.style.border = 'none';
      btn.style.fontSize = '16px';
      btn.style.cursor = 'pointer';
      btn.style.position = 'fixed';
      btn.style.top = '20px';
      btn.style.left = '20px';
      btn.style.zIndex = 999999;
      btn.addEventListener('click', () => {
        const ok = document.createElement('div');
        ok.textContent = 'Booking confirmed';
        ok.style.position = 'fixed';
        ok.style.top = '60px';
        ok.style.left = '20px';
        ok.style.background = '#e6ffed';
        ok.style.color = '#0a662a';
        ok.style.padding = '8px 12px';
        ok.style.border = '1px solid #0a662a';
        ok.style.borderRadius = '6px';
        ok.style.zIndex = 999999;
        document.body.appendChild(ok);
      });
      document.body.appendChild(btn);
    })();
    """
    try:
        page.evaluate(js)
        return True
    except Exception:
        return False

# ---------- Schedule-klik & auto-book ----------
def find_schedule_buttons_sorted(page):
    selectors = [
        'button:has-text("Schedule")',
        'a:has-text("Schedule")',
        '[role="button"]:has-text("Schedule")',
        'input[type="button"][value*="Schedule"]',
        'input[type="submit"][value*="Schedule"]'
    ]
    locs = []
    for sel in selectors:
        try:
            q = page.locator(sel)
            count = q.count()
            for i in range(min(count, 50)):
                locs.append(q.nth(i))
        except Exception:
            pass

    def y_of(l):
        try:
            box = l.bounding_box()
            return box["y"] if box else 1e9
        except Exception:
            return 1e9

    return sorted(locs, key=y_of)

def auto_book_top_schedule(page):
    """
    Klik de visueel bovenste 'Schedule'-knop, bevestig indien nodig,
    en valideer met succeswoorden. Log snapshots voor debug.
    """
    try:
        Path("pre_booking.html").write_text(page.content())
        page.screenshot(path="pre_booking.png", full_page=True)
    except Exception:
        pass

    btns = find_schedule_buttons_sorted(page)
    if not btns:
        return {"success": False, "reason": "no-schedule-buttons"}

    btn = btns[0]
    try:
        try:
            page.evaluate("""(el)=>{el.style.outline='3px solid #00ff88'}""", btn)
        except Exception:
            pass

        btn.scroll_into_view_if_needed(timeout=7000)
        try:
            btn.click(timeout=7000)
        except Exception:
            btn.click(timeout=7000, force=True)

        # mogelijke bevestiging
        try_click_any(page, [
            'button:has-text("Confirm")', 'button:has-text("Bevestig")',
            'button:has-text("Bevestigen")', 'button:has-text("Yes")', 'button:has-text("Ja")',
            'input[type="submit"]'
        ], timeout_ms=5000)

        page.wait_for_load_state("networkidle", timeout=20000)

        try:
            Path("post_click.html").write_text(page.content())
            page.screenshot(path="post_click.png", full_page=True)
        except Exception:
            pass

        body = ""
        try:
            body = page.inner_text("body").lower()
        except Exception:
            pass
        success_words = ["geboekt", "gepland", "bevestigd", "booked", "scheduled", "confirmed"]
        ok = any(w in body for w in success_words)

        if not ok:
            try_click_any(page, [
                'button:has-text("Confirm")', 'button:has-text("Bevestig")',
                'button:has-text("Yes")', 'button:has-text("Ja")'
            ], timeout_ms=3000)
            page.wait_for_load_state("networkidle", timeout=10000)
            try:
                body = page.inner_text("body").lower()
                ok = any(w in body for w in success_words)
            except Exception:
                pass

        return {"success": ok, "reason": "ok" if ok else "no-success-text"}

    except Exception as e:
        try:
            Path("booking_error.html").write_text(page.content())
            page.screenshot(path="booking_error.png", full_page=True)
        except Exception:
            pass
        return {"success": False, "reason": f"exception: {e}"}

# ---------- Hoofdflow ----------
def run_check():
    if not (PASS_USER and PASS_PASS and BASE_URL):
        raise SystemExit("Vul je gebruikersnaam (PASS_USER/PASS_GEBRUIKER), wachtwoord (PASS_PASS/PASS_WACHTWOORD) en BASE_URL in .env / secrets in.")

    state = load_state()
    already_booked = bool(state.get("booked", False))
    booking_result = None  # vullen we bij poging tot boeken

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

            # ---------- TESTMODI ----------
            if BOOKING_TEST_MODE in ("inject", "dry-run"):
                # Forceer 'er is beschikbaarheid' voor test
                text = "test override: availability present"
                if BOOKING_TEST_MODE == "inject":
                    inject_fake_schedule_button(page)
                    booking_result = auto_book_top_schedule(page)
                else:  # dry-run
                    booking_result = {"success": True, "reason": "dry-run"}

            # ---------- Normale modus ----------
            elif AUTO_BOOK and has_availability(text) and not already_booked:
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

    # ---- Beschikbaarheidslogica & meldingen ----
    available_now = has_availability(text)
    was_available = bool(state.get("available", False))

    state["last_seen"] = datetime.now().isoformat(timespec="seconds")
    state["available"] = available_now
    state["calendar_hash"] = calc_hash(text)

    # Succesvol automatisch geboekt?
    if booking_result and booking_result.get("success"):
        state["booked"] = True
        save_state(state)
        notify_self("✅ Slot automatisch GEBOEKT via bovenste 'Schedule'-knop. Check PASS ter bevestiging.")
        # Vriend krijgt alleen "plekken beschikbaar"
        notify_friend("🎉 Er zijn plekken beschikbaar op PASS.")
        return

    save_state(state)

    # Testping alleen als je die expliciet aanzet én er géén dagen zijn
    if TEST_PING_WHEN_NO_DAYS and not available_now:
        notify_both("🧪 Testping: 'Geen dagen gevonden.' is zichtbaar — melding werkt ✅")

    # Bij overgang naar beschikbaarheid, maar niet (of niet succesvol) auto-geboekt
    if available_now and not was_available:
        if AUTO_BOOK and not (booking_result and booking_result.get("success")):
            # auto-book geprobeerd maar mislukte, of niet geprobeerd (geen knop)
            reason = (booking_result or {}).get("reason", "no-attempt")
            notify_self(f"⚠️ Dagen gevonden maar automatisch boeken mislukte (reden: {reason}). Boek handmatig.")
            notify_friend("🎉 Er zijn plekken beschikbaar op PASS.")
        elif not AUTO_BOOK:
            notify_both("🎉 Er zijn NU dagen beschikbaar op PASS. Snel inloggen en plannen!")
    else:
        print("[INFO] Geen nieuwe beschikbaarheid.")

if __name__ == "__main__":
    run_check()

