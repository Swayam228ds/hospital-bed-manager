"""SQLite storage for wards, beds, patients and settings."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "hospital.db"            # demo hospital (simulated clock, reset any time)
LIVE_DB_PATH = ROOT / "data" / "my_hospital.db"    # your own hospital, set up by hand

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

DEFAULT_SETTINGS = {"today": "2026-10-19", "w_urgency": "10", "w_wait": "1", "w_prob": "20", "wait_cap": "30"}


def connect(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def session(path=DB_PATH):
    con = connect(path)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init(con):
    con.executescript(SCHEMA)
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
