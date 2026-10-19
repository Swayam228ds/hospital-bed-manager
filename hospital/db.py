"""Storage for wards, beds, patients and settings.

A hospital lives either in a local SQLite file or in a PostgreSQL database (e.g. Supabase).
The rest of the code writes SQLite-style SQL; PgConnection translates it for Postgres.
"""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "hospital.db"            # demo hospital (simulated clock, reset any time)
LIVE_DB_PATH = ROOT / "data" / "my_hospital.db"    # your own hospital, set up by hand
# Kept out of "public" so Supabase's REST API does not expose it. DB_SCHEMA lets tests use their own schema.
PG_SCHEMA_NAME = re.sub(r"\W", "", os.environ.get("DB_SCHEMA", "bed_manager")) or "bed_manager"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wards (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS beds (
    id INTEGER PRIMARY KEY, ward_id INTEGER NOT NULL REFERENCES wards(id),
    label TEXT NOT NULL, patient_id INTEGER REFERENCES patients(id));
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL, age INTEGER, gender TEXT,
    source TEXT NOT NULL,              -- 'opd' (planned) or 'emergency'
    ward TEXT,                         -- ward the doctor requested
    condition TEXT,                    -- short reason for admission
    clinical TEXT NOT NULL DEFAULT '{}',  -- JSON of model inputs (comorbidities, vitals...)
    urgency INTEGER NOT NULL DEFAULT 1,   -- 1 routine, 2 soon, 3 urgent (set by doctor)
    admit_prob REAL,                   -- M2 output for emergency patients
    status TEXT NOT NULL,              -- 'waiting', 'admitted', 'discharged'
    added_on TEXT NOT NULL, admitted_on TEXT, discharged_on TEXT,
    los_p50 REAL, los_p90 REAL,        -- M3 output, days
    sim_true_los REAL                  -- demo only: the stay the simulator will actually use
);
CREATE TABLE IF NOT EXISTS daily_log (
    day TEXT PRIMARY KEY, admissions INTEGER NOT NULL, discharges INTEGER NOT NULL,
    arrivals INTEGER NOT NULL, occupied INTEGER NOT NULL);
"""

# Same tables in Postgres types. Row level security is on with no policies, so only the
# database owner (this app) can read them, never Supabase's public API.
PG_SCHEMA = f"""
CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA_NAME};
SET search_path TO {PG_SCHEMA_NAME};
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wards (id SERIAL PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS patients (
    id SERIAL PRIMARY KEY, name TEXT NOT NULL, age INTEGER, gender TEXT, source TEXT NOT NULL,
    ward TEXT, condition TEXT, clinical TEXT NOT NULL DEFAULT '{{}}', urgency INTEGER NOT NULL DEFAULT 1,
    admit_prob DOUBLE PRECISION, status TEXT NOT NULL, added_on TEXT NOT NULL, admitted_on TEXT,
    discharged_on TEXT, los_p50 DOUBLE PRECISION, los_p90 DOUBLE PRECISION, sim_true_los DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS beds (
    id SERIAL PRIMARY KEY, ward_id INTEGER NOT NULL REFERENCES wards(id),
    label TEXT NOT NULL, patient_id INTEGER REFERENCES patients(id));
CREATE TABLE IF NOT EXISTS daily_log (
    day TEXT PRIMARY KEY, admissions INTEGER NOT NULL, discharges INTEGER NOT NULL,
    arrivals INTEGER NOT NULL, occupied INTEGER NOT NULL);
ALTER TABLE settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE wards ENABLE ROW LEVEL SECURITY;
ALTER TABLE patients ENABLE ROW LEVEL SECURITY;
ALTER TABLE beds ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_log ENABLE ROW LEVEL SECURITY;
"""

DEFAULT_SETTINGS = {"today": "2026-10-19", "w_urgency": "10", "w_wait": "1", "w_prob": "20", "wait_cap": "30"}


# ---------- Postgres ----------

class _PgRow(dict):
    """A row readable by column name or by position, like sqlite3.Row."""
    def __init__(self, cols, values):
        super().__init__(zip(cols, values))
        self._values = values

    def __getitem__(self, k):
        return self._values[k] if isinstance(k, int) else super().__getitem__(k)


class _PgCursor:
    def __init__(self, cur, lastrowid=None):
        self._cur, self.lastrowid = cur, lastrowid

    def _row(self, r):
        return None if r is None else _PgRow([d.name for d in self._cur.description], r)

    def fetchone(self):
        return self._row(self._cur.fetchone()) if self._cur.description else None

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()] if self._cur.description else []

    def __iter__(self):
        return iter(self.fetchall())


def _to_pg(sql):
    """Translate the SQLite dialect used in this project to Postgres. Returns (sql, wants_id)."""
    s = sql.replace("?", "%s")
    s = re.sub(r"INSERT OR IGNORE INTO (.*)", r"INSERT INTO \1 ON CONFLICT DO NOTHING", s, flags=re.S)
    if "INSERT OR REPLACE" in s:
        raise NotImplementedError("INSERT OR REPLACE is only used by the SQLite demo")
    s = re.sub(r"DROP TABLE IF EXISTS (\w+)", r"DROP TABLE IF EXISTS \1 CASCADE", s)
    wants_id = bool(re.match(r"\s*INSERT INTO (wards|beds|patients)\b", s)) and "RETURNING" not in s.upper()
    return (s + " RETURNING id" if wants_id else s), wants_id


class PgConnection:
    is_pg = True

    def __init__(self, url):
        import psycopg
        # prepare_threshold=None keeps it working behind Supabase's transaction pooler too.
        self._con = psycopg.connect(url, prepare_threshold=None, connect_timeout=15)
        self._con.execute(f"CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA_NAME}")
        self._con.execute(f"SET search_path TO {PG_SCHEMA_NAME}")

    def execute(self, sql, params=()):
        sql, wants_id = _to_pg(sql)
        cur = self._con.cursor()
        cur.execute(sql, params)
        return _PgCursor(cur, cur.fetchone()[0] if wants_id else None)

    def executescript(self, script):
        self._con.execute(script)

    def commit(self):
        self._con.commit()

    def close(self):
        self._con.close()


def is_url(target):
    return isinstance(target, str) and target.startswith(("postgres://", "postgresql://"))


def describe(target):
    """Where a hospital is stored, without the password."""
    if is_url(target):
        return "PostgreSQL at " + re.sub(r"^\w+://[^@]*@", "", target).split("?")[0]
    p = Path(target)
    return f"SQLite file {p.relative_to(ROOT).as_posix() if p.is_relative_to(ROOT) else p}"


def live_target():
    """Your own hospital: Postgres if DATABASE_URL is set (e.g. in .env), else a local SQLite file."""
    url = os.environ.get("DATABASE_URL", "").strip()
    return url if url else LIVE_DB_PATH


# ---------- shared ----------

def connect(target=DB_PATH):
    if is_url(target):
        return PgConnection(target)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(target)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def session(target=DB_PATH):
    con = connect(target)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init(con):
    con.executescript(PG_SCHEMA if getattr(con, "is_pg", False) else SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, v))


def get_settings(con):
    return {r["key"]: r["value"] for r in con.execute("SELECT * FROM settings")}


def set_setting(con, key, value):
    con.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)))


def row_to_dict(row):
    d = dict(row)
    if "clinical" in d:
        d["clinical"] = json.loads(d["clinical"] or "{}")
    d.pop("sim_true_los", None)
    return d
