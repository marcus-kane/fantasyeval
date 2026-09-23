"""Phase 1: ESPN auth.

Grabs espn_s2 + SWID from a Playwright persistent profile (./espn_profile/) and
writes them to .env alongside LEAGUE_ID and YEAR. The first run (or an expired
session) opens a headed browser so you can log in by hand; later runs are headless.

    uv run python auth.py            # refresh cookies, then verify league access
    uv run python auth.py --verify   # only verify the cookies already in .env

LEAGUE_ID may be a comma-separated list to sync several leagues.
"""
import argparse
import json
import os
import urllib.error
import urllib.request

from dotenv import load_dotenv, set_key
from playwright.sync_api import sync_playwright

ENV = ".env"
PROFILE = "./espn_profile"
LOGIN_URL = "https://www.espn.com/fantasy/football/"
LEAGUE_URL = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}"
              "/segments/0/leagues/{league}?view=mTeam&view=mRoster&view=mSettings")
LOGIN_TIMEOUT_S = 300


class AuthError(RuntimeError):
    pass


def read_cookies(headless):
    """Return {'espn_s2', 'SWID'} from the persistent profile, or None if not logged in."""
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL)
        # Headless: just check. Headed: wait for you to finish logging in.
        for _ in range(1 if headless else LOGIN_TIMEOUT_S):
            got = {c["name"]: c["value"] for c in ctx.cookies() if c["name"] in ("espn_s2", "SWID")}
            if len(got) == 2:
                break
            page.wait_for_timeout(1000)
        ctx.close()
    return got if len(got) == 2 else None


def check_league(data, league):
    """Fail loudly on a response that authenticated but carries no roster data."""
    teams = data.get("teams") or []
    if not teams:
        raise AuthError(f"League {league}: no teams returned. Wrong LEAGUE_ID/YEAR, or cookies lack access.")
    if not any(t.get("roster", {}).get("entries") for t in teams):
        raise AuthError(f"League {league}: every roster is empty. Cookies are likely stale; rerun `uv run python auth.py`.")
    return len(teams)


def verify():
    load_dotenv(ENV, override=True)
    s2, swid, year = os.environ["ESPN_S2"], os.environ["SWID"], os.environ["YEAR"]
    for league in os.environ["LEAGUE_ID"].split(","):
        req = urllib.request.Request(LEAGUE_URL.format(year=year, league=league.strip()),
                                     headers={"Cookie": f"espn_s2={s2}; SWID={swid}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(f"League {league}: HTTP {e.code}. espn_s2/SWID rejected; rerun `uv run python auth.py`.") from e
            raise
        n = check_league(data, league)
        print(f"League {league}: OK ({n} teams, {data.get('settings', {}).get('name', '?')})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="skip the browser, just test .env cookies")
    ap.add_argument("--headed", action="store_true", help="force a visible browser login")
    a = ap.parse_args()
    if not a.verify:
        cookies = None if a.headed else read_cookies(headless=True)
        if cookies is None:
            print(f"Not logged in: log in to ESPN in the browser window (waiting up to {LOGIN_TIMEOUT_S}s)...")
            cookies = read_cookies(headless=False)
        if cookies is None:
            raise AuthError("Login timed out; espn_s2/SWID cookies never appeared.")
        set_key(ENV, "ESPN_S2", cookies["espn_s2"])
        set_key(ENV, "SWID", cookies["SWID"])  # keeps the {curly braces}
        print("Wrote ESPN_S2 and SWID to .env")
    verify()


if __name__ == "__main__":
    main()
