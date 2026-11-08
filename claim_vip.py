"""Daily VIP reward claimer v13 - fast, no screenshots, flexible ACCOUNTS format."""
import json
import multiprocessing
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor

from playwright.sync_api import sync_playwright

URL = "https://gamesawa.com/ng/#/home"
HEADLESS = "--show" not in sys.argv
WORKERS = int(os.environ.get("WORKERS", "4"))       # lower to 3 if failures appear
RETRY_NOPOPUP = os.environ.get("RETRY_NOPOPUP", "0") == "1"
PHONE_BOX = "input[placeholder='Enter phone number']:visible"
PASS_BOX = "input[placeholder='Enter your password']:visible"


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


def claim_vip(page):
    """Returns 'claimed', 'tapped' or None. The Claim button is part of an
    image on this site, so we tap its position instead of searching text."""
    popup = page.get_by_text("Upgrade to vip", exact=False).filter(visible=True)
    try:
        popup.first.wait_for(state="visible", timeout=8000)
    except Exception:
        return None
    pause(0.9, 1.3)                      # let the popup finish animating
    page.mouse.click(195, 513)           # the Claim button
    got = try_click(page.get_by_text("Click to receive"), timeout=6000)
    pause(0.4, 0.7)
    try:
        if popup.first.is_visible():
            page.mouse.click(349, 104)   # X of the VIP popup
            pause(0.4, 0.6)
    except Exception:
        pass
    return "claimed" if got else "tapped"


def process(browser, iphone, acc, final):
    """Returns (status, reason). status: claimed / tapped / nopopup / failed."""
    m = mask(acc["phone"])
    status, reason = "failed", ""
    for attempt in (1, 2):
        ctx = browser.new_context(**iphone)   # fresh session = no logout needed
        page = ctx.new_page()
        try:
            login(page, acc["phone"], acc["password"])
            result = claim_vip(page)
            if result:
                print(f"{m}: {result.upper()}", flush=True)
                return result, ""
            status = "nopopup"
            reason = "no VIP popup (probably already claimed today)"
            print(f"{m}: NO VIP POPUP", flush=True)
            break
        except Exception as e:
            status = "failed"
            reason = str(e).split("\n")[0][:120]
            print(f"{m}: attempt {attempt} failed - {reason}", flush=True)
        finally:
            ctx.close()
    return status, reason


def worker(chunk, final):
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        iphone = p.devices["iPhone 13"]
        for acc in chunk:
            status, reason = process(browser, iphone, acc, final)
            rows.append((acc["phone"], status, reason))
            pause(0.2, 0.5)
        browser.close()
    return rows


def run_pass(accts, final):
    workers = max(1, min(WORKERS, len(accts)))
    chunks = [accts[k::workers] for k in range(workers)]
    ctx = multiprocessing.get_context("spawn")
    out = {}
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        for rows in ex.map(worker, chunks, [final] * workers):
            for phone, status, reason in rows:
                out[phone] = (status, reason)
    return out


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


def main():
    accounts = load_accounts()

    print(f"SCRIPT VERSION v13 (no screenshots, flexible accounts format) - {len(accounts)} accounts, "
          f"{WORKERS} workers", flush=True)
    start = time.time()

    # Pass 1: all accounts, several at a time (each gets 2 attempts)
    results = run_pass(accounts, False)
    print(f"Pass 1 finished in {(time.time() - start) / 60:.1f} min", flush=True)

    # Pass 2: retry accounts that FAILED (also in parallel)
    retry = [a for a in accounts
             if results[a["phone"]][0] == "failed"
             or (RETRY_NOPOPUP and results[a["phone"]][0] == "nopopup")]
    if retry:
        print(f"\nRETRY PASS: trying {len(retry)} unclaimed accounts again",
              flush=True)
        results.update(run_pass(retry, True))

    claimed = [p for p, (s, _) in results.items() if s in ("claimed", "tapped")]
    failed = [(p, r) for p, (s, r) in results.items() if s == "failed"]
    nopopup = [p for p, (s, _) in results.items() if s == "nopopup"]
    mins = (time.time() - start) / 60
    print("\n" + "=" * 34, flush=True)
    print(f"SUMMARY: {len(claimed)} of {len(accounts)} claimed in {mins:.1f} min",
          flush=True)
    if failed:
        print(f"FAILED after retries ({len(failed)}):", flush=True)
        for phone, reason in failed:
            print(f"  {mask(phone)} - {reason}", flush=True)
    if nopopup:
        print(f"NO VIP POPUP, probably already claimed today ({len(nopopup)}):",
              flush=True)
        print("  " + ", ".join(mask(p) for p in nopopup), flush=True)
    if not failed and not nopopup:
        print("All accounts claimed.", flush=True)
    print("=" * 34, flush=True)
    # red cross in GitHub only if nothing worked or a real failure remains
    if not claimed or failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
