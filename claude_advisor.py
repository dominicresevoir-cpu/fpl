"""
The analysis.py module produces numbers; this module asks Claude to weigh the
trade-offs a rules engine is bad at — e.g. "is a -4 hit worth it this week
given my rivals' likely picks and the fixture swing two weeks out?"
"""

import os
import json
import anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """You are a sharp, concise Fantasy Premier League strategy advisor.
You are advising a manager whose actual goal is to beat a small mini-league,
not to maximize overall rank. That means:
- If they're behind their rivals, favor differentials over safe/template picks.
- If they're ahead, favor protecting the lead with template/safe picks.
- Always weigh the -4 point hit cost explicitly against expected score gain.
- If mini_league_differential_report is present, treat rival captaincy history
  as the single highest-leverage signal in the whole dataset — a differential
  captain doubles the swing on one player in one gameweek, more than any
  transfer will. Call out by name any player rivals have repeatedly
  captained that the manager doesn't own or isn't considering as captain.
  Also surface "hidden edge" picks (safe, proven, but low mini-league
  ownership) and "double differential" picks (low ownership everywhere),
  and say which fits given whether the manager is ahead or behind. Remember
  this data is a lag indicator (last known rival squads, not this week's),
  so state conclusions with that caveat rather than certainty.
- Be direct and specific: name the players, name the gameweeks, give a
  recommendation, not just a list of options.
- Keep the whole report under 400 words. No preamble, no disclaimers.
- If there is no current_squad data (see the "note" field), skip the
  transfer/captain/chip sections entirely and instead give a league-wide
  watchlist: standout picks per position from top_players_by_position and
  any notable fixture swings, framed as "players to consider" rather than
  personalized advice.
- You have web search available. Use it sparingly and only for things the
  data below can't tell you — late injury/suspension news, press-conference
  team news, or a lineup doubt on a player you're about to recommend as a
  captain or transfer target. Don't search for anything already present in
  the data (scores, fixtures, form).
"""


def get_recommendation(context: dict) -> str:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    user_prompt = f"""Here is this week's data. Give me your recommendation.

{json.dumps(context, indent=2, default=str)}

Structure your answer as:
1. Captain (and vice) pick, one line of reasoning
2. Transfer verdict — make the move(s) or hold, and whether any hit is worth it
3. Chip watch — only if something in the data flags a window
4. Mini-league edge — only if mini_league_differential_report is present: the
   captaincy signal first, then any hidden-edge/double-differential pick worth
   taking given the manager's position in the standings
5. One sentence on where this leaves you relative to your mini-league rivals, if rival data is present
"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")
