import sqlite3
import sys
from pathlib import Path

from seeder import seed_users, seed_watchlists, table_exists

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / 'portal.db')


def create_tables():
    create_users_table()
    create_communities_table()
    create_posts_table()
    create_watchlist_table()
    create_sessions_table()
    create_request_log_table()
    create_fingerprints_table()
    create_behavior_events_table()
    create_score_events_table()
    create_monitor_runs_table()
    create_monitor_run_items_table()
    migrate()
    drop_legacy_policies()
    seed_users()
    # Communities and Posts are NOT seeded here - they come from real Reddit
    # data. Run:  python reddit_ingest.py --subreddits python,dataisbeautiful
    seed_watchlists()


def create_users_table():
    # Use 'with' to wrap the work in a transaction
    with sqlite3.connect(DB_PATH) as connection:

        # Create a cursor object
        cursor = connection.cursor()

        # Write the SQL command to create the Users table
        create_table_query = '''
        CREATE TABLE IF NOT EXISTS Users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            agency_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        '''

        # Execute the SQL command
        cursor.execute(create_table_query)

        # Commit the changes
        connection.commit()


def create_communities_table():
    # One row per subreddit, populated from /r/{name}/about via reddit_ingest.py.
    # 'name' is the bare slug ('python'), not 'r/python' - it's the join key.
    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        create_table_query = '''
        CREATE TABLE IF NOT EXISTS Communities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT,
            title TEXT,
            public_description TEXT,
            subscribers INTEGER,
            active_users INTEGER,
            subreddit_type TEXT,
            over18 INTEGER DEFAULT 0,
            created_utc TEXT,
            url TEXT,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        '''

        cursor.execute(create_table_query)
        connection.commit()


def create_posts_table():
    # The records a scraper will eventually try to extract. post_id is Reddit's
    # own base36 id (the 't3_' prefix stripped), which makes re-ingest an upsert
    # rather than a duplicate - scores and comment counts move, ids don't.
    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        create_table_query = '''
        CREATE TABLE IF NOT EXISTS Posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL UNIQUE,
            community_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            author TEXT,
            permalink TEXT,
            url TEXT,
            domain TEXT,
            flair TEXT,
            score INTEGER,
            upvote_ratio REAL,
            num_comments INTEGER,
            over18 INTEGER DEFAULT 0,
            is_self INTEGER DEFAULT 0,
            stickied INTEGER DEFAULT 0,
            selftext TEXT,
            created_utc TEXT,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (community_id) REFERENCES Communities (id)
        );
        '''

        cursor.execute(create_table_query)

        # /records filters on community then orders by score, so index that pair.
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_posts_community_score '
            'ON Posts (community_id, score DESC);'
        )

        connection.commit()


def create_watchlist_table():
    # What gates the data behind the login. A user only sees posts from the
    # communities they watch, so "scrape everything" means "get more accounts"
    # - which is exactly the pressure the detection layer exists to measure.
    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        create_table_query = '''
        CREATE TABLE IF NOT EXISTS Watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            community_id INTEGER NOT NULL,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, community_id),
            FOREIGN KEY (user_id) REFERENCES Users (id),
            FOREIGN KEY (community_id) REFERENCES Communities (id)
        );
        '''

        cursor.execute(create_table_query)
        connection.commit()


def create_sessions_table():
    # One row per login. bot_score is the running total from ScoreEvents.
    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        create_table_query = '''
        CREATE TABLE IF NOT EXISTS Sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_token TEXT NOT NULL UNIQUE,
            user_id INTEGER,
            ip_address TEXT,
            user_agent TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            bot_score INTEGER DEFAULT 0,
            verdict TEXT DEFAULT 'unscored',
            FOREIGN KEY (user_id) REFERENCES Users (id)
        );
        '''

        cursor.execute(create_table_query)
        connection.commit()


def create_request_log_table():
    # Every request. Feeds /debug/sessions and the Stage 2 flakiness work.
    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        create_table_query = '''
        CREATE TABLE IF NOT EXISTS RequestLog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            requested_at TEXT DEFAULT CURRENT_TIMESTAMP,
            method TEXT,
            path TEXT,
            status_code INTEGER,
            latency_ms INTEGER,
            header_order TEXT,
            sec_fetch_mode TEXT,
            sec_ch_ua TEXT,
            injected_fault TEXT,
            FOREIGN KEY (session_id) REFERENCES Sessions (id)
        );
        '''

        cursor.execute(create_table_query)
        connection.commit()


def create_fingerprints_table():
    # Stage 3. One row per telemetry POST from the client bundle.
    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        create_table_query = '''
        CREATE TABLE IF NOT EXISTS Fingerprints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            collected_at TEXT DEFAULT CURRENT_TIMESTAMP,
            webdriver_flag INTEGER,
            plugin_count INTEGER,
            languages TEXT,
            hardware_concurrency INTEGER,
            device_memory REAL,
            screen_width INTEGER,
            screen_height INTEGER,
            viewport_width INTEGER,
            viewport_height INTEGER,
            outer_height INTEGER,
            canvas_hash TEXT,
            webgl_hash TEXT,
            webgl_vendor TEXT,
            webgl_renderer TEXT,
            reported_timezone TEXT,
            font_count INTEGER,
            notification_permission TEXT,
            permissions_query_state TEXT,
            chrome_runtime_present INTEGER,
            load_to_first_interaction_ms INTEGER,
            raw_payload TEXT,
            FOREIGN KEY (session_id) REFERENCES Sessions (id)
        );
        '''

        cursor.execute(create_table_query)
        connection.commit()


def create_behavior_events_table():
    # Stage 6. Raw interaction stream — mousemove, click, scroll, page_change.
    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        create_table_query = '''
        CREATE TABLE IF NOT EXISTS BehaviorEvents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            occurred_at TEXT DEFAULT CURRENT_TIMESTAMP,
            event_type TEXT NOT NULL,
            target_selector TEXT,
            pointer_x INTEGER,
            pointer_y INTEGER,
            ms_since_previous INTEGER,
            FOREIGN KEY (session_id) REFERENCES Sessions (id)
        );
        '''

        cursor.execute(create_table_query)
        connection.commit()


def create_score_events_table():
    # The observation layer. One row every time a signal fires, with its weight.
    # Keeping these itemised is what lets you answer "why was this blocked?"
    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        create_table_query = '''
        CREATE TABLE IF NOT EXISTS ScoreEvents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            fired_at TEXT DEFAULT CURRENT_TIMESTAMP,
            signal_name TEXT NOT NULL,
            signal_source TEXT,
            weight INTEGER NOT NULL,
            observed_value TEXT,
            expected_value TEXT,
            note TEXT,
            FOREIGN KEY (session_id) REFERENCES Sessions (id)
        );
        '''

        cursor.execute(create_table_query)
        connection.commit()


def create_monitor_runs_table():
    # One row per sweep. The dashboard's history table reads straight off this.
    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        create_table_query = '''
        CREATE TABLE IF NOT EXISTS MonitorRuns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger TEXT NOT NULL,
            backend TEXT NOT NULL,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            status TEXT DEFAULT 'running',
            communities_checked INTEGER DEFAULT 0,
            posts_new INTEGER DEFAULT 0,
            posts_refreshed INTEGER DEFAULT 0,
            error TEXT
        );
        '''

        cursor.execute(create_table_query)
        connection.commit()


def create_monitor_run_items_table():
    # Per-community outcome within a run, so one dead subreddit doesn't just
    # vanish into an aggregate count.
    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        create_table_query = '''
        CREATE TABLE IF NOT EXISTS MonitorRunItems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            community_id INTEGER,
            community_name TEXT,
            status TEXT,
            posts_new INTEGER DEFAULT 0,
            posts_refreshed INTEGER DEFAULT 0,
            error TEXT,
            FOREIGN KEY (run_id) REFERENCES MonitorRuns (id),
            FOREIGN KEY (community_id) REFERENCES Communities (id)
        );
        '''

        cursor.execute(create_table_query)
        connection.commit()


def migrate():
    """Add monitoring columns to tables that predate the dashboard.

    Idempotent - checks PRAGMA table_info rather than catching errors, so it's
    safe to run on every startup.
    """
    additions = {
        'Communities': [
            ('monitor_enabled', 'INTEGER DEFAULT 1'),
            ('monitor_sort', "TEXT DEFAULT 'new'"),
            ('monitor_limit', 'INTEGER DEFAULT 50'),
            ('last_checked_at', 'TEXT'),
            ('added_by_user_id', 'INTEGER'),
        ],
        'Posts': [
            # No DEFAULT CURRENT_TIMESTAMP here: sqlite rejects non-constant
            # defaults on ALTER TABLE ADD COLUMN. The upsert sets it explicitly.
            ('first_seen_at', 'TEXT'),
            ('first_seen_run_id', 'INTEGER'),
        ],
    }

    with sqlite3.connect(DB_PATH) as connection:

        cursor = connection.cursor()

        for table, columns in additions.items():
            existing = {row[1] for row in cursor.execute(f'PRAGMA table_info({table});')}

            for column, spec in columns:
                if column in existing:
                    continue
                cursor.execute(f'ALTER TABLE {table} ADD COLUMN {column} {spec};')
                print(f"  migrated: {table}.{column}")

        # Posts that predate the column were all first seen at ingest time.
        cursor.execute(
            'UPDATE Posts SET first_seen_at = fetched_at WHERE first_seen_at IS NULL;'
        )

        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_posts_first_seen ON Posts (first_seen_at DESC);'
        )

        connection.commit()


def drop_legacy_policies():
    """Communities/Posts replaced the faker-generated insurance records."""
    if not table_exists('Policies'):
        return

    with sqlite3.connect(DB_PATH) as connection:
        cursor = connection.cursor()
        cursor.execute('DROP TABLE Policies;')
        connection.commit()

    print("Dropped legacy 'Policies' table (replaced by Communities + Posts).")


def reset_db():
    """Nuke and rebuild. Ingested Reddit data goes with it - re-run the ingest."""
    db_file = Path(DB_PATH)

    if db_file.exists():
        db_file.unlink()
        print(f"Deleted {db_file}")

    create_tables()


if __name__ == '__main__':
    if '--reset' in sys.argv:
        reset_db()
    else:
        create_tables()

    print("Schema ready. Next: python reddit_ingest.py --subreddits python,programming")
