import random
import sqlite3
import hashlib
from pathlib import Path

from faker import Faker

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / 'portal.db')

# Deterministic seeds. Your scraper tests need stable accounts across runs,
# otherwise a regression looks identical to a reshuffle. (The Reddit data
# itself is live and will move between ingests - that's the point of it.)
fake = Faker()
Faker.seed(42)
random.seed(42)

# Every seeded account gets this. The portal is a lab target, not a service.
DEFAULT_PASSWORD = b'password123'

# How many communities each analyst account watches. Overlapping watchlists
# mean two accounts see different-but-intersecting slices of the same corpus.
COMMUNITIES_PER_USER = 3


def table_exists(table_name: str) -> bool:
    # Ask sqlite's own catalogue whether the table is there
    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?;",
            (table_name,),
        )

        return cursor.fetchone() is not None


def table_is_empty(table_name: str) -> bool:
    # Safer gate for seeding than 'was it just created'
    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        cursor.execute(f"SELECT COUNT(*) FROM {table_name};")
        count = cursor.fetchone()[0]

        return count == 0


def seed_users():
    if not table_is_empty('Users'):
        print("Table 'Users' already has data - skipping seed.")
        return

    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        rows = []
        for _ in range(5):
            username = fake.unique.user_name()
            print(f"Seeding user: {username} / password123")
            # Placeholder hashing - fine for a throwaway target, not for anything real
            password_hash = hashlib.sha256(DEFAULT_PASSWORD).hexdigest()
            agency_name = f"{fake.last_name()} {random.choice(['Social Labs', 'Media Insights', 'Signal Group'])}"
            rows.append((username, password_hash, agency_name))

        cursor.executemany(
            'INSERT INTO Users (username, password_hash, agency_name) VALUES (?, ?, ?);',
            rows,
        )

        connection.commit()

        print(f"Seeded {len(rows)} rows into 'Users'.")


def seed_watchlists(per_user=COMMUNITIES_PER_USER):
    """Make sure no community is invisible to everyone.

    Two modes, because the dashboard now owns watchlists and this must not
    fight it:

      - Empty Watchlist (fresh database): deal communities round-robin so the
        seeded demo accounts each start with something to look at.
      - Otherwise: only adopt *orphans* - communities nobody watches, which is
        what a CLI ingest of a brand-new subreddit produces. Anything a user
        added through /monitor is left exactly as they set it.
    """
    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        cursor.execute('SELECT id FROM Users ORDER BY id;')
        user_ids = [row[0] for row in cursor.fetchall()]

        cursor.execute('SELECT id FROM Communities ORDER BY id;')
        community_ids = [row[0] for row in cursor.fetchall()]

        if not user_ids:
            print("No users found - seed Users before watchlists.")
            return

        if not community_ids:
            print("No communities yet - run an ingest, then re-run this.")
            return

        cursor.execute('SELECT COUNT(*) FROM Watchlist;')
        bootstrapping = cursor.fetchone()[0] == 0

        if bootstrapping:
            # Deal communities round-robin from a per-user rotated order, so
            # the demo accounts see overlapping-but-different slices.
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
            cursor.execute(
                '''
                SELECT c.id FROM Communities c
                WHERE NOT EXISTS (SELECT 1 FROM Watchlist w WHERE w.community_id = c.id);
                '''
            )
            orphans = [row[0] for row in cursor.fetchall()]

            if not orphans:
                return

            rows = [(user_ids[0], community_id) for community_id in orphans]

        cursor.executemany(
            'INSERT OR IGNORE INTO Watchlist (user_id, community_id) VALUES (?, ?);',
            rows,
        )

        connection.commit()

        if cursor.rowcount > 0:
            print(f"Watchlist: {cursor.rowcount} new pairing(s).")
