"""
Optional third-party odds signal (The Odds API) — a market-based cross-check
on fixture difficulty, independent of both FPL's own difficulty label and its
team-strength ratings. Only used when ODDS_API_KEY is set; any failure here
should be treated as non-fatal by callers, since this is a bonus signal, not
a required one.
"""

from __future__ import annotations

import requests

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# The Odds API returns full club names; FPL's bootstrap uses short display
# names. Covers the 2025/26 Premier League sides — add an entry here if a
# name stops matching after promotion/relegation.
TEAM_NAME_ALIASES = {
    "Arsenal": "Arsenal",
    "Aston Villa": "Aston Villa",
    "Bournemouth": "Bournemouth",
    "Brentford": "Brentford",
    "Brighton": "Brighton and Hove Albion",
    "Burnley": "Burnley",
    "Chelsea": "Chelsea",
    "Crystal Palace": "Crystal Palace",
    "Everton": "Everton",
    "Fulham": "Fulham",
    "Leeds": "Leeds United",
    "Liverpool": "Liverpool",
    "Man City": "Manchester City",
    "Man Utd": "Manchester United",
    "Newcastle": "Newcastle United",
    "Nott'm Forest": "Nottingham Forest",
    "Sunderland": "Sunderland",
    "Spurs": "Tottenham Hotspur",
    "West Ham": "West Ham United",
    "Wolves": "Wolverhampton Wanderers",
}


def _normalize(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def get_match_win_probabilities(api_key: str) -> dict:
    """Returns {(home_fpl_name, away_fpl_name): {"home": p, "draw": p, "away": p}}
    for upcoming EPL fixtures, derived from bookmaker h2h odds with the
    overround removed and averaged across bookmakers. Raises
    requests.RequestException on failure — callers should catch it."""
    resp = requests.get(
        f"{ODDS_API_BASE}/sports/soccer_epl/odds/",
        params={"apiKey": api_key, "regions": "uk", "markets": "h2h", "oddsFormat": "decimal"},
        timeout=10,
    )
    resp.raise_for_status()
    events = resp.json()

    alias_lookup = {_normalize(full): fpl_name for fpl_name, full in TEAM_NAME_ALIASES.items()}

    def to_fpl_name(odds_api_name: str) -> str | None:
        normalized = _normalize(odds_api_name)
        if normalized in alias_lookup:
            return alias_lookup[normalized]
        for norm_full, fpl_name in alias_lookup.items():
            if norm_full in normalized or normalized in norm_full:
                return fpl_name
        return None

    probabilities = {}
    for event in events:
        home_raw, away_raw = event.get("home_team", ""), event.get("away_team", "")
        home = to_fpl_name(home_raw)
        away = to_fpl_name(away_raw)
        if not home or not away:
            continue

        home_probs, draw_probs, away_probs = [], [], []
        for bookmaker in event.get("bookmakers", []):
            h2h = next((m for m in bookmaker.get("markets", []) if m["key"] == "h2h"), None)
            if not h2h:
                continue
            outcomes = {o["name"]: o["price"] for o in h2h["outcomes"]}
            if home_raw not in outcomes or away_raw not in outcomes or "Draw" not in outcomes:
                continue
            implied = {
                "home": 1 / outcomes[home_raw],
                "draw": 1 / outcomes["Draw"],
                "away": 1 / outcomes[away_raw],
            }
            overround = sum(implied.values())
            home_probs.append(implied["home"] / overround)
            draw_probs.append(implied["draw"] / overround)
            away_probs.append(implied["away"] / overround)

        if not home_probs:
            continue

        probabilities[(home, away)] = {
            "home": sum(home_probs) / len(home_probs),
            "draw": sum(draw_probs) / len(draw_probs),
            "away": sum(away_probs) / len(away_probs),
        }

    return probabilities
