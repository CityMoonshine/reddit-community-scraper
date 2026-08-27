"""Runtime configuration. Everything here is env-driven so the same image
runs on a laptop and on the VPS with only compose environment differing.
"""

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

# Repo root when running from a checkout; harmless inside the container.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _list(name):
    """Comma-separated env var -> list of non-empty trimmed strings."""
    return [item.strip() for item in (os.getenv(name) or '').split(',') if item.strip()]


# The shared SQLite file. Both the api and worker containers mount the volume
# that holds it, which is why WAL mode in db.py is not optional.
DB_PATH = os.getenv('DB_PATH', str(BASE_DIR / 'data' / 'portal.db'))

SESSION_LIFETIME = timedelta(hours=_int('SESSION_HOURS', 8))
COOKIE_NAME = os.getenv('COOKIE_NAME', 'portal_session')

# Set true only when the API is served over HTTPS, otherwise the browser drops
# the cookie and every login silently fails.
COOKIE_SECURE = _bool('COOKIE_SECURE', False)

# 'browser' drives headed Chromium under Xvfb; 'api' uses Reddit's OAuth API.
SCRAPE_BACKEND = os.getenv('SCRAPE_BACKEND', 'browser')

SWEEP_INTERVAL_MINUTES = _int('SWEEP_INTERVAL_MINUTES', 60)

# Reddit blocks headless Chromium outright, so this stays false in normal use.
# Exposed anyway so you can reproduce the block page on demand.
PLAYWRIGHT_HEADLESS = _bool('PLAYWRIGHT_HEADLESS', False)

# How often the worker looks for a run queued by the API's "Run sweep now".
QUEUE_POLL_SECONDS = _int('QUEUE_POLL_SECONDS', 15)

# A run still 'running' after this is assumed dead and reaped.
STALE_RUN_MINUTES = _int('STALE_RUN_MINUTES', 45)

# Trust X-Forwarded-For. True in compose because nginx is the only ingress;
# never enable it when the API is reachable directly.
TRUST_PROXY_HEADERS = _bool('TRUST_PROXY_HEADERS', False)

# Reddit OAuth, only needed by the 'api' scrape backend.
REDDIT_CLIENT_ID = os.getenv('REDDIT_CLIENT_ID')
REDDIT_CLIENT_SECRET = os.getenv('REDDIT_CLIENT_SECRET')
REDDIT_USERNAME = os.getenv('REDDIT_USERNAME')
REDDIT_PASSWORD = os.getenv('REDDIT_PASSWORD')
REDDIT_USER_AGENT = os.getenv(
    'REDDIT_USER_AGENT', 'python:scraping-defense-lab:v0.2 (by /u/unknown)'
)

# --------------------------------------------------------------- webshare
# A transport toggle, deliberately independent of SCRAPE_BACKEND: 'how we
# fetch' (browser vs api) and 'where we exit' (direct vs Webshare) are
# separate axes, so all four combinations are reachable.
#
# Only the browser backend consumes this. The OAuth API path is sanctioned
# access over credentials - putting a proxy in front of it buys nothing and
# only adds a way for it to break.
WEBSHARE_ENABLED = _bool('WEBSHARE_ENABLED', False)

# Dashboard -> API -> Keys. Not the proxy password; this reads the proxy list.
WEBSHARE_API_TOKEN = os.getenv('WEBSHARE_API_TOKEN')

WEBSHARE_API_BASE = os.getenv('WEBSHARE_API_BASE', 'https://proxy.webshare.io/api/v2')

# 'direct' gives one host:port per proxy, which is what per-community rotation
# needs. 'backbone' routes everything through one gateway and picks the exit
# itself, which would make the pool below pointless.
WEBSHARE_LIST_MODE = os.getenv('WEBSHARE_LIST_MODE', 'direct')

# Optional ISO-3166 filter, e.g. 'US,GB'. Empty means whatever the plan has.
WEBSHARE_COUNTRIES = _list('WEBSHARE_COUNTRIES')

# Upper bound on how many proxies to pull. Webshare pages at 100.
WEBSHARE_MAX_PROXIES = _int('WEBSHARE_MAX_PROXIES', 100)

# How long a fetched list stays good. Webshare rotates the underlying IPs on
# their own schedule, so a worker running for days must re-read eventually.
WEBSHARE_LIST_TTL_MINUTES = _int('WEBSHARE_LIST_TTL_MINUTES', 60)

# How long an exit IP sits out after it earns a block page. The whole point of
# a pool is that a burned IP stops being handed out for a while.
WEBSHARE_COOLDOWN_MINUTES = _int('WEBSHARE_COOLDOWN_MINUTES', 30)

# Attempts per community before a block is treated as final. With a pool, one
# block means 'that exit IP is burned', not 'the sweep is over' - but it must
# still terminate, or a fully-burned pool would spin forever.
WEBSHARE_MAX_ATTEMPTS = _int('WEBSHARE_MAX_ATTEMPTS', 3)

# 'community' takes a fresh exit IP (and a fresh browser context, so a fresh
# cookie jar) per subreddit. 'sweep' keeps one for the whole run.
WEBSHARE_ROTATE = os.getenv('WEBSHARE_ROTATE', 'community')

# Answers with the caller's IP as plain text. Used by --check to prove that
# traffic actually leaves through the proxy rather than around it.
WEBSHARE_EGRESS_CHECK_URL = os.getenv(
    'WEBSHARE_EGRESS_CHECK_URL', 'https://ipv4.webshare.io/'
)

# ------------------------------------------------------------- ai scoring
# Rank posts with Claude against a rubric the operator edits on the dashboard.
# The rubric lives in the database (ScoringPrompts), not here - the whole point
# is that it changes without a redeploy. What lives here is how to call the API.
SCORING_ENABLED = _bool('SCORING_ENABLED', False)

ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')

SCORING_MODEL = os.getenv('SCORING_MODEL', 'claude-opus-5')

# Scoring is high-volume and each post is a small judgement, which is the shape
# that does not repay deep reasoning. Raise it if the rubric gets subtle.
SCORING_EFFORT = os.getenv('SCORING_EFFORT', 'low')

# Posts scored per pass. A sweep can discover hundreds; scoring them all in one
# go would make the run unbounded, so it drains over successive passes.
SCORING_BATCH_LIMIT = _int('SCORING_BATCH_LIMIT', 40)

# Thinking tokens count against this, so it is not as generous as it looks.
SCORING_MAX_TOKENS = _int('SCORING_MAX_TOKENS', 4000)

# Posts scored in parallel. The rubric prefix is cached, so concurrency mostly
# buys latency rather than cost.
SCORING_CONCURRENCY = _int('SCORING_CONCURRENCY', 4)

# How much of a self-post body to send. Long enough to judge, short enough that
# one rambling post does not dominate the bill.
SCORING_SELFTEXT_CHARS = _int('SCORING_SELFTEXT_CHARS', 6000)
