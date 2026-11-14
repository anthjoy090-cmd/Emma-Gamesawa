"""Lucky Draw spinner v1 - spins each account's coins, then reports the wallets.

MANUAL USE ONLY (no schedule). Uses the same ACCOUNTS secret as the claim script.
Rules: bet the biggest of 10000 / 2000 / 500 / 200 that the coin balance allows,
tap Play, go back right away (no waiting for the animation), open Lucky Draw
again, repeat. Stop when the coins are below 200. Only COINS are spent - the
wallet is never touched. Final line shows the total wallet of all accounts.
"""
import json
import multiprocessing
import os
import random
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal

from playwright.sync_api import sync_playwright

URL = "https://gamesawa.com/ng/#/home"
HEADLESS = "--show" not in sys.argv
WORKERS = int(os.environ.get("WORKERS", "4"))       # lower to 3 if failures appear
BLOCK_HEAVY = os.environ.get("BLOCK_HEAVY", "0") == "1"   # skip images/fonts/media
MAX_SPINS = int(os.environ.get("MAX_SPINS", "400"))  # safety cap per account
BETS = [10000, 2000, 500, 200]                      # biggest first
PHONE_BOX = "input[placeholder='Enter phone number']:visible"
PASS_BOX = "input[placeholder='Enter your password']:visible"
PLAY_RE = re.compile(r"^\s*Play\s+[\d,]+\s+Coins\s*$")
NUM = r"([\d,]+(?:\.\d+)?)"

def pause(a=0.3, b=0.7):
    time.sleep(random.uniform(a, b))

def mask(phone):
    return phone[:4] + "****" + phone[-2:]

def try_click(locator, timeout=4000):
    try:
        locator.filter(visible=True).first.click(timeout=timeout)
        return True
    except Exception:
        return False

def close_promo(page):
    # The "Register now" promo popup covers the Log In button; tap its X.
    try:
        page.get_by_text("Register now").filter(visible=True).first.wait_for(
            state="visible", timeout=3000)
        page.mouse.click(339, 166)
        pause(0.5, 0.8)
    except Exception:
        pass

def login(page, phone, password):
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    try:  # wait for the VISIBLE Log In button (a hidden copy may exist)
        page.get_by_text("Log In", exact=True).filter(visible=True).first.wait_for(
            state="visible", timeout=12000)
    except Exception:
        pass  # carry on; the next steps have their own checks
    pause(0.3, 0.6)
    close_promo(page)
    for attempt in range(3):           # open the login box (retry if ignored)
        if not try_click(page.get_by_text("Log In", exact=True), timeout=8000):
            page.mouse.click(253, 26)  # fallback: tap the Log In spot
        try:
            page.locator(PHONE_BOX).first.wait_for(state="visible", timeout=4000)
            break
        except Exception:
            close_promo(page)
            if attempt == 2:
                raise Exception("login box did not open")
    try:                               # Register form instead of Login form?
        if page.get_by_text("Create Account").filter(visible=True).count() == 0:
            try_click(page.get_by_text("Log in", exact=True), timeout=3000)
            pause(0.5, 0.8)
    except Exception:
        pass
    page.locator(PHONE_BOX).first.fill(phone, timeout=8000)
    page.locator(PASS_BOX).first.fill(password, timeout=8000)
    pause(0.2, 0.4)
    if not try_click(page.get_by_text("Log in", exact=True), timeout=8000):
        raise Exception("Could not find the Log in button")


def load_accounts():
    """Reads the ACCOUNTS secret. Accepts any of these layouts:
      1) a JSON list:  [{"phone":"0801...","password":"abc"},{...}]
      2) one JSON object per line
      3) simple lines:  08012345678 mypassword   (a comma instead of a space is OK)
    """
    raw = os.environ.get("ACCOUNTS", "").strip()
    if not raw and os.path.exists("accounts.json"):
        with open("accounts.json") as f:
            raw = f.read().strip()

    def clean(a):
        return {"phone": str(a["phone"]).strip(), "password": str(a["password"])}

    try:                                   # layout 1 (or a single object)
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        accounts = [clean(a) for a in data]
        if accounts:
            return accounts
    except Exception:
        pass

    accounts = []                          # layouts 2 and 3, line by line
    for line in raw.splitlines():
        line = line.strip().rstrip(",").strip()
        if line in ("", "[", "]", "[]"):
            continue
        if line.startswith("{"):
            try:
                accounts.append(clean(json.loads(line)))
                continue
            except Exception:
                pass
        parts = line.replace(",", " ").replace(";", " ").split()
        if len(parts) >= 2:
            accounts.append({"phone": parts[0], "password": parts[1]})
    if not accounts:
        print("ERROR: no accounts found in the ACCOUNTS secret.\n"
              "Put one account per line like this:\n"
              "08012345678 mypassword", flush=True)
        sys.exit(1)
    return accounts

_shot_count = {}


def debug_shot(page, name, limit=2):
    """Temporary: at most `limit` failure screenshots per worker."""
    n = _shot_count.get("fail", 0)
    if n < limit:
        _shot_count["fail"] = n + 1
        try:
            page.screenshot(path=name, scale="css")
        except Exception:
            pass


def to_dec(text):
    return Decimal(text.replace(",", ""))


def page_text(page):
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def read_coins(page):
    m = re.search(r"\bCoin\b\s*" + NUM, page_text(page))
    return to_dec(m.group(1)) if m else None


def read_wallet_home(page):
    m = re.search(NUM + r"\s*NGN", page_text(page))
    return to_dec(m.group(1)) if m else None


class BetLocked(Exception):
    """The site shows 'VIP 2 Unlock' for this bet - the account cannot use it."""


def choose_bet(coins, locked=()):
    for bet in BETS:
        if bet not in locked and coins >= bet:
            return bet
    return None


def bet_locked(page):
    try:
        return page.get_by_text("Unlock").filter(visible=True).count() > 0
    except Exception:
        return False


def play_button(page):
    return page.get_by_text(PLAY_RE).filter(visible=True).first


def dismiss_vip_popup(page):
    """If the VIP Reward popup shows after login, just close it (no claiming)."""
    try:
        page.get_by_text("Vip Gift", exact=True).filter(visible=True).first.wait_for(
            state="visible", timeout=10000)
        page.get_by_text("Upgrade to vip", exact=False).filter(
            visible=True).first.wait_for(state="visible", timeout=2000)
        page.mouse.click(349, 104)           # X of the VIP popup
        pause(0.4, 0.6)
    except Exception:
        pass


def route(page):
    """The page's #/route, lower-case (e.g. '#/home', '#/luckdraw')."""
    try:
        return (page.evaluate("location.hash") or "").lower()
    except Exception:
        return ""


def on_home(page):
    return route(page).startswith("#/home")


def on_game(page):
    return "luckdraw" in route(page)


def wait_home(page, timeout=6000):
    end = time.time() + timeout / 1000
    while time.time() < end:
        if on_home(page):
            try:    # let the tiles render
                page.get_by_text("Lucky Draw", exact=True).filter(
                    visible=True).first.wait_for(state="visible", timeout=4000)
            except Exception:
                pass
            pause(0.3, 0.5)
            return True
        time.sleep(0.1)
    return False


def recover_home(page):
    """Reload the site (stays logged in). Used if a stray tap opened a game."""
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    if not wait_home(page, timeout=15000):
        page.reload(wait_until="domcontentloaded", timeout=45000)
        if not wait_home(page, timeout=15000):
            raise Exception(f"could not recover the home page (route {route(page)!r})")
    dismiss_vip_popup(page)


def open_lucky_draw(page):
    for _ in range(3):
        if on_game(page):
            try:
                play_button(page).wait_for(state="visible", timeout=12000)
                pause(0.3, 0.6)
                return
            except Exception:
                recover_home(page)
        elif not on_home(page):
            recover_home(page)               # e.g. a stray tap opened a game
        if not try_click(page.get_by_text("Lucky Draw", exact=True), timeout=6000):
            page.mouse.click(118, 90)        # fallback: the gift icon
        try:
            page.wait_for_function(
                "location.hash.toLowerCase().includes('luckdraw')", timeout=12000)
            play_button(page).wait_for(state="visible", timeout=12000)
            pause(0.3, 0.6)
            return
        except Exception:
            pass                             # wrong page? loop recovers and retries
    raise Exception(f"could not open Lucky Draw (route {route(page)!r})")


def select_bet(page, bet):
    for _ in range(2):
        pb = play_button(page).bounding_box()
        cands = page.get_by_text(str(bet), exact=True).filter(visible=True)
        best = None
        for i in range(cands.count()):
            el = cands.nth(i)
            bb = el.bounding_box()
            # the bet buttons sit just above the Play button
            if bb and pb and bb["y"] < pb["y"] and (
                    best is None or bb["y"] > best[1]["y"]):
                best = (el, bb)
        if best:
            try:
                best[0].click(timeout=5000)
            except Exception:
                pass
        try:
            page.get_by_text(f"Play {bet} Coins").filter(
                visible=True).first.wait_for(state="visible", timeout=3000)
            return
        except Exception:
            pause(0.4, 0.7)
    raise Exception(f"could not select the {bet} bet")


def spin_once(page, coins, bet):
    """Pick the bet, tap Play, wait until the coins drop. Returns new coins."""
    try:
        select_bet(page, bet)
    except Exception:
        if bet_locked(page):
            raise BetLocked(bet)
        raise
    if not try_click(page.get_by_text(PLAY_RE), timeout=6000):
        if bet_locked(page):
            raise BetLocked(bet)
        raise Exception("could not tap the Play button")
    for _ in range(14):
        pause(0.35, 0.5)
        c = read_coins(page)
        if c is not None and c < coins:
            return c
    if bet_locked(page):                      # coins did not drop + lock popup
        raise BetLocked(bet)
    return coins                              # did not register


def go_home(page):
    """Leave the game screen. Tries browser-back, the in-app arrow, then the route."""
    try:
        page.go_back(timeout=8000)
    except Exception:
        pass
    if wait_home(page, 5000):
        return
    if on_game(page):
        page.mouse.click(15, 25)             # the "< Lucky Draw" arrow
        if wait_home(page, 5000):
            return
    try:
        page.evaluate("location.hash = '#/home'")
    except Exception:
        pass
    if wait_home(page, 5000):
        return
    recover_home(page)                       # last resort: reload


def stable_wallet(page):
    last = None
    for _ in range(4):
        w = read_wallet_home(page)
        if w is not None and w == last:
            return w
        last = w
        pause(0.7, 1.0)
    return last


def spin_account(page, phone, password, state):
    login(page, phone, password)
    dismiss_vip_popup(page)
    open_lucky_draw(page)
    coins = read_coins(page)
    if coins is None:
        raise Exception("could not read the coin balance")
    stuck = 0
    locked = set()
    while state["spins"] < MAX_SPINS:
        bet = choose_bet(coins, locked)
        if bet is None:                       # nothing affordable/unlocked: stop
            break
        try:
            new_coins = spin_once(page, coins, bet)
        except BetLocked:
            locked.add(bet)                   # this account can't use that bet
            go_home(page)
            open_lucky_draw(page)
            c = read_coins(page)
            if c is not None:
                coins = c
            continue
        if new_coins < coins:
            state["spins"] += 1
            stuck = 0
        else:
            stuck += 1
            if stuck >= 3:
                raise Exception("spins did not register")
        go_home(page)                          # leave while it animates
        open_lucky_draw(page)                  # coins/wallet refresh on re-entry
        c = read_coins(page)
        if c is None:
            raise Exception("could not read the coin balance")
        coins = c
    go_home(page)
    wallet = stable_wallet(page)
    if wallet is None:
        raise Exception("could not read the wallet balance")
    return state["spins"], coins, wallet


def fmt(amount):
    return f"{amount:,.2f}"


def process(browser, iphone, acc):
    """Returns (status, spins, coins_left, wallet, reason)."""
    m = mask(acc["phone"])
    tag = f"acc{acc['phone'][-6:]}"
    reason = ""
    state = {"spins": 0}      # shared by all attempts, so earlier spins still count
    for attempt in (1, 2, 3):
        ctx = browser.new_context(**iphone)
        if BLOCK_HEAVY:
            ctx.route("**/*", lambda route: route.abort()
                      if route.request.resource_type in ("image", "media", "font")
                      else route.continue_())
        page = ctx.new_page()
        try:
            spins, coins, wallet = spin_account(
                page, acc["phone"], acc["password"], state)
            if spins > 0:
                print(f"{m}: SPINNED {fmt(wallet)} ({spins} spins)", flush=True)
                return "spinned", spins, coins, wallet, ""
            print(f"{m}: NO SPIN (coins {coins:,.0f}) {fmt(wallet)}", flush=True)
            return "nospin", 0, coins, wallet, ""
        except Exception as e:
            reason = str(e).split("\n")[0][:120]
            print(f"{m}: attempt {attempt} failed - {reason}", flush=True)
            if attempt >= 2:
                body = page_text(page)[:150].replace("\n", " | ")
                print("   PAGE SAYS:", page.url, "|", body, flush=True)
                debug_shot(page, f"{tag}_failed.png")
        finally:
            ctx.close()
    return "failed", state["spins"], None, None, reason


def worker(chunk):
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        iphone = p.devices["iPhone 13"]
        for acc in chunk:
            rows.append((acc["phone"],) + process(browser, iphone, acc))
            pause(0.2, 0.5)
        browser.close()
    return rows


def main():
    accounts = load_accounts()
    print(f"SCRIPT VERSION spin FAST v1 - {len(accounts)} accounts, "
          f"{WORKERS} workers", flush=True)
    start = time.time()
    workers = max(1, min(WORKERS, len(accounts)))
    chunks = [accounts[k::workers] for k in range(workers)]
    ctx = multiprocessing.get_context("spawn")
    rows = []
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        for part in ex.map(worker, chunks):
            rows += part

    ok = [r for r in rows if r[1] in ("spinned", "nospin")]
    failed = [r for r in rows if r[1] == "failed"]
    total_wallet = sum((r[4] for r in ok), Decimal(0))
    total_spins = sum(r[2] for r in ok)
    mins = (time.time() - start) / 60
    print("\n" + "=" * 34, flush=True)
    print(f"SUMMARY: {len(ok)} of {len(accounts)} accounts done in {mins:.1f} min "
          f"({total_spins} spins)", flush=True)
    print(f"TOTAL WALLET: {fmt(total_wallet)} NGN", flush=True)
    if failed:
        print(f"FAILED ({len(failed)}) - their wallets are NOT in the total:",
              flush=True)
        for r in failed:
            print(f"  {mask(r[0])} - {r[5]}", flush=True)
    print("=" * 34, flush=True)
    if failed or not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
