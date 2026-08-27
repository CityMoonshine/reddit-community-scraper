# Scraping Defense

A lab target: a login-gated portal serving **real Reddit community data**, wired
with the instrumentation you'd use to detect someone scraping it.

The data being protected is genuine — subreddit metadata and posts pulled from
Reddit's OAuth API — so extraction attempts run against realistic shapes
(pagination, flairs, moving scores) rather than faker filler.

## Setup

```bash
cd backend
python createDb.py                    # schema + 5 seeded accounts
python -m playwright install chromium
```

## Ingest — browsing (default)

`browse_ingest.py` drives a real Chromium window: it opens the subreddit, waits
for the feed, scrolls, and reads the cards — the same path a person takes.

```bash
python browse_ingest.py --subreddits python,programming
python browse_ingest.py --subreddits python --sort top --time-filter week --limit 150
python browse_ingest.py --subreddits python --dry-run
```

No credentials needed. The window is visible on purpose — clicking around in it
mid-run will fight the scroller.

Two things about Reddit's current front end drove the implementation:

- **Every useful field is a DOM attribute** on `<shreddit-post>`: `id`,
  `post-title`, `author`, `score`, `comment-count`, `upvote-ratio`,
  `created-timestamp`, `domain`, `permalink`. No text parsing.
- **The feed is virtualised.** Cards are recycled out of the DOM as you scroll —
  a live count goes 27 → 49 → 25. Posts are harvested after *every* scroll and
  accumulated by id; reading the DOM once at the end loses most of them.

What the browser path can't get: **total subscriber count**. This layout renders
weekly actives and weekly contributions instead, and old.reddit (which does show
"N readers") answers with a login wall. `subscribers` stays null; `active_users`
is real. The API path below fills it in.

### What Reddit blocks

Worth knowing, since it's the subject of this repo:

| Approach | Result |
|---|---|
| `requests`/`httpx` on `www.reddit.com/r/x/hot.json` | `403` |
| `old.reddit.com/r/x/.json` | redirect to login wall |
| **Headless** Chromium on `reddit.com` | "You've been blocked by network security" |
| **Headed** Chromium | loads normally |

## Ingest — OAuth API (alternative)

`reddit_ingest.py` takes the sanctioned route. Same CLI, same tables, and it
does return real subscriber counts — but it needs credentials.

```bash
cp .env.example .env      # fill in client id + secret
python reddit_ingest.py --subreddits python,programming
```

Credentials come from https://www.reddit.com/prefs/apps → "create another app…"
→ type **script**. The client id is the short string under the app name.

The client paces at ~50 req/min (half the 100/min OAuth budget), reads
`x-ratelimit-remaining` off every response, and honours `Retry-After` on 429.

Either path is an upsert keyed on Reddit's post id: rows persist and their score
and comment count refresh, so repeat ingests show movement, not duplicates.

## Monitoring dashboard

Sign in and go to **`/monitor`**. That's where you add the communities to watch:

- **Add** a subreddit by name (`python`), prefixed (`r/python`), or by pasting a
  full URL — all three normalise to the same thing. Invalid names are rejected
  before they reach a URL or the database.
- Per community, choose which listing to watch (`new` by default — it's the one
  that surfaces new posts) and how many posts to pull per sweep (10–500).
- **Pause / Resume** a community without losing its history, or **Remove** it
  from your watchlist. Posts already collected stay either way.
- **Run sweep now** triggers one out of band, without waiting for the hour.

The page also shows the last 10 sweeps with their outcomes, and a
**Latest discoveries** table: the newest posts found, with author, score,
comments, flair, when they were posted and when the sweep first saw them.

Adding a community creates the row immediately but doesn't fetch anything — the
next sweep fills in metadata and posts. That keeps the dashboard responsive
instead of blocking an HTTP request on a multi-minute browser session.

## Hourly sweeps

```bash
python monitor.py --loop                 # hourly, on the hour, until Ctrl-C
python monitor.py --loop --interval 15   # every 15 minutes instead
python monitor.py --once                 # one sweep, then exit
python monitor.py --once --backend api   # OAuth instead of the browser
```

**Scheduling caveat, and it matters:** the browser backend drives a *headed*
Chromium, because Reddit serves headless Chromium a block page. A Windows Task
Scheduler job set to "run whether user is logged on or not" has no interactive
desktop, so the browser backend will fail there. Either:

- keep `monitor.py --loop` running in a logged-in session (simplest), or
- schedule `--backend api`, which needs `.env` credentials but runs truly
  headless and is the right choice for an unattended box.

Each sweep writes a `MonitorRuns` row plus one `MonitorRunItems` row per
community, so a subreddit that goes private shows up as a failed item rather
than vanishing into an aggregate count. Guards worth knowing about:

- **No overlapping sweeps.** A second run — from the scheduler or the dashboard
  button — is refused while one is live, so two Chromium windows never fight.
- **Stale runs are reaped.** A run still marked `running` after 45 minutes (the
  process was killed, the machine slept, Chromium hung) is marked failed on the
  next sweep, so the dashboard doesn't show a phantom run forever.
- **New posts are stamped.** `Posts.first_seen_at` and `first_seen_run_id` are
  written on insert only, so a post keeps the timestamp of the sweep that
  discovered it no matter how many later sweeps refresh its score.

## Run

```bash
uvicorn main:app --reload
```

Sign in at `/login` with any seeded username and `password123` (the usernames
print during `createDb.py`). Posts live at `/records`; the monitoring dashboard
is at `/monitor`.

## Schema

| Table | Role |
|---|---|
| `Communities` | one row per subreddit |
| `Posts` | the records a scraper wants; `post_id` is Reddit's own id |
| `Watchlist` | which accounts may see which communities — the auth gate |
| `MonitorRuns` | one row per sweep: trigger, backend, status, totals |
| `MonitorRunItems` | per-community outcome inside a sweep |
| `Users`, `Sessions` | login and cookie-backed sessions |
| `RequestLog` | every request: header order, `sec-fetch-mode`, `sec-ch-ua`, latency |
| `Fingerprints` | client telemetry (stage 3) |
| `BehaviorEvents` | pointer / scroll / click stream (stage 6) |
| `ScoreEvents` | itemised signal firings — answers "why was this blocked?" |

Posts are gated by watchlist rather than a column on the row, so "scrape
everything" means "acquire more accounts" — which is the pressure the detection
layer exists to measure.

## Debug views

- `/debug/sessions` — every session, newest first, with signal counts
- `/debug/sessions/{id}` — one session replayed: requests, signals and behaviour
  merged onto a single timeline with a running bot score

Both are unauthenticated by design and excluded from `RequestLog`.
