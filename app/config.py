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
