"""Table definitions and migrations. Owned by the api container, which runs
init_db() at startup; the worker waits for it via db.wait_for_schema().
"""

from app.db import connection_scope, table_exists

SCHEMA = {
    'Users': '''
        CREATE TABLE IF NOT EXISTS Users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            agency_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    ''',

    # One row per subreddit. 'name' is the bare slug ('python'), the join key.
    'Communities': '''
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
            monitor_enabled INTEGER DEFAULT 1,
            monitor_sort TEXT DEFAULT 'new',
            monitor_limit INTEGER DEFAULT 50,
            last_checked_at TEXT,
            added_by_user_id INTEGER,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    ''',

    # The records a scraper wants. post_id is Reddit's own base36 id, which
    # makes re-ingest an upsert rather than a duplicate.
    'Posts': '''
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
            first_seen_at TEXT,
            first_seen_run_id INTEGER,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (community_id) REFERENCES Communities (id)
        );
    ''',

    # What gates the data behind the login: an account sees a subreddit's posts
    # only while it watches that subreddit.
    'Watchlist': '''
        CREATE TABLE IF NOT EXISTS Watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            community_id INTEGER NOT NULL,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, community_id),
            FOREIGN KEY (user_id) REFERENCES Users (id),
            FOREIGN KEY (community_id) REFERENCES Communities (id)
        );
    ''',

    'Sessions': '''
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
    ''',

    # Every request. See the note in api/main.py about what an SPA does to the
    # header-order and sec-fetch-mode signals.
    'RequestLog': '''
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
    ''',

    'Fingerprints': '''
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
    ''',

    'BehaviorEvents': '''
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
    ''',

    # One row every time a signal fires, with its weight. Keeping these
    # itemised is what lets you answer "why was this blocked?"
    'ScoreEvents': '''
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
    ''',

    # One row per sweep. 'queued' is how the api container asks the worker for
    # an out-of-band run - it can't spawn a browser in its own image.
    'MonitorRuns': '''
        CREATE TABLE IF NOT EXISTS MonitorRuns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger TEXT NOT NULL,
            backend TEXT NOT NULL,
            queued_at TEXT DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            finished_at TEXT,
            status TEXT DEFAULT 'queued',
            communities_checked INTEGER DEFAULT 0,
            posts_new INTEGER DEFAULT 0,
            posts_refreshed INTEGER DEFAULT 0,
            error TEXT
        );
    ''',

    'MonitorRunItems': '''
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
    ''',
}

INDEXES = (
    'CREATE INDEX IF NOT EXISTS idx_posts_community_score ON Posts (community_id, score DESC);',
    'CREATE INDEX IF NOT EXISTS idx_posts_first_seen ON Posts (first_seen_at DESC);',
    'CREATE INDEX IF NOT EXISTS idx_runitems_run ON MonitorRunItems (run_id);',
    'CREATE INDEX IF NOT EXISTS idx_requestlog_session ON RequestLog (session_id);',
)

# Columns added after a table shipped. Checked against PRAGMA table_info rather
# than caught as errors, so this is safe to run on every startup.
MIGRATIONS = {
    'Communities': [
        ('monitor_enabled', 'INTEGER DEFAULT 1'),
        ('monitor_sort', "TEXT DEFAULT 'new'"),
        ('monitor_limit', 'INTEGER DEFAULT 50'),
        ('last_checked_at', 'TEXT'),
        ('added_by_user_id', 'INTEGER'),
    ],
    'Posts': [
        # No DEFAULT CURRENT_TIMESTAMP: sqlite rejects non-constant defaults on
        # ALTER TABLE ADD COLUMN. The upsert sets it explicitly.
        ('first_seen_at', 'TEXT'),
        ('first_seen_run_id', 'INTEGER'),
    ],
    'MonitorRuns': [
        ('queued_at', 'TEXT'),
    ],
}


def init_db(seed=True):
    with connection_scope() as connection:
        cursor = connection.cursor()

        for statement in SCHEMA.values():
            cursor.execute(statement)

        for table, columns in MIGRATIONS.items():
            if not table_exists(connection, table):
                continue

            existing = {row[1] for row in cursor.execute(f'PRAGMA table_info({table});')}

            for column, spec in columns:
                if column not in existing:
                    cursor.execute(f'ALTER TABLE {table} ADD COLUMN {column} {spec};')
                    print(f'migrated: {table}.{column}', flush=True)

        cursor.execute('UPDATE Posts SET first_seen_at = fetched_at WHERE first_seen_at IS NULL;')
        cursor.execute('UPDATE MonitorRuns SET queued_at = started_at WHERE queued_at IS NULL;')

        for statement in INDEXES:
            cursor.execute(statement)

        if table_exists(connection, 'Policies'):
            cursor.execute('DROP TABLE Policies;')
            print('dropped legacy Policies table', flush=True)

    if seed:
        from app.seed import seed_users, seed_watchlists
        seed_users()
        seed_watchlists()
