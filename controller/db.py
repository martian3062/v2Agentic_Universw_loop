"""SQLite store. One file holds everything the controller knows, so a restart resumes where it stopped.

Durability: WAL + synchronous=NORMAL survives a killed process (only an OS crash can lose the last commit).
Every unit of work (one chunk ingested, one decision taken, one request about to be sent) is one transaction.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS chunks (
  chunk INTEGER PRIMARY KEY, kind TEXT, events INTEGER, last_event_id INTEGER, until_minute INTEGER,
  ingested_wall REAL, ingest_seconds REAL);

CREATE TABLE IF NOT EXISTS workflows (
  workflow_id TEXT PRIMARY KEY, tenant_id TEXT, spec TEXT);

-- every config version we have seen, whoever made it
CREATE TABLE IF NOT EXISTS config_versions (
  workflow_id TEXT, version INTEGER, actor TEXT, changed_dims TEXT, proposal_id TEXT, approval_id TEXT,
  applied_minute INTEGER, config TEXT, PRIMARY KEY (workflow_id, version));

-- a person-feature tuple; leads point at one
CREATE TABLE IF NOT EXISTS segs (
  seg_id INTEGER PRIMARY KEY, workflow_id TEXT, lang TEXT, city_tier TEXT, age_band TEXT, value_band TEXT,
  UNIQUE (workflow_id, lang, city_tier, age_band, value_band));
CREATE TABLE IF NOT EXISTS leads (lead_id TEXT PRIMARY KEY, seg_id INTEGER, day INTEGER) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS leads_day ON leads(day);

CREATE TABLE IF NOT EXISTS campaigns (
  campaign_id TEXT PRIMARY KEY, workflow_id TEXT, created_minute INTEGER, leads INTEGER, config_version INTEGER);

-- hourly call aggregates: detection, experiments, dry runs
CREATE TABLE IF NOT EXISTS agg_hour (
  workflow_id TEXT, hour INTEGER, cohort_id TEXT, config_version INTEGER,
  wm TEXT, voice TEXT, cid TEXT, prompt TEXT,
  dials INTEGER, conn INTEGER, eng INTEGER, flags INTEGER, qa_n INTEGER,
  PRIMARY KEY (workflow_id, hour, cohort_id, config_version, wm, voice, cid, prompt)) WITHOUT ROWID;

-- daily aggregates by person segment: who a variant works for
CREATE TABLE IF NOT EXISTS agg_seg (
  workflow_id TEXT, day INTEGER, seg_id INTEGER, cohort_id TEXT, config_version INTEGER, wm TEXT, voice TEXT,
  cid TEXT, dials INTEGER, conn INTEGER, eng INTEGER,
  PRIMARY KEY (workflow_id, day, seg_id, cohort_id, config_version, wm, voice, cid)) WITHOUT ROWID;

-- daily outcome per workflow: leads uploaded/dialled/concluded
CREATE TABLE IF NOT EXISTS daily (
  workflow_id TEXT, day INTEGER, uploaded INTEGER DEFAULT 0, dials INTEGER DEFAULT 0, conn INTEGER DEFAULT 0,
  eng INTEGER DEFAULT 0, leads_dialled INTEGER DEFAULT 0, PRIMARY KEY (workflow_id, day));

-- the one shared cohort object (C2): detection, experiments, proposals and the supervisor view use these ids
CREATE TABLE IF NOT EXISTS cohorts (
  cohort_id TEXT, workflow_id TEXT, where_json TEXT, origin TEXT, created_minute INTEGER,
  status TEXT, PRIMARY KEY (workflow_id, cohort_id));   -- ids like lang_ta repeat across workflows

CREATE TABLE IF NOT EXISTS experiments (
  exp_id TEXT PRIMARY KEY, workflow_id TEXT, dim TEXT, cohort_id TEXT, kpi TEXT, control TEXT, arms TEXT,
  plan TEXT, status TEXT, created_minute INTEGER, live_minute INTEGER, live_version INTEGER,
  versions TEXT, ended_minute INTEGER, winner TEXT, result TEXT, last_eval TEXT, monitor TEXT);

-- a desired change, persisted as replayable ops; governance batches intents into proposals
CREATE TABLE IF NOT EXISTS intents (
  intent_id TEXT PRIMARY KEY, workflow_id TEXT, kind TEXT, owner TEXT, ops TEXT, summary TEXT, rationale TEXT,
  evidence TEXT, estimate TEXT, urgent INTEGER, path TEXT, status TEXT, proposal_id TEXT, attempts INTEGER,
  created_minute INTEGER, updated_minute INTEGER, note TEXT);

CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY, workflow_id TEXT, path TEXT, status TEXT, base_version INTEGER,
  proposed_config TEXT, body TEXT, approval_id TEXT, intents TEXT, urgent INTEGER,
  created_minute INTEGER, submitted_minute INTEGER, decided_minute INTEGER, applied_minute INTEGER,
  applied_version INTEGER, reasons TEXT, apply_by INTEGER, expires_at INTEGER,
  send_attempts INTEGER DEFAULT 0, next_send_minute INTEGER);   -- retry backoff for uncertain sends

-- one controller process per world and per workflow (controller/leases.py)
CREATE TABLE IF NOT EXISTS leases (
  name TEXT PRIMARY KEY, owner TEXT, host TEXT, pid INTEGER, heartbeat REAL);

CREATE TABLE IF NOT EXISTS incidents (
  incident_id TEXT PRIMARY KEY, workflow_id TEXT, kind TEXT, key TEXT, status TEXT, opened_minute INTEGER,
  resolved_minute INTEGER, data TEXT);

-- the audit log (C12): every detection, decision, request, decision received, apply and outcome
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, minute INTEGER, workflow_id TEXT, kind TEXT, summary TEXT, data TEXT,
  proposal_id TEXT, approval_id TEXT, version INTEGER, ref TEXT, wall REAL);
CREATE INDEX IF NOT EXISTS decisions_wf ON decisions(workflow_id, minute);
CREATE INDEX IF NOT EXISTS decisions_prop ON decisions(proposal_id);
CREATE INDEX IF NOT EXISTS decisions_ref ON decisions(ref);

CREATE TABLE IF NOT EXISTS metrics (
  minute INTEGER PRIMARY KEY, wall REAL, events INTEGER, ingest_s REAL, decide_s REAL, advance_s REAL,
  credits REAL, pending INTEGER, decisions INTEGER, rss_mb REAL, api_errors INTEGER);

CREATE TABLE IF NOT EXISTS alerts (
  alert_key TEXT PRIMARY KEY, minute INTEGER, severity TEXT, workflow_id TEXT, message TEXT,
  resolved_minute INTEGER);
"""


def atomic(fn):
    """Run an owner's decision in ONE transaction: the intent it files and the state that intent depends on
    (an experiment's conclusion, an incident's chosen fix, a number put on probation) commit together or not at
    all. Nested `with store:` blocks inside join it. Never wrap a step that spends credits: a rollback there would
    erase the record of a charge that the simulator has already made."""
    import functools

    @functools.wraps(fn)
    def run(self, *args, **kwargs):
        with self.s:
            return fn(self, *args, **kwargs)
    return run


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.execute("PRAGMA cache_size=-65536")
        self.db.executescript(SCHEMA)
        self._migrate()
        self._depth = 0

    def _migrate(self):
        """Bring stores created by earlier versions up to the current schema, in place."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(proposals)")}
        for col, decl in (("send_attempts", "INTEGER DEFAULT 0"), ("next_send_minute", "INTEGER")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE proposals ADD COLUMN {col} {decl}")
        # cohorts re-keyed per workflow
        pk = [r["name"] for r in self.db.execute("PRAGMA table_info(cohorts)") if r["pk"]]
        if pk == ["cohort_id"]:
            self.db.executescript("""
                BEGIN;
                ALTER TABLE cohorts RENAME TO cohorts_v1;
                CREATE TABLE cohorts (cohort_id TEXT, workflow_id TEXT, where_json TEXT, origin TEXT,
                  created_minute INTEGER, status TEXT, PRIMARY KEY (workflow_id, cohort_id));
                INSERT OR IGNORE INTO cohorts SELECT cohort_id, workflow_id, where_json, origin, created_minute, status
                  FROM cohorts_v1;
                DROP TABLE cohorts_v1;
                COMMIT;""")

    # ------------------------------------------------------------ transactions
    def __enter__(self):
        if self._depth == 0:
            self.db.execute("BEGIN IMMEDIATE")
        self._depth += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self._depth -= 1
        if self._depth == 0:
            self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        return False

    def x(self, sql: str, args=()):
        return self.db.execute(sql, args)

    def q(self, sql: str, args=()):
        return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        return self.db.execute(sql, args).fetchone()

    # ------------------------------------------------------------ meta
    def get(self, key: str, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set(self, key: str, value):
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    def next_seq(self, name: str) -> int:
        n = self.get(f"seq:{name}", 0) + 1
        self.set(f"seq:{name}", n)
        return n

    # ------------------------------------------------------------ audit
    def record(self, minute: int, workflow_id: str | None, kind: str, summary: str, data=None, *,
               proposal_id=None, approval_id=None, version=None, ref=None):
        self.x("INSERT INTO decisions(minute,workflow_id,kind,summary,data,proposal_id,approval_id,version,ref,wall)"
               " VALUES(?,?,?,?,?,?,?,?,?,?)",
               (minute, workflow_id, kind, summary, json.dumps(data, default=str) if data is not None else None,
                proposal_id, approval_id, version, ref, time.time()))

    def alert(self, key: str, minute: int, severity: str, workflow_id: str | None, message: str):
        self.x("INSERT INTO alerts(alert_key,minute,severity,workflow_id,message) VALUES(?,?,?,?,?) "
               "ON CONFLICT(alert_key) DO UPDATE SET message=excluded.message, resolved_minute=NULL",
               (key, minute, severity, workflow_id, message))

    def resolve_alert(self, key: str, minute: int):
        self.x("UPDATE alerts SET resolved_minute=? WHERE alert_key=? AND resolved_minute IS NULL", (minute, key))
