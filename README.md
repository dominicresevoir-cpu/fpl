# FPL Mini-League Agent

A semi-automated Fantasy Premier League advisor: pulls live data, scores
players, flags transfer/captain/chip opportunities, then has Claude weigh the
trade-offs and write a plain-English recommendation. It never submits
anything on your behalf — you make the actual moves in the FPL app.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Then fill in `.env`:

- **FPL_TEAM_ID** — go to the FPL site, click "Points" on your team, the
  number in the URL (`.../entry/<this number>/event/...`) is your team ID.
- **ANTHROPIC_API_KEY** — from [console.anthropic.com](https://console.anthropic.com).
- **FPL_EMAIL / FPL_PASSWORD** (optional but recommended) — your normal FPL
  login. This unlocks accurate current-squad, bank, and free-transfer data
  before a gameweek deadline, which the public API doesn't expose. Without
  this, the tool falls back to last week's public picks plus whatever you
  type into `MANUAL_BANK` / `MANUAL_FREE_TRANSFERS`.
- **FPL_LEAGUE_ID** (optional) — go to your mini-league on the FPL site, the
  number in the URL is the league ID. Enables rival-aware recommendations
  (are you chasing or protecting a lead this week).

## Run it

```bash
python main.py
```

This prints a report to the console and saves it to `reports/gw<N>_report.md`.
Run it once a day or so as a gameweek deadline approaches — fixture
difficulty, form, and prices all shift, so the closer to the deadline, the
better the recommendation.

## Web app (app.py)

A small Flask site sits on top of the same pipeline (`core.py`, shared with
`main.py`), for browsing reports and player data without touching the CLI:

- `/` and `/reports` — latest and historical reports
- `/players` — sortable/filterable table of every player's score, price, and
  fixture difficulty (cached 30 min; deliberately skips the odds signal and
  skips the FPL login attempt, so browsing this page never spends Odds API
  quota or waits on FPL's currently-broken login)
- `/run` — triggers a fresh report live from the browser instead of waiting
  for the scheduled GitHub Actions run. Gated behind `RUN_ANALYSIS_PASSCODE`
  (generate one with `python -c "import secrets; print(secrets.token_urlsafe(18))"`
  and set it in `.env`) plus a 10-minute cooldown and a 5-per-day cap, since
  each run is a real Anthropic API call. **Results here are shown inline
  only — they aren't saved to `reports/`**; the scheduled weekly job is the
  only durable history.

Run it locally with:

```bash
gunicorn -w 1 --timeout 120 app:app
```

(`-w 1` because the cooldown/cap/cache above are plain in-process state —
they only work correctly with a single worker. `--timeout 120` because a
`/run` call — Claude with web search plus live FPL/Odds calls — routinely
takes longer than gunicorn's 30-second default.)

To deploy it, connect this repo to [Render](https://render.com) (free tier,
no build step needed beyond `pip install -r requirements.txt`) or a similar
host, using `gunicorn -w 1 --timeout 120 app:app` as the start command and
the same environment variables as the GitHub Actions secrets below, plus
`RUN_ANALYSIS_PASSCODE`.

## Automating the weekly run (GitHub Actions)

This is wired up in `.github/workflows/weekly-run.yml`: it runs `main.py` on a
schedule, commits the report to `reports/`, and emails you the result. It
never submits transfers — same read-only/recommend-only behaviour as running
it locally.

**One-time setup:**

1. Push this repo to GitHub (private repo recommended, since secrets live
   here even though GitHub encrypts them at rest).
2. Go to **Settings → Secrets and variables → Actions → New repository
   secret** and add each of these:

   | Secret | Value |
   |---|---|
   | `FPL_TEAM_ID` | your team ID |
   | `FPL_LEAGUE_ID` | your mini-league ID (optional) |
   | `FPL_EMAIL` | your FPL login email |
   | `FPL_PASSWORD` | your FPL login password |
   | `ANTHROPIC_API_KEY` | from console.anthropic.com |
   | `MANUAL_BANK` | e.g. `0.0` (fallback if login fails) |
   | `MANUAL_FREE_TRANSFERS` | e.g. `1` (fallback if login fails) |
   | `CHIPS_AVAILABLE` | e.g. `wildcard,freehit,bboost,3xc` |
   | `LOOKAHEAD_GAMEWEEKS` | e.g. `5` |
   | `MAIL_SERVER` | e.g. `smtp.gmail.com` |
   | `MAIL_PORT` | e.g. `465` |
   | `MAIL_USERNAME` | the sending email address |
   | `MAIL_PASSWORD` | an **app password**, not your real email password (Gmail: Google Account → Security → App passwords) |
   | `MAIL_TO` | where you want the report sent — can be the same address |

3. That's it — it'll fire Thursday and Saturday mornings (UTC), and you can
   also trigger it on demand from the **Actions** tab → *FPL weekly
   recommendation* → **Run workflow**, useful for double gameweeks or
   deadlines that fall outside that window.

**Why Thu + Sat rather than a single fixed time:** FPL deadlines move around
(early kickoffs, Boxing Day, rearranged fixtures), so one cron time will
occasionally miss the window entirely. Running twice covers the usual
Fri-evening / Sat-late-morning deadlines cheaply — worth glancing at the
deadline yourself for the handful of gameweeks that don't follow the normal
pattern, and using the manual trigger then.

**Note on the FPL login secrets:** storing your real FPL email/password as
repo secrets is the same trust model as storing them in a local `.env` —
GitHub encrypts them and they're never printed in logs, but it's worth using
a private repo and treating them like any other credential.

## How the scoring works (analysis.py)

Each player gets a score from FPL's own next-gameweek projection, recent
form, underlying ICT/xGI data, and upcoming fixture ease — see `WEIGHTS` at
the top of `analysis.py`. This is intentionally a simple, readable weighted
sum rather than a trained model, so you can see exactly why a player is
recommended and adjust the weights as the season gives you signal on what's
actually predictive.

**Fixture ease** doesn't just take FPL's own 1-5 difficulty label at face
value — it's blended with FPL's own team-strength ratings (attack/defence,
home/away, always available, no extra setup) and, if `ODDS_API_KEY` is set,
a bookmaker-implied win probability. The three sources can disagree — e.g. a
fixture FPL rates "3" against a team whose underlying strength or market
odds say otherwise — and blending them smooths out cases where FPL's
editorial label lags reality.

The final report also has web search available (via the Claude API) to
catch late injury/team news the static FPL data wouldn't reflect — used
sparingly, mainly to sanity-check a captain or transfer pick close to a
deadline.

## Known limitations

- **Free transfer tracking**: FPL's exact saved-transfer rules have changed
  between seasons. If you're not using the authenticated `my-team` endpoint,
  double-check `MANUAL_FREE_TRANSFERS` against the app each week.
- **Rival picks**: the public API only exposes a manager's picks *after*
  that gameweek's deadline has passed, so "what are my rivals doing this
  week" isn't knowable in advance — only how they've played historically
  (used here as a rough signal, e.g. who's been captaining well).
- **FPL login endpoint is unofficial** — it's the same one the website uses,
  and is the standard approach used by most open-source FPL tools, but FPL
  could change it without notice. If login stops working, the tool falls
  back to public-picks mode automatically.
