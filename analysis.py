"""
Turns raw FPL data into an expected-points-style score per player, then uses
that score to suggest transfers, captaincy, and chip timing.

This is deliberately simple and transparent (not a black-box ML model) so you
can see exactly why a player is recommended and tune the weights yourself.
"""

from __future__ import annotations

from collections import defaultdict

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# Weights are a starting point — tune based on what you find predictive.
WEIGHTS = {
    "form": 0.35,          # recent form (last few GWs)
    "ep_next": 0.35,       # FPL's own single-GW projection
    "ict": 0.10,           # underlying involvement (influence/creativity/threat)
    "xgi": 0.10,           # expected goal involvement per 90 (attackers/mids)
    "fixture_ease": 0.10,  # inverse of upcoming fixture difficulty
}


def team_strength_by_id(bootstrap: dict) -> dict[int, dict]:
    """FPL's own per-team strength ratings (attack/defence, home/away) —
    a second, independent signal for fixture difficulty alongside FPL's
    editorial 1-5 difficulty label."""
    return {t["id"]: t for t in bootstrap["teams"]}


def fixture_difficulty_by_team(
    fixtures: list,
    lookahead_events: list[int],
    teams: dict[int, dict] | None = None,
    odds_probabilities: dict | None = None,
) -> dict[int, float]:
    """Average fixture difficulty (1=easiest, 5=hardest) per team over the
    given list of upcoming gameweek IDs. Handles double/blank gameweeks
    naturally since a team can appear 0, 1, or 2+ times per event.

    Blends up to three independent signals when available, rather than
    trusting FPL's own editorial label at face value: FPL's 1-5 difficulty
    rating, a score derived from FPL's own team-strength ratings (`teams`),
    and a score derived from bookmaker win probabilities
    (`odds_probabilities`, from odds_client.get_match_win_probabilities)."""
    diffs = defaultdict(list)
    for f in fixtures:
        if f["event"] not in lookahead_events:
            continue
        diffs[f["team_h"]].append((f["team_h_difficulty"], f["team_a"], "home"))
        diffs[f["team_a"]].append((f["team_a_difficulty"], f["team_h"], "away"))

    if not teams:
        return {
            team: (sum(d for d, _, _ in entries) / len(entries) if entries else 3.0)
            for team, entries in diffs.items()
        }

    overall_values = [
        t[f"strength_overall_{venue}"] for t in teams.values() for venue in ("home", "away")
    ]
    lo, hi = min(overall_values), max(overall_values)
    span = (hi - lo) or 1

    def opponent_strength_score(opponent_id: int, opponent_venue: str) -> float:
        raw = teams[opponent_id][f"strength_overall_{opponent_venue}"]
        return 1 + 4 * (raw - lo) / span  # normalized to 1 (easiest) - 5 (hardest)

    def odds_score(team_id: int, own_venue: str, opponent_id: int) -> float | None:
        if not odds_probabilities:
            return None
        home_id, away_id = (team_id, opponent_id) if own_venue == "home" else (opponent_id, team_id)
        probs = odds_probabilities.get((teams[home_id]["name"], teams[away_id]["name"]))
        if not probs:
            return None
        own_win_prob = probs["home"] if own_venue == "home" else probs["away"]
        return 1 + 4 * (1 - own_win_prob)  # normalized to 1 (easiest) - 5 (hardest)

    result = {}
    for team, entries in diffs.items():
        if not entries:
            result[team] = 3.0
            continue
        blended = []
        for fpl_difficulty, opponent_id, own_venue in entries:
            opponent_venue = "away" if own_venue == "home" else "home"
            scores = [fpl_difficulty, opponent_strength_score(opponent_id, opponent_venue)]
            market_score = odds_score(team, own_venue, opponent_id)
            if market_score is not None:
                scores.append(market_score)
            blended.append(sum(scores) / len(scores))
        result[team] = sum(blended) / len(blended)
    return result


def fixtures_per_team_per_event(fixtures: list) -> dict[tuple[int, int], int]:
    """Count of fixtures for (team_id, event_id) — >1 means double gameweek,
    0 means blank gameweek for that team in that event."""
    counts = defaultdict(int)
    for f in fixtures:
        if f["event"] is None:
            continue
        counts[(f["team_h"], f["event"])] += 1
        counts[(f["team_a"], f["event"])] += 1
    return counts


def build_player_index(bootstrap: dict, fixture_diff: dict[int, float]) -> dict[int, dict]:
    players = {}
    for p in bootstrap["elements"]:
        try:
            form = float(p["form"] or 0)
            ep_next = float(p["ep_next"] or 0)
            ict = float(p["ict_index"] or 0)
            xgi90 = float(p["expected_goal_involvements_per_90"] or 0)
        except (TypeError, ValueError):
            form = ep_next = ict = xgi90 = 0.0

        team_diff = fixture_diff.get(p["team"], 3.0)
        fixture_ease = (5 - team_diff) / 4  # normalize to ~0-1, higher = easier

        score = (
            WEIGHTS["form"] * form
            + WEIGHTS["ep_next"] * ep_next
            + WEIGHTS["ict"] * (ict / 10)     # ict_index runs much higher scale
            + WEIGHTS["xgi"] * (xgi90 * 10)   # per-90 xGI is a small decimal
            + WEIGHTS["fixture_ease"] * fixture_ease * 5
        )

        players[p["id"]] = {
            "id": p["id"],
            "name": p["web_name"],
            "team": p["team"],
            "position": POSITIONS.get(p["element_type"], "?"),
            "now_cost": p["now_cost"] / 10,  # FPL stores price *10
            "selected_by_percent": float(p["selected_by_percent"] or 0),
            "chance_of_playing_next_round": p["chance_of_playing_next_round"],
            "news": p["news"],
            "score": round(score, 2),
            "form": form,
            "ep_next": ep_next,
        }
    return players


def suggest_transfers(
    squad_ids: list[int],
    players: dict[int, dict],
    bank: float,
    free_transfers: int,
    top_n: int = 5,
) -> list[dict]:
    """For each squad player, find same-position replacements that are a
    clear upgrade in score within budget. Flags injury/suspension risk
    (chance_of_playing_next_round) as a priority signal regardless of score."""
    suggestions = []
    squad_set = set(squad_ids)

    for pid in squad_ids:
        p = players[pid]
        budget_for_swap = p["now_cost"] + bank

        candidates = [
            c for c in players.values()
            if c["position"] == p["position"]
            and c["id"] not in squad_set
            and c["now_cost"] <= budget_for_swap
        ]
        candidates.sort(key=lambda c: c["score"], reverse=True)

        injury_flag = (
            p["chance_of_playing_next_round"] is not None
            and p["chance_of_playing_next_round"] < 75
        )

        if candidates:
            best = candidates[0]
            score_gain = best["score"] - p["score"]
            if score_gain > 1.0 or injury_flag:
                suggestions.append({
                    "out": p["name"],
                    "out_reason": p["news"] if injury_flag else f"score {p['score']}",
                    "in": best["name"],
                    "in_score": best["score"],
                    "score_gain": round(score_gain, 2),
                    "cost_delta": round(best["now_cost"] - p["now_cost"], 1),
                    "urgent": injury_flag,
                })

    suggestions.sort(key=lambda s: (not s["urgent"], -s["score_gain"]))

    hit_note = (
        f"You have {free_transfers} free transfer(s). "
        f"Anything beyond that costs -4 pts."
    )
    for s in suggestions[:top_n]:
        s["hit_note"] = hit_note
    return suggestions[:top_n]


def top_players_by_position(players: dict[int, dict], top_n: int = 5) -> dict[str, list[dict]]:
    """League-wide top scorers per position — used when no squad data is
    available to fall back on (e.g. pre-season, before GW1's deadline)."""
    by_position = defaultdict(list)
    for p in players.values():
        by_position[p["position"]].append(p)
    return {
        pos: [
            {"name": p["name"], "score": p["score"], "now_cost": p["now_cost"]}
            for p in sorted(ranked, key=lambda p: p["score"], reverse=True)[:top_n]
        ]
        for pos, ranked in by_position.items()
    }


def suggest_captain(squad_ids: list[int], players: dict[int, dict]) -> list[dict]:
    ranked = sorted(
        (players[pid] for pid in squad_ids),
        key=lambda p: p["score"],
        reverse=True,
    )
    return ranked[:3]  # top 3, in case #1 is an injury doubt


def chip_recommendations(
    squad_ids: list[int],
    teams_by_player: dict[int, int],
    fixture_counts: dict[tuple[int, int], int],
    next_events: list[int],
    chips_available: list[str],
) -> list[str]:
    notes = []
    for event in next_events:
        squad_teams = {teams_by_player[pid] for pid in squad_ids}
        doubles = [t for t in squad_teams if fixture_counts.get((t, event), 1) >= 2]
        blanks = [t for t in squad_teams if fixture_counts.get((t, event), 1) == 0]

        if doubles and "bboost" in chips_available:
            notes.append(
                f"GW{event}: {len(doubles)} of your squad's teams have a double "
                f"gameweek — a candidate week for Bench Boost."
            )
        if doubles and "3xc" in chips_available:
            notes.append(
                f"GW{event}: double gameweek teams present — consider Triple "
                f"Captain if your top scorer's team is among them."
            )
        if len(blanks) >= 4 and "freehit" in chips_available:
            notes.append(
                f"GW{event}: {len(blanks)} of your squad's teams have no fixture "
                f"(blank gameweek) — a candidate week for Free Hit."
            )
    return notes
