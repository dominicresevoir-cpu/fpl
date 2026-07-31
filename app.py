"""
Web UI for the FPL agent, on top of the same core.py pipeline the CLI uses.

Run locally with:  python app.py   (or: gunicorn -w 1 app:app)

IMPORTANT: run with a single worker (gunicorn -w 1). The /run cooldown,
daily cap, and the /players cache below are plain module-level state — they
only work as intended when every request hits the same process.
"""

from __future__ import annotations

import hmac
import os
import re
import threading
import time
from datetime import datetime, timezone

import bleach
import markdown
from dotenv import load_dotenv
from flask import Flask, abort, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from core import FPLConfig, fetch_player_data, generate_report

load_dotenv()

app = Flask(__name__)
limiter = Limiter(key_func=get_remote_address, app=app, default_limits=[])

config = FPLConfig.from_env()

REPORTS_DIR = "reports"
GW_REPORT_RE = re.compile(r"^gw(\d+)_report\.md$")

# Deliberately restrictive — report text can include web-search-sourced
# content, so don't trust it with scripts/styles/forms before rendering.
ALLOWED_TAGS = [
    "p", "br", "strong", "em", "ul", "ol", "li", "a", "code", "pre",
    "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
    "table", "thead", "tbody", "tr", "th", "td",
]
ALLOWED_ATTRS = {"a": ["href", "title"]}


def render_markdown(text: str) -> str:
    html = markdown.markdown(text, extensions=["tables"])
    return bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS)


def read_report(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read()


@app.route("/")
def home():
    raw = read_report(os.path.join(REPORTS_DIR, "latest_report.md"))
    html = render_markdown(raw) if raw is not None else None
    return render_template("report.html", title="Latest report", html=html)


@app.route("/reports")
def reports_list():
    gameweeks = []
    if os.path.isdir(REPORTS_DIR):
        for fname in os.listdir(REPORTS_DIR):
            m = GW_REPORT_RE.match(fname)
            if m:
                gameweeks.append(int(m.group(1)))
    gameweeks.sort(reverse=True)
    return render_template("reports_list.html", gameweeks=gameweeks)


@app.route("/reports/<int:gw>")
def report_detail(gw):
    raw = read_report(os.path.join(REPORTS_DIR, f"gw{gw}_report.md"))
    if raw is None:
        abort(404)
    return render_template("report.html", title=f"Gameweek {gw} report", html=render_markdown(raw))


# --- /players: cached so browsing the table never spends Odds API quota or
# hammers the FPL API. No auth attempt either (see fetch_player_data docs). ---
PLAYERS_CACHE_TTL = 1800  # 30 minutes
_players_cache = {"data": None, "fetched_at": 0.0}


def get_cached_players() -> list[dict]:
    now = time.time()
    if _players_cache["data"] is None or (now - _players_cache["fetched_at"]) > PLAYERS_CACHE_TTL:
        data = fetch_player_data(config, include_odds=False, authenticate=False)
        team_names = {t["id"]: t["name"] for t in data["bootstrap"]["teams"]}
        players_list = []
        for p in data["players"].values():
            enriched = dict(p)
            enriched["team_name"] = team_names.get(p["team"], "?")
            players_list.append(enriched)
        players_list.sort(key=lambda p: p["score"], reverse=True)
        _players_cache["data"] = players_list
        _players_cache["fetched_at"] = now
    return _players_cache["data"]


@app.route("/players")
def players():
    return render_template("players.html", players=get_cached_players())


# --- /run: gated behind a passcode + hard cooldown + daily cap. This is
# deliberately not a real auth system — see the project plan for why. ---
RUN_COOLDOWN_SECONDS = 10 * 60
MAX_RUNS_PER_DAY = 5
_last_run_at: datetime | None = None
_run_timestamps_today: list[datetime] = []
# Now that gunicorn uses threads (not just one worker), guard the
# check-then-set below so two near-simultaneous requests can't both pass
# the cooldown/cap check before either one records its run.
_run_state_lock = threading.Lock()


def _runs_today_count(now: datetime) -> int:
    global _run_timestamps_today
    _run_timestamps_today = [t for t in _run_timestamps_today if t.date() == now.date()]
    return len(_run_timestamps_today)


@app.route("/run", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def run_analysis():
    global _last_run_at

    error = None
    report_html = None

    if request.method == "POST":
        submitted = request.form.get("passcode", "")
        expected = os.environ.get("RUN_ANALYSIS_PASSCODE")
        now = datetime.now(timezone.utc)

        if not expected:
            error = "RUN_ANALYSIS_PASSCODE isn't configured on the server."
        elif not hmac.compare_digest(submitted, expected):
            error = "Wrong passcode."
        else:
            # Claim a cooldown/cap slot under the lock (fast), then run the
            # slow Claude call outside it so unrelated requests aren't
            # blocked for the ~40s+ duration of a real run.
            with _run_state_lock:
                if _last_run_at and (now - _last_run_at).total_seconds() < RUN_COOLDOWN_SECONDS:
                    remaining = int(RUN_COOLDOWN_SECONDS - (now - _last_run_at).total_seconds())
                    error = f"Please wait {remaining // 60}m {remaining % 60}s before running again."
                elif _runs_today_count(now) >= MAX_RUNS_PER_DAY:
                    error = f"Daily limit of {MAX_RUNS_PER_DAY} manual runs reached — try again tomorrow."
                else:
                    _last_run_at = now
                    _run_timestamps_today.append(now)

            if error is None:
                try:
                    result = generate_report(config)
                    report_html = render_markdown(result.report_markdown)
                except Exception as e:
                    error = f"Analysis failed: {e}"

    return render_template(
        "run.html",
        error=error,
        report_html=report_html,
        last_run_at=_last_run_at,
    )


if __name__ == "__main__":
    app.run(debug=True, port=8000)
