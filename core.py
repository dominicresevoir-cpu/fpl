"""
Reusable pipeline logic shared by the CLI (main.py) and the web app (app.py).

Nothing here prints or writes to disk — callers (CLI or Flask routes) decide
how to surface warnings and whether/where to persist a report.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime

from fpl_client import FPLClient, current_and_next_event
from analysis import (
    fixture_difficulty_by_team,
    fixtures_per_team_per_event,
    build_player_index,
    suggest_transfers,
    suggest_captain,
    chip_recommendations,
    top_players_by_position,
    team_strength_by_id,
)
from claude_advisor import get_recommendation
from rivals import build_rival_report
from odds_client import get_match_win_probabilities


@dataclass
class FPLConfig:
    team_id: int
    league_id: str | None
    email: str | None
    password: str | None
    manual_bank: float
    manual_free_transfers: int
    chips_available: list[str]
    lookahead: int
    odds_api_key: str | None

    @classmethod
    def from_env(cls) -> "FPLConfig":
        return cls(
            team_id=int(os.environ["FPL_TEAM_ID"]),
            league_id=os.environ.get("FPL_LEAGUE_ID"),
            email=os.environ.get("FPL_EMAIL"),
            password=os.environ.get("FPL_PASSWORD"),
            manual_bank=float(os.environ.get("MANUAL_BANK", 0.0)),
            manual_free_transfers=int(os.environ.get("MANUAL_FREE_TRANSFERS", 1)),
            chips_available=[
                c.strip() for c in os.environ.get("CHIPS_AVAILABLE", "").split(",") if c.strip()
            ],
            lookahead=int(os.environ.get("LOOKAHEAD_GAMEWEEKS", 5)),
            odds_api_key=os.environ.get("ODDS_API_KEY"),
        )


@dataclass
class ReportResult:
    gameweek_id: int
    gameweek_name: str
    deadline: str
    squad_source: str
    report_markdown: str
    generated_at: datetime
    warnings: list[str] = field(default_factory=list)


def fetch_player_data(config: FPLConfig, include_odds: bool = True, authenticate: bool = False) -> dict:
    """The read-only half of the pipeline: bootstrap/fixtures/player scores,
    no squad lookup and no Claude call. Used by the CLI's full pipeline
    (authenticate=True, since get_current_squad needs a logged-in client for
    the live-squad path) and by the web app's /players page (defaults:
    include_odds=False so browsing the table never spends Odds API quota,
    authenticate=False so every page view doesn't also attempt — and wait
    out — FPL's currently-broken login)."""
    client = FPLClient(config.email, config.password) if authenticate else FPLClient()
    bootstrap = client.get_bootstrap()
    fixtures = client.get_fixtures()
    last_finished, next_event = current_and_next_event(bootstrap)

    lookahead_events = list(range(next_event["id"], next_event["id"] + config.lookahead))
    team_strength = team_strength_by_id(bootstrap)

    odds_probabilities = None
    warnings = []
    if include_odds and config.odds_api_key:
        try:
            odds_probabilities = get_match_win_probabilities(config.odds_api_key)
        except Exception as e:
            warnings.append(f"Couldn't fetch odds data ({e}) — continuing without it.")

    fixture_diff = fixture_difficulty_by_team(
        fixtures, lookahead_events, team_strength, odds_probabilities
    )
    fixture_counts = fixtures_per_team_per_event(fixtures)
    players = build_player_index(bootstrap, fixture_diff)

    return {
        "client": client,
        "bootstrap": bootstrap,
        "fixtures": fixtures,
        "last_finished": last_finished,
        "next_event": next_event,
        "lookahead_events": lookahead_events,
        "fixture_counts": fixture_counts,
        "players": players,
        "warnings": warnings,
    }


def get_current_squad(client: FPLClient, bootstrap: dict, config: FPLConfig, last_finished, next_event):
    """Returns (squad_ids, bank, free_transfers, source_note)."""
    my_team = client.get_my_team(config.team_id)
    if my_team:
        squad_ids = [p["element"] for p in my_team["picks"]]
        bank = my_team["transfers"]["bank"] / 10
        free_transfers = my_team["transfers"]["limit"] - my_team["transfers"]["made"] \
            if my_team["transfers"].get("limit") else config.manual_free_transfers
        return squad_ids, bank, free_transfers, "live (authenticated)"

    if last_finished:
        picks = client.get_public_picks(config.team_id, last_finished["id"])
        if picks:
            squad_ids = [p["element"] for p in picks["picks"]]
            return squad_ids, config.manual_bank, config.manual_free_transfers, (
                f"approximate — last public picks from GW{last_finished['id']}, "
                f"bank/FT are manual overrides from .env"
            )

    raise RuntimeError(
        "Could not determine your squad. Either set FPL_EMAIL/FPL_PASSWORD in "
        ".env for live data, or wait until after GW1's deadline for public picks."
    )


def generate_report(config: FPLConfig) -> ReportResult:
    """Full pipeline: player data -> squad detection -> transfer/captain/chip
    suggestions (or a league-wide watchlist if no squad) -> optional
    mini-league context -> Claude-written recommendation. No printing, no
    disk writes — see save_report() for persistence."""
    data = fetch_player_data(config, include_odds=True, authenticate=True)
    client = data["client"]
    bootstrap = data["bootstrap"]
    last_finished = data["last_finished"]
    next_event = data["next_event"]
    lookahead_events = data["lookahead_events"]
    fixture_counts = data["fixture_counts"]
    players = data["players"]
    warnings = list(data["warnings"])
    teams_by_player = {pid: p["team"] for pid, p in players.items()}

    try:
        squad_ids, bank, free_transfers, squad_source = get_current_squad(
            client, bootstrap, config, last_finished, next_event
        )
    except RuntimeError as e:
        warnings.append(
            f"{e} Continuing with public data only — no personalized "
            "transfer/captain/chip advice this run."
        )
        squad_ids = None
        squad_source = "unavailable — no squad data (pre-season or login unavailable)"

    rival_context = None
    rival_differential_report = None
    if config.league_id:
        try:
            standings = client.get_league_standings(int(config.league_id))
            results = standings["standings"]["results"]
            rival_context = [
                {"entry_name": r["entry_name"], "player_name": r["player_name"],
                 "total": r["total"], "rank": r["rank"]}
                for r in results[:10]
            ]
        except Exception as e:
            warnings.append(f"Couldn't fetch league standings: {e}")

        if squad_ids is not None:
            try:
                completed_events = [e["id"] for e in bootstrap["events"] if e["finished"]][-3:]
                rival_differential_report = build_rival_report(
                    client, int(config.league_id), config.team_id, players, completed_events, squad_ids
                )
            except Exception as e:
                warnings.append(f"Couldn't build rival differential report: {e}")

    if squad_ids is not None:
        transfer_ideas = suggest_transfers(squad_ids, players, bank, free_transfers)
        captain_ideas = suggest_captain(squad_ids, players)
        chip_notes = chip_recommendations(
            squad_ids, teams_by_player, fixture_counts, lookahead_events, config.chips_available
        )
        context = {
            "gameweek": next_event["name"],
            "deadline": next_event["deadline_time"],
            "squad_data_source": squad_source,
            "bank": bank,
            "free_transfers": free_transfers,
            "chips_available": config.chips_available,
            "current_squad": [
                {"name": players[pid]["name"], "position": players[pid]["position"],
                 "score": players[pid]["score"]}
                for pid in squad_ids
            ],
            "transfer_ideas": transfer_ideas,
            "captain_ideas": [
                {"name": c["name"], "score": c["score"]} for c in captain_ideas
            ],
            "chip_notes": chip_notes,
            "mini_league_standings": rival_context,
            "mini_league_differential_report": rival_differential_report,
        }
    else:
        context = {
            "gameweek": next_event["name"],
            "deadline": next_event["deadline_time"],
            "squad_data_source": squad_source,
            "note": "No squad data available, so this is league-wide analysis "
                    "only — not personalized transfer/captain/chip advice.",
            "top_players_by_position": top_players_by_position(players),
            "mini_league_standings": rival_context,
        }

    report_body = get_recommendation(context)

    generated_at = datetime.now()
    report_markdown = (
        f"# FPL Recommendation — {next_event['name']}\n\n"
        f"*Generated {generated_at.isoformat(timespec='minutes')} "
        f"| squad data: {squad_source}*\n\n"
        f"{report_body}\n"
    )

    return ReportResult(
        gameweek_id=next_event["id"],
        gameweek_name=next_event["name"],
        deadline=next_event["deadline_time"],
        squad_source=squad_source,
        report_markdown=report_markdown,
        generated_at=generated_at,
        warnings=warnings,
    )


def save_report(result: ReportResult, reports_dir: str = "reports") -> str:
    """Writes reports/gw<N>_report.md and reports/latest_report.md. Returns
    the dated file's path."""
    os.makedirs(reports_dir, exist_ok=True)
    out_path = os.path.join(reports_dir, f"gw{result.gameweek_id}_report.md")
    with open(out_path, "w") as f:
        f.write(result.report_markdown)
    with open(os.path.join(reports_dir, "latest_report.md"), "w") as f:
        f.write(result.report_markdown)
    return out_path
