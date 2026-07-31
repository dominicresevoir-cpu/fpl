"""
Run this before each gameweek deadline:

    python main.py

Reads config from .env (copy .env.example -> .env and fill it in).
Writes reports/gw<N>_report.md and prints it to the console.
"""

import os
from datetime import datetime
from dotenv import load_dotenv

from fpl_client import FPLClient, current_and_next_event, deadline_countdown
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

load_dotenv()

TEAM_ID = int(os.environ["FPL_TEAM_ID"])
LEAGUE_ID = os.environ.get("FPL_LEAGUE_ID")
EMAIL = os.environ.get("FPL_EMAIL")
PASSWORD = os.environ.get("FPL_PASSWORD")
# Manual fallback if not logged in — update these yourself each week.
MANUAL_BANK = float(os.environ.get("MANUAL_BANK", 0.0))
MANUAL_FREE_TRANSFERS = int(os.environ.get("MANUAL_FREE_TRANSFERS", 1))
CHIPS_AVAILABLE = [c.strip() for c in os.environ.get("CHIPS_AVAILABLE", "").split(",") if c.strip()]
LOOKAHEAD = int(os.environ.get("LOOKAHEAD_GAMEWEEKS", 5))
# Optional — enables a bookmaker-odds cross-check on fixture difficulty.
# Get a free-tier key at https://the-odds-api.com
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")


def get_current_squad(client: FPLClient, bootstrap: dict, last_finished, next_event):
    """Returns (squad_ids, bank, free_transfers, source_note)."""
    my_team = client.get_my_team(TEAM_ID)
    if my_team:
        squad_ids = [p["element"] for p in my_team["picks"]]
        bank = my_team["transfers"]["bank"] / 10
        free_transfers = my_team["transfers"]["limit"] - my_team["transfers"]["made"] \
            if my_team["transfers"].get("limit") else MANUAL_FREE_TRANSFERS
        return squad_ids, bank, free_transfers, "live (authenticated)"

    if last_finished:
        picks = client.get_public_picks(TEAM_ID, last_finished["id"])
        if picks:
            squad_ids = [p["element"] for p in picks["picks"]]
            return squad_ids, MANUAL_BANK, MANUAL_FREE_TRANSFERS, (
                f"approximate — last public picks from GW{last_finished['id']}, "
                f"bank/FT are manual overrides from .env"
            )

    raise RuntimeError(
        "Could not determine your squad. Either set FPL_EMAIL/FPL_PASSWORD in "
        ".env for live data, or wait until after GW1's deadline for public picks."
    )


def main():
    client = FPLClient(EMAIL, PASSWORD)
    bootstrap = client.get_bootstrap()
    last_finished, next_event = current_and_next_event(bootstrap)
    fixtures = client.get_fixtures()

    print(f"Next gameweek: {next_event['name']} — deadline in {deadline_countdown(next_event)}")

    lookahead_events = list(range(next_event["id"], next_event["id"] + LOOKAHEAD))
    team_strength = team_strength_by_id(bootstrap)
    odds_probabilities = None
    if ODDS_API_KEY:
        try:
            odds_probabilities = get_match_win_probabilities(ODDS_API_KEY)
        except Exception as e:
            print(f"⚠️  Couldn't fetch odds data ({e}) — continuing without it.")
    fixture_diff = fixture_difficulty_by_team(
        fixtures, lookahead_events, team_strength, odds_probabilities
    )
    fixture_counts = fixtures_per_team_per_event(fixtures)
    players = build_player_index(bootstrap, fixture_diff)
    teams_by_player = {pid: p["team"] for pid, p in players.items()}

    try:
        squad_ids, bank, free_transfers, squad_source = get_current_squad(
            client, bootstrap, last_finished, next_event
        )
    except RuntimeError as e:
        print(f"⚠️  {e}\n    Continuing with public data only — no personalized "
              "transfer/captain/chip advice this run.")
        squad_ids = None
        squad_source = "unavailable — no squad data (pre-season or login unavailable)"

    rival_context = None
    rival_differential_report = None
    if LEAGUE_ID:
        try:
            standings = client.get_league_standings(int(LEAGUE_ID))
            results = standings["standings"]["results"]
            rival_context = [
                {"entry_name": r["entry_name"], "player_name": r["player_name"],
                 "total": r["total"], "rank": r["rank"]}
                for r in results[:10]
            ]
        except Exception as e:
            print(f"⚠️  Couldn't fetch league standings: {e}")

        if squad_ids is not None:
            try:
                # Last up to 3 COMPLETED gameweeks — rival picks only go public
                # once a gameweek's deadline has passed.
                completed_events = [e["id"] for e in bootstrap["events"] if e["finished"]][-3:]
                rival_differential_report = build_rival_report(
                    client, int(LEAGUE_ID), TEAM_ID, players, completed_events, squad_ids
                )
            except Exception as e:
                print(f"⚠️  Couldn't build rival differential report: {e}")

    if squad_ids is not None:
        transfer_ideas = suggest_transfers(squad_ids, players, bank, free_transfers)
        captain_ideas = suggest_captain(squad_ids, players)
        chip_notes = chip_recommendations(
            squad_ids, teams_by_player, fixture_counts, lookahead_events, CHIPS_AVAILABLE
        )
        context = {
            "gameweek": next_event["name"],
            "deadline": next_event["deadline_time"],
            "squad_data_source": squad_source,
            "bank": bank,
            "free_transfers": free_transfers,
            "chips_available": CHIPS_AVAILABLE,
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

    report = (
        f"# FPL Recommendation — {next_event['name']}\n\n"
        f"*Generated {datetime.now().isoformat(timespec='minutes')} "
        f"| squad data: {squad_source}*\n\n"
        f"{report_body}\n"
    )

    os.makedirs("reports", exist_ok=True)
    out_path = f"reports/gw{next_event['id']}_report.md"
    with open(out_path, "w") as f:
        f.write(report)
    # Stable filename alongside the dated one, so CI/email steps don't need
    # to know the current gameweek number.
    with open("reports/latest_report.md", "w") as f:
        f.write(report)

    print("\n" + report)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
