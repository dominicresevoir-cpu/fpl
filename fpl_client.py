"""
Thin wrapper around the FPL public API (undocumented but stable and widely used
by community tools). No API key needed for read-only data.

Auth (optional): FPL doesn't have an official login API, but the same endpoint
the website itself uses can be called directly, which is how most open-source
FPL tools get around the "my team isn't public until the deadline passes"
limitation. Store credentials in your local .env, never commit them.
"""

from __future__ import annotations

import requests
from datetime import datetime, timezone

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0"}


class FPLClient:
    def __init__(self, email: str | None = None, password: str | None = None):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.authenticated = False
        if email and password:
            self._login(email, password)

    def _login(self, email: str, password: str):
        """Best-effort login using FPL's own login form endpoint.
        This mirrors what fantasy.premierleague.com does in-browser.
        Unofficial — if FPL changes their auth flow this may need updating."""
        try:
            resp = self.session.post(
                "https://users.premierleague.com/accounts/login/",
                data={
                    "login": email,
                    "password": password,
                    "redirect_uri": "https://fantasy.premierleague.com/a/login",
                    "app": "plfpl-web",
                },
                timeout=10,
            )
        except requests.RequestException as e:
            print(f"⚠️  FPL login endpoint unreachable ({e}) — falling back to "
                  "public data only.")
            return
        # A successful login redirects; a failed one re-renders the login page.
        if resp.status_code == 200 and "login" not in resp.url:
            self.authenticated = True
        else:
            print("⚠️  FPL login did not appear to succeed — falling back to "
                  "public data only. Double-check FPL_EMAIL / FPL_PASSWORD.")

    def get_bootstrap(self) -> dict:
        r = self.session.get(f"{BASE}/bootstrap-static/")
        r.raise_for_status()
        return r.json()

    def get_fixtures(self, event: int | None = None) -> list:
        url = f"{BASE}/fixtures/"
        if event:
            url += f"?event={event}"
        r = self.session.get(url)
        r.raise_for_status()
        return r.json()

    def get_entry(self, team_id: int) -> dict:
        r = self.session.get(f"{BASE}/entry/{team_id}/")
        r.raise_for_status()
        return r.json()

    def get_public_picks(self, team_id: int, event: int) -> dict | None:
        """Picks for a PAST or CURRENT event. Only visible publicly once
        that gameweek's deadline has passed."""
        r = self.session.get(f"{BASE}/entry/{team_id}/event/{event}/picks/")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def get_my_team(self, team_id: int) -> dict | None:
        """Accurate CURRENT squad, bank, and free transfers — including for
        gameweeks that haven't happened yet. Requires authentication."""
        if not self.authenticated:
            return None
        r = self.session.get(f"{BASE}/my-team/{team_id}/")
        if r.status_code != 200:
            return None
        return r.json()

    def get_league_standings(self, league_id: int, page: int = 1) -> dict:
        r = self.session.get(
            f"{BASE}/leagues-classic/{league_id}/standings/?page_standings={page}"
        )
        r.raise_for_status()
        return r.json()


def current_and_next_event(bootstrap: dict) -> tuple[dict | None, dict]:
    """Returns (last_finished_event, next_unplayed_event)."""
    events = bootstrap["events"]
    last_finished = None
    next_event = None
    for e in events:
        if e["finished"]:
            last_finished = e
        if e["is_next"] or (e["is_current"] and not e["finished"]):
            next_event = next_event or e
    if next_event is None:
        # Season over, or between-seasons — just fall back to last event.
        next_event = events[-1]
    return last_finished, next_event


def deadline_countdown(event: dict) -> str:
    deadline = datetime.fromisoformat(event["deadline_time"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    delta = deadline - now
    if delta.total_seconds() < 0:
        return "deadline has passed"
    hours = int(delta.total_seconds() // 3600)
    return f"{hours} hours ({delta.days}d {hours % 24}h)"
