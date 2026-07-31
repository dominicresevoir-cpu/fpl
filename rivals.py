"""
The mini-league differential tracker (see brief section 3).

Every player sits on two ownership axes: wider ownership (selected_by_percent,
already in the bootstrap data) and mini-league ownership (how many of YOUR
specific rivals hold him — not in the API, built here from their public picks).

Crossing those axes gives four categories:

                    high mini-league own.     low mini-league own.
high wider own.     pure template             hidden edge
low wider own.      false differential        double differential

Captaincy is the highest-leverage lens (a differential swings a few points a
season; a differential CAPTAIN doubles the swing in a single gameweek), so
this module builds and returns the captaincy view alongside the ownership one
so callers can lead with whichever they want.

Caveats, worth keeping visible downstream rather than silently assumed away:
- Rival picks are only public once a gameweek's deadline has passed, so this
  is a lag indicator — last known squad, not confirmed current-week.
- Squads are sticky (most managers make 0-1 transfers/week) so it's a strong
  proxy, not a guarantee for the upcoming gameweek.
- Small rival counts (a mini-league might be 6-10 people) are fine here —
  the goal is optimizing against those exact people, not statistical power.
"""

from __future__ import annotations

from collections import defaultdict


def get_rival_entry_ids(client, league_id: int, exclude_team_id: int | None = None) -> list[dict]:
    """Returns [{'entry': id, 'entry_name': ..., 'player_name': ...}, ...] for
    every manager in the mini-league except (optionally) the user themselves."""
    standings = client.get_league_standings(league_id)
    results = standings["standings"]["results"]
    rivals = [
        {"entry": r["entry"], "entry_name": r["entry_name"], "player_name": r["player_name"]}
        for r in results
        if exclude_team_id is None or r["entry"] != exclude_team_id
    ]
    return rivals


def pull_rival_picks(client, rivals: list[dict], completed_events: list[int]) -> dict[int, dict[int, dict]]:
    """Returns {event_id: {entry_id: picks_response}} for the given completed
    gameweeks. Skips a (rival, event) combo silently if picks aren't public
    (e.g. a rival who joined the league mid-season) rather than failing the
    whole run over one missing entry."""
    by_event = {}
    for event in completed_events:
        entry_picks = {}
        for rival in rivals:
            picks = client.get_public_picks(rival["entry"], event)
            if picks:
                entry_picks[rival["entry"]] = picks
        by_event[event] = entry_picks
    return by_event


def tally_ownership_and_captaincy(
    rival_picks_by_event: dict[int, dict[int, dict]],
    rivals: list[dict],
) -> tuple[dict[int, set[int]], dict[int, dict[int, int]]]:
    """
    Returns:
      current_owners: {player_id: {rival_entry_ids currently holding him, per
                        the most recent event we have picks for}}
      captain_counts: {player_id: {event_id: count of rivals who captained him}}

    Ownership uses only the MOST RECENT event with data per rival (their
    current squad as best known); captaincy is tallied per-event across all
    events pulled, since captain choice varies week to week and the history
    itself is the signal.
    """
    current_owners = defaultdict(set)
    captain_counts = defaultdict(lambda: defaultdict(int))

    if not rival_picks_by_event:
        return current_owners, captain_counts

    events_sorted = sorted(rival_picks_by_event.keys())
    latest_event = events_sorted[-1]

    # Ownership: latest known squad per rival, falling back to the most
    # recent earlier event if a rival's picks are missing for latest_event.
    for rival in rivals:
        entry_id = rival["entry"]
        for event in reversed(events_sorted):
            picks = rival_picks_by_event.get(event, {}).get(entry_id)
            if picks:
                for pick in picks["picks"]:
                    current_owners[pick["element"]].add(entry_id)
                break  # got this rival's most recent squad, move on

    # Captaincy: every event, every rival, whoever they captained.
    for event, entry_picks in rival_picks_by_event.items():
        for entry_id, picks in entry_picks.items():
            for pick in picks["picks"]:
                if pick.get("is_captain"):
                    captain_counts[pick["element"]][event] += 1

    return current_owners, captain_counts


def classify_quadrant(mini_league_owned: int, num_rivals: int, wider_pct: float,
                       wider_threshold: float = 15.0) -> str:
    """wider_threshold: selected_by_percent cutoff for 'high wider ownership'.
    15% is a reasonable pre-season default — tune once real season data exists."""
    high_mini = num_rivals > 0 and (mini_league_owned / num_rivals) >= 0.5
    high_wider = wider_pct >= wider_threshold

    if high_wider and high_mini:
        return "pure template"
    if high_wider and not high_mini:
        return "hidden edge"
    if not high_wider and high_mini:
        return "false differential"
    return "double differential"


def build_differential_table(
    players: dict[int, dict],
    current_owners: dict[int, set[int]],
    captain_counts: dict[int, dict[int, int]],
    num_rivals: int,
    squad_ids: list[int] | None = None,
) -> list[dict]:
    """One row per player who is either in your squad or held/captained by at
    least one rival — that's the set worth showing, not all 600+ players."""
    relevant_ids = set(current_owners.keys()) | set(captain_counts.keys())
    if squad_ids:
        relevant_ids |= set(squad_ids)

    rows = []
    for pid in relevant_ids:
        p = players.get(pid)
        if not p:
            continue
        mini_owned = len(current_owners.get(pid, ()))
        total_captain_mentions = sum(captain_counts.get(pid, {}).values())
        quadrant = classify_quadrant(mini_owned, num_rivals, p["selected_by_percent"])

        rows.append({
            "name": p["name"],
            "position": p["position"],
            "in_your_squad": squad_ids is not None and pid in squad_ids,
            "mini_league_owners": mini_owned,
            "mini_league_owners_pct": round(100 * mini_owned / num_rivals, 0) if num_rivals else 0,
            "wider_ownership_pct": p["selected_by_percent"],
            "quadrant": quadrant,
            "times_captained_by_rivals_recently": total_captain_mentions,
            "score": p["score"],
        })

    # Surface the highest-leverage rows first: recent rival captaincy, then
    # hidden-edge/double-differential picks with a strong underlying score.
    rows.sort(key=lambda r: (
        -r["times_captained_by_rivals_recently"],
        r["quadrant"] not in ("hidden edge", "double differential"),
        -r["score"],
    ))
    return rows


def build_rival_report(
    client,
    league_id: int,
    user_team_id: int,
    players: dict[int, dict],
    completed_events: list[int],
    squad_ids: list[int] | None = None,
) -> dict:
    """Top-level entry point: does the full pull + tally + classify pipeline
    and returns a dict ready to drop into the Claude advisor context.
    Returns {'note': ...} instead if there aren't enough completed gameweeks
    yet to have any public rival picks (e.g. pre-season / GW1)."""
    if not completed_events:
        return {
            "note": "No completed gameweeks yet — rival picks aren't public "
                     "pre-deadline, so the mini-league differential view will "
                     "populate from GW1 onward."
        }

    rivals = get_rival_entry_ids(client, league_id, exclude_team_id=user_team_id)
    if not rivals:
        return {"note": "Could not find any rivals in this mini-league."}

    rival_picks_by_event = pull_rival_picks(client, rivals, completed_events)
    current_owners, captain_counts = tally_ownership_and_captaincy(rival_picks_by_event, rivals)
    table = build_differential_table(players, current_owners, captain_counts, len(rivals), squad_ids)

    return {
        "num_rivals": len(rivals),
        "rivals": [r["entry_name"] for r in rivals],
        "events_analyzed": completed_events,
        "caveat": "Lag indicator — last known rival squads, not confirmed for "
                  "the upcoming gameweek. Small rival count is expected and fine.",
        "top_captaincy_signals": [
            r for r in table if r["times_captained_by_rivals_recently"] > 0
        ][:8],
        "hidden_edges_and_double_differentials": [
            r for r in table
            if r["quadrant"] in ("hidden edge", "double differential") and r["in_your_squad"] is False
        ][:8],
        "your_squad_quadrants": [r for r in table if r["in_your_squad"]],
    }
