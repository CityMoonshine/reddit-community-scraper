"""Route browser sweeps through Webshare exit IPs.

Why this exists, in one sentence: the block page documented in browser.py is an
IP-reputation decision Reddit makes *before* it looks at the browser, so no
amount of making Chromium look more human clears it - only a different exit IP
does.

    "On a VPS this is almost always the datacenter IP being filtered rather
     than anything about the browser, and it will not clear on its own."
                                            - app/ingest/browser.py

This module is a transport, not a backend. It hands out proxy endpoints; it
does not know what a subreddit is. SCRAPE_BACKEND still chooses *how* we fetch
(browser vs OAuth); WEBSHARE_ENABLED chooses *where we exit from*. Only the
browser path consumes it - see the note in config.py.

How it gets its exit IPs
------------------------
Webshare's API lists the proxies on the plan, each as its own host:port with
its own credentials:

    GET /api/v2/proxy/list/?mode=direct   Authorization: Token <API key>

That is 'direct' mode, and the mode matters. In 'backbone' mode every request
goes to one gateway host and Webshare picks the exit itself, which would make
the pool below pointless - we could not tell one exit from another, so we could
not retire the one that just got blocked.

The list is cached for WEBSHARE_LIST_TTL_MINUTES. The worker is a long-lived
process and the plan's IPs do not change minute to minute, so re-reading the
list per sweep would be a request that buys nothing.

Cooldown is the part that earns the pool
----------------------------------------
A block page means *this exit IP is burned*, not *the sweep is over*. So the
caller penalises the endpoint, it sits out WEBSHARE_COOLDOWN_MINUTES, and the
next attempt comes from somewhere else. Bounded by WEBSHARE_MAX_ATTEMPTS, or a
fully-burned pool would spin forever.

Failing loudly
--------------
If WEBSHARE_ENABLED is true and the pool cannot be built, this raises rather
than quietly falling back to a direct connection. Silently going direct is the
worst outcome available: you believe you are proxied, the VPS IP is what
actually shows up at Reddit, and the symptom is indistinguishable from the
proxy simply not helping.

Standalone use:

    python -m app.ingest.webshare --list      # what the plan has
    python -m app.ingest.webshare --check     # prove traffic exits through it
"""

import argparse
import itertools
import sys
import time

import httpx

from app.config import (WEBSHARE_API_BASE, WEBSHARE_API_TOKEN,
                        WEBSHARE_COOLDOWN_MINUTES, WEBSHARE_COUNTRIES,
                        WEBSHARE_EGRESS_CHECK_URL, WEBSHARE_ENABLED,
                        WEBSHARE_LIST_MODE, WEBSHARE_LIST_TTL_MINUTES,
                        WEBSHARE_MAX_ATTEMPTS, WEBSHARE_MAX_PROXIES,
                        WEBSHARE_ROTATE)

# Webshare pages the proxy list at 100 items and ignores larger values.
PAGE_SIZE = 100

LIST_TIMEOUT = 30.0

# The egress check goes through a proxy that may be slow to hand-shake; this is
# deliberately looser than a normal API timeout.
CHECK_TIMEOUT = 30.0


class WebshareError(RuntimeError):
    """Anything that makes proxied scraping impossible."""


class WebshareAuthError(WebshareError):
    pass


class NoProxyAvailable(WebshareError):
    """Every proxy in the pool is cooling down."""


class ProxyEndpoint:
    """One Webshare exit, in the shapes the two HTTP stacks want."""

    def __init__(self, address, port, username, password,
                 country=None, city=None, proxy_id=None):
        self.address = address
        self.port = int(port)
        self.username = username
        self.password = password
        self.country = country
        self.city = city
        self.proxy_id = proxy_id

    @property
    def key(self):
        return f'{self.address}:{self.port}'

    @property
    def label(self):
        where = self.country or '??'
        if self.city:
            where = f'{where}/{self.city}'
        return f'{self.key} ({where})'

    def playwright_proxy(self):
        """The dict playwright's launch/new_context takes."""
        return {
            'server': f'http://{self.key}',
            'username': self.username,
            'password': self.password,
        }

    def httpx_url(self):
        """Credentials inline, which is the only form httpx accepts."""
        return f'http://{self.username}:{self.password}@{self.key}'

    def __repr__(self):
        return f'<ProxyEndpoint {self.label}>'


def _headers():
    if not WEBSHARE_API_TOKEN:
        raise WebshareAuthError(
            'WEBSHARE_ENABLED is on but WEBSHARE_API_TOKEN is empty. Get the '
            'token from the Webshare dashboard under API -> Keys (it is not '
            'the proxy password) and put it in .env.'
        )

    return {'Authorization': f'Token {WEBSHARE_API_TOKEN}'}


def fetch_proxy_list(limit=None, mode=None, countries=None):
    """Read the plan's proxies from Webshare. Returns [ProxyEndpoint].

    Walks pages until `limit` is satisfied or the list runs out.
    """
    limit = limit or WEBSHARE_MAX_PROXIES
    mode = mode or WEBSHARE_LIST_MODE
    countries = WEBSHARE_COUNTRIES if countries is None else countries

    endpoints = []
    page = 1

    with httpx.Client(timeout=LIST_TIMEOUT, headers=_headers()) as http:
        while len(endpoints) < limit:
            params = {
                'mode': mode,
                'page': page,
                'page_size': min(PAGE_SIZE, limit - len(endpoints)),
            }

            if countries:
                params['country_code__in'] = ','.join(countries)

            try:
                response = http.get(f'{WEBSHARE_API_BASE}/proxy/list/', params=params)
            except httpx.HTTPError as exc:
                raise WebshareError(
                    f'Could not reach the Webshare API ({exc}). Check outbound '
                    f'network from the worker container.'
                ) from exc

            if response.status_code in (401, 403):
                raise WebshareAuthError(
                    f'Webshare rejected the API token (HTTP '
                    f'{response.status_code}). Confirm WEBSHARE_API_TOKEN is '
                    f'the key from API -> Keys and has not been rotated.'
                )

            if response.status_code != 200:
                raise WebshareError(
                    f'Webshare proxy list returned HTTP {response.status_code}: '
                    f'{response.text[:200]}'
                )

            payload = response.json()
            results = payload.get('results') or []

            for item in results:
                # Webshare flags proxies it could not verify. Handing one out
                # spends an attempt on a connection that was never going to
                # work, and the failure looks like a Reddit block.
                if item.get('valid') is False:
                    continue

                address = item.get('proxy_address')
                port = item.get('port')

                if not address or not port:
                    continue

                endpoints.append(ProxyEndpoint(
                    address=address,
                    port=port,
                    username=item.get('username'),
                    password=item.get('password'),
                    country=item.get('country_code'),
                    city=item.get('city_name'),
                    proxy_id=item.get('id'),
                ))

            if not payload.get('next') or not results:
                break

            page += 1

    if not endpoints:
        detail = f' matching {",".join(countries)}' if countries else ''
        raise WebshareError(
            f'Webshare returned no usable proxies{detail}. Check the plan has '
            f'active proxies, and that WEBSHARE_COUNTRIES is not filtering '
            f'them all out.'
        )

    return endpoints[:limit]


class ProxyPool:
    """Round-robin over the plan's exits, with a cooldown on burned ones.

    Not thread-safe, and does not need to be: exactly one worker executes
    exactly one sweep at a time (see the claim/queue guard in worker/monitor).
    """

    def __init__(self, endpoints=None, ttl_minutes=None, cooldown_minutes=None):
        self._endpoints = list(endpoints) if endpoints else []
        self._ttl = (ttl_minutes if ttl_minutes is not None
                     else WEBSHARE_LIST_TTL_MINUTES) * 60
        self._cooldown = (cooldown_minutes if cooldown_minutes is not None
                          else WEBSHARE_COOLDOWN_MINUTES) * 60

        # Keyed on address:port rather than the endpoint object, so a refresh
        # that rebuilds the objects does not forget who was burned.
        self._cooling = {}

        self._fetched_at = time.monotonic() if self._endpoints else 0.0
        self._cursor = 0

    # ---------------------------------------------------------------- list

    def _stale(self):
        return not self._endpoints or (time.monotonic() - self._fetched_at) > self._ttl

    def refresh(self, force=False):
        if not force and not self._stale():
            return

        self._endpoints = fetch_proxy_list()
        self._fetched_at = time.monotonic()
        self._cursor = 0

        print(f'[webshare] {len(self._endpoints)} proxies available', flush=True)

    # -------------------------------------------------------------- rotate

    def _available(self):
        now = time.monotonic()
        return [e for e in self._endpoints if self._cooling.get(e.key, 0) <= now]

    def acquire(self):
        """The next usable exit. Raises NoProxyAvailable if all are cooling."""
        self.refresh()

        available = self._available()

        if not available:
            # Deliberately no forced re-read here. The exits are all cooling,
            # not missing - fetching the same list again would spend an API
            # call to learn nothing, and if the token has since been rotated it
            # would replace 'every exit is burned' with a credentials error,
            # which is the wrong diagnosis entirely.
            soonest = (min(self._cooling.values()) - time.monotonic()
                       if self._cooling else 0)

            raise NoProxyAvailable(
                f'All {len(self._endpoints)} Webshare exits are cooling down '
                f'after block pages; the first frees up in '
                f'{max(0, int(soonest))}s. Reddit is refusing this whole pool - '
                f'a larger plan, a different country mix, or SCRAPE_BACKEND=api '
                f'are the ways out.'
            )

        endpoint = available[self._cursor % len(available)]
        self._cursor += 1

        return endpoint

    def penalize(self, endpoint, reason=''):
        """Retire an exit that got blocked, for WEBSHARE_COOLDOWN_MINUTES."""
        if endpoint is None:
            return

        self._cooling[endpoint.key] = time.monotonic() + self._cooldown

        detail = f' ({reason})' if reason else ''
        print(f'[webshare] {endpoint.label} cooling down for '
              f'{int(self._cooldown / 60)}m{detail}', flush=True)

    def forgive(self, endpoint):
        """Clear a cooldown - an exit that just worked is not burned."""
        if endpoint is not None:
            self._cooling.pop(endpoint.key, None)

    # ------------------------------------------------------------ reporting

    def describe(self):
        available = len(self._available())
        return (f'{available}/{len(self._endpoints)} Webshare exits available, '
                f'rotating per {WEBSHARE_ROTATE}')


def pool(enabled=None):
    """The pool for this process, or None when the toggle is off.

    Returning None rather than a no-op pool is deliberate: callers then branch
    once, and the direct path stays byte-for-byte what it was before this
    module existed.
    """
    if not (WEBSHARE_ENABLED if enabled is None else enabled):
        return None

    built = ProxyPool()
    built.refresh(force=True)

    return built


def egress_ip(endpoint, url=None):
    """The IP the outside world sees for this endpoint. Raises on failure.

    Worth having as its own function: 'the proxy is configured' and 'traffic
    actually leaves through it' are different claims, and only this one is
    evidence.
    """
    url = url or WEBSHARE_EGRESS_CHECK_URL

    with httpx.Client(timeout=CHECK_TIMEOUT, proxy=endpoint.httpx_url()) as http:
        response = http.get(url)
        response.raise_for_status()
        return response.text.strip()[:64]


# ------------------------------------------------------------------ cli

def _cmd_list():
    endpoints = fetch_proxy_list()

    print(f'{len(endpoints)} proxies ({WEBSHARE_LIST_MODE} mode)\n', flush=True)

    for endpoint in endpoints:
        print(f'  {endpoint.label}', flush=True)

    return 0


def _cmd_check(limit):
    endpoints = fetch_proxy_list()[:limit]

    print(f'Checking egress through {len(endpoints)} proxies '
          f'against {WEBSHARE_EGRESS_CHECK_URL}\n', flush=True)

    try:
        direct = httpx.get(WEBSHARE_EGRESS_CHECK_URL, timeout=CHECK_TIMEOUT).text.strip()
        print(f'  direct (no proxy): {direct}\n', flush=True)
    except httpx.HTTPError as exc:
        direct = None
        print(f'  direct (no proxy): failed - {exc}\n', flush=True)

    failures = 0

    for endpoint in endpoints:
        try:
            seen = egress_ip(endpoint)
        except httpx.HTTPError as exc:
            failures += 1
            print(f'  {endpoint.label}: FAILED - {type(exc).__name__}: {exc}',
                  flush=True)
            continue

        # The failure that matters is not an error, it is a success that went
        # around the proxy - same IP as direct means nothing is being proxied.
        if direct and seen == direct:
            failures += 1
            print(f'  {endpoint.label}: LEAKING - egress is {seen}, same as '
                  f'direct', flush=True)
        else:
            print(f'  {endpoint.label}: ok, egress {seen}', flush=True)

    print('', flush=True)

    if failures:
        print(f'{failures}/{len(endpoints)} proxies unusable.', flush=True)
        return 1

    print(f'All {len(endpoints)} proxies route correctly.', flush=True)
    return 0


def _cmd_rotate(rounds):
    built = ProxyPool()
    built.refresh(force=True)

    print(f'{built.describe()}\n', flush=True)

    for index in itertools.islice(itertools.count(1), rounds):
        endpoint = built.acquire()
        print(f'  {index}. {endpoint.label}', flush=True)

    return 0


def main():
    parser = argparse.ArgumentParser(
        description='Inspect and verify the Webshare proxy pool.'
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--list', action='store_true',
                       help="Show the plan's proxies.")
    group.add_argument('--check', action='store_true',
                       help='Prove traffic actually exits through each proxy.')
    group.add_argument('--rotate', type=int, metavar='N',
                       help='Show which exit the pool would hand out N times.')
    parser.add_argument('--limit', type=int, default=5,
                        help='How many proxies --check should test (default 5).')
    args = parser.parse_args()

    try:
        if args.list:
            return _cmd_list()
        if args.check:
            return _cmd_check(args.limit)
        return _cmd_rotate(args.rotate)
    except WebshareError as exc:
        print(f'\n{exc}\n', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
