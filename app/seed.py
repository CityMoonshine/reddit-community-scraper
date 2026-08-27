"""Demo accounts, and making sure no community is invisible to everyone.

Community and post data is never seeded - it comes from real Reddit via the
worker's sweeps.
"""

import hashlib
import random

from faker import Faker

from app.db import connection_scope

# Deterministic, so your scraper tests get stable accounts across rebuilds.
# (The Reddit data itself is live and will move - that's the point of it.)
fake = Faker()
Faker.seed(42)
random.seed(42)

DEFAULT_PASSWORD = b'password123'
USER_COUNT = 5
COMMUNITIES_PER_USER = 3


def hash_password(password):
    # Matches the API's verifier. Fine for a throwaway target; swap for
    # passlib/bcrypt if you ever want this to resemble real auth.
    if isinstance(password, str):
        password = password.encode()
    return hashlib.sha256(password).hexdigest()


def seed_users():
    with connection_scope() as connection:
        cursor = connection.cursor()

        if cursor.execute('SELECT COUNT(*) FROM Users;').fetchone()[0]:
            return

        rows = []
        for _ in range(USER_COUNT):
            username = fake.unique.user_name()
            agency = f"{fake.last_name()} {random.choice(['Social Labs', 'Media Insights', 'Signal Group'])}"
            rows.append((username, hash_password(DEFAULT_PASSWORD), agency))
            print(f'seeded user: {username} / password123', flush=True)

        cursor.executemany(
            'INSERT INTO Users (username, password_hash, agency_name) VALUES (?, ?, ?);',
            rows,
        )


def seed_watchlists(per_user=COMMUNITIES_PER_USER):
    """Make sure no community is invisible to everyone.

    Two modes, because the dashboard owns watchlists now and this must not
    fight it:

      - Empty Watchlist (fresh database): deal communities round-robin so the
        demo accounts each start with something to look at.
      - Otherwise: only adopt *orphans* - communities nobody watches, which is
        what a CLI ingest of a brand-new subreddit produces. Anything a user
        added through the dashboard is left exactly as they set it.
    """
    with connection_scope() as connection:
        cursor = connection.cursor()

        user_ids = [row[0] for row in cursor.execute('SELECT id FROM Users ORDER BY id;')]
        community_ids = [row[0] for row in cursor.execute('SELECT id FROM Communities ORDER BY id;')]

        if not user_ids or not community_ids:
            return

        bootstrapping = cursor.execute('SELECT COUNT(*) FROM Watchlist;').fetchone()[0] == 0

        if bootstrapping:
            rows = []
            for offset, user_id in enumerate(user_ids):
                rotated = community_ids[offset:] + community_ids[:offset]
                step = max(1, len(community_ids) // max(1, per_user))
                picks = (rotated[::step][:per_user]
                         if len(community_ids) > per_user else rotated)
                rows.extend((user_id, community_id) for community_id in picks)

            watched = {community_id for _, community_id in rows}
            rows.extend((user_ids[0], community_id)
                        for community_id in community_ids if community_id not in watched)
        else:
            orphans = [
                row[0] for row in cursor.execute(
                    '''
                    SELECT c.id FROM Communities c
                    WHERE NOT EXISTS (
                        SELECT 1 FROM Watchlist w WHERE w.community_id = c.id
                    );
                    '''
                )
            ]

            if not orphans:
                return

            rows = [(user_ids[0], community_id) for community_id in orphans]

        cursor.executemany(
            'INSERT OR IGNORE INTO Watchlist (user_id, community_id) VALUES (?, ?);',
            rows,
        )

        if cursor.rowcount > 0:
            print(f'watchlist: {cursor.rowcount} new pairing(s)', flush=True)
