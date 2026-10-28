#!/usr/bin/env python3
"""OBS todo overlay + manage panel, backed by SQLite.

Pages:
  /        -> stream overlay (read-only, follows the server's current day)
  /manage  -> control panel (tasks, timer, summary, log, tracker)

Usage: python3 server.py [--file ../todolist.txt] [--db todo.db] [--port 8787]
"""

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ITEM_RE = re.compile(r"^\[\s*([XO]?)\s*\]\s*(.*)$")
STATE_MAP = {"X": "done", "O": "doing", "": "todo"}

PF_DATE_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
PF_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*~\s*(\d{1,2}):(\d{2})$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    id       INTEGER PRIMARY KEY,
    label    TEXT NOT NULL,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id       INTEGER PRIMARY KEY,
    day_id   INTEGER NOT NULL REFERENCES days(id) ON DELETE CASCADE,
    text     TEXT NOT NULL,
    details  TEXT NOT NULL DEFAULT '',
    state    TEXT NOT NULL DEFAULT 'todo' CHECK (state IN ('todo','doing','done')),
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS categories (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pomo_sessions (
    id          INTEGER PRIMARY KEY,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    tasks_done  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pomo_periods (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER REFERENCES pomo_sessions(id) ON DELETE SET NULL,
    day_id      INTEGER REFERENCES days(id) ON DELETE SET NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    task_id     INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    task_label  TEXT NOT NULL DEFAULT '',
    minutes     INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'timer'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_periods_dedupe
    ON pomo_periods(category_id, task_label, minutes, started_at, ended_at);
CREATE TABLE IF NOT EXISTS tracker_groups (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tracker_subjects (
    id            INTEGER PRIMARY KEY,
    group_id      INTEGER NOT NULL REFERENCES tracker_groups(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'book' CHECK (kind IN ('book','video','article','course','other')),
    url           TEXT NOT NULL DEFAULT '',
    unit_total    REAL,
    unit_done     REAL NOT NULL DEFAULT 0,
    unit_label    TEXT NOT NULL DEFAULT 'pages',
    position_at   TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL DEFAULT 'todo' CHECK (state IN ('todo','doing','paused','done','dropped')),
    started_at    TEXT,
    finished_at   TEXT,
    last_touched  TEXT,
    snooze_until  TEXT,
    snooze_reason TEXT NOT NULL DEFAULT '',
    is_next       INTEGER NOT NULL DEFAULT 0,
    notes         TEXT NOT NULL DEFAULT '',
    position      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tracker_progress_log (
    id              INTEGER PRIMARY KEY,
    subject_id      INTEGER NOT NULL REFERENCES tracker_subjects(id) ON DELETE CASCADE,
    logged_at       TEXT NOT NULL,
    delta           REAL NOT NULL,
    unit_done_after REAL NOT NULL,
    note            TEXT NOT NULL DEFAULT ''
);
"""


def parse_seed(text: str):
    items = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = ITEM_RE.match(line)
        if m:
            items.append({"state": STATE_MAP[m.group(1)], "text": m.group(2)})
    return items


def get_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str, seed_file: str | None):
    conn = get_db(db_path)
    conn.executescript(SCHEMA)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)")]
    if "done_at" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN done_at TEXT")
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(pomo_sessions)")]
    if "tasks_done" not in cols:
        conn.execute("ALTER TABLE pomo_sessions ADD COLUMN tasks_done INTEGER NOT NULL DEFAULT 0")
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(pomo_periods)")]
    if "day_id" not in cols:
        conn.execute("ALTER TABLE pomo_periods ADD COLUMN day_id INTEGER REFERENCES days(id) ON DELETE SET NULL")
    if "subject_id" not in cols:
        conn.execute("ALTER TABLE pomo_periods ADD COLUMN subject_id INTEGER REFERENCES tracker_subjects(id) ON DELETE SET NULL")
    if conn.execute("SELECT COUNT(*) FROM days").fetchone()[0] == 0:
        cur = conn.execute("INSERT INTO days (label, position) VALUES ('Day 1', 1)")
        day_id = cur.lastrowid
        if seed_file and Path(seed_file).exists():
            items = parse_seed(Path(seed_file).read_text(encoding="utf-8"))
            conn.executemany(
                "INSERT INTO tasks (day_id, text, state, position) VALUES (?,?,?,?)",
                [(day_id, i["text"], i["state"], n + 1) for n, i in enumerate(items)],
            )
        conn.execute("INSERT INTO settings (key, value) VALUES ('current_day_id', ?)", (str(day_id),))
    conn.execute(
        "UPDATE pomo_periods SET day_id=? WHERE source='timer' AND day_id IS NULL",
        (current_day_id(conn),),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- helpers

def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def today_str() -> str:
    return date.today().isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def current_day_id(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='current_day_id'").fetchone()
    if row:
        day = conn.execute("SELECT id FROM days WHERE id=?", (row["value"],)).fetchone()
        if day:
            return day["id"]
    first = conn.execute("SELECT id FROM days ORDER BY position LIMIT 1").fetchone()
    return first["id"]


def todo_payload(conn) -> dict:
    tasks = conn.execute(
        "SELECT id, text, state FROM tasks "
        "ORDER BY CASE state WHEN 'doing' THEN 0 WHEN 'todo' THEN 1 ELSE 2 END, position"
    ).fetchall()
    return {
        "items": [dict(t) for t in tasks],
        "stats": {
            "done": sum(t["state"] == "done" for t in tasks),
            "total": len(tasks),
        },
    }


def create_day(conn, label: str | None = None) -> sqlite3.Row:
    row = conn.execute("SELECT COALESCE(MAX(position),0)+1 AS p FROM days").fetchone()
    if not label:
        label = f"Day {row['p']}"
    cur = conn.execute("INSERT INTO days (label, position) VALUES (?,?)", (label, row["p"]))
    conn.commit()
    return conn.execute("SELECT id, label, position FROM days WHERE id=?", (cur.lastrowid,)).fetchone()


def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value):
    conn.execute(
        "INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# ---------------------------------------------------------------- pomodoro engine

def pomo_config(conn) -> dict:
    return {
        "focus_min": int(get_setting(conn, "pomo_focus_min", 50)),
        "break_min": int(get_setting(conn, "pomo_break_min", 10)),
        "auto_next": get_setting(conn, "pomo_auto_next", "0") == "1",
    }


def default_pomo_state(cfg: dict) -> dict:
    return {
        "phase": "focus",
        "running": False,
        "started_at": None,
        "elapsed_ms": 0,
        "duration_min": cfg["focus_min"],
        "category_id": None,
        "block_category_id": None,
        "subject_id": None,
        "block_subject_id": None,
        "block_day_id": None,
        "phase_started_at": None,
        "logged_ms": 0,
    }


def get_pomo_state(conn) -> dict:
    raw = get_setting(conn, "pomo_state")
    cfg = pomo_config(conn)
    st = default_pomo_state(cfg)
    if raw:
        try:
            st.update(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            pass
    if not st["running"] and not st["elapsed_ms"]:
        st["duration_min"] = cfg["focus_min"] if st["phase"] == "focus" else cfg["break_min"]
    return st


def save_pomo_state(conn, st: dict):
    set_setting(conn, "pomo_state", json.dumps(st))


def pomo_elapsed_ms(st: dict) -> int:
    elapsed = int(st.get("elapsed_ms") or 0)
    if st.get("running") and st.get("started_at"):
        elapsed += int((datetime.now() - parse_iso(st["started_at"])).total_seconds() * 1000)
    return max(0, elapsed)


def new_phase(conn, st: dict, phase: str, running: bool) -> dict:
    cfg = pomo_config(conn)
    st["phase"] = phase
    st["duration_min"] = cfg["focus_min"] if phase == "focus" else cfg["break_min"]
    st["elapsed_ms"] = 0
    st["running"] = running
    st["started_at"] = now_iso() if running else None
    st["phase_started_at"] = now_iso()
    st["logged_ms"] = 0
    return st


def log_period(conn, st: dict, minutes: int, started_at: str, ended_at: str):
    cat = st.get("block_category_id")
    if cat is None:
        cat = st.get("category_id")
    day = st.get("block_day_id")
    if day is None:
        day = current_day_id(conn)
    sub = st.get("block_subject_id")
    if sub is None:
        sub = st.get("subject_id")
    conn.execute(
        "INSERT OR IGNORE INTO pomo_periods "
        "(session_id, day_id, category_id, subject_id, task_id, task_label, minutes, started_at, ended_at, source) "
        "VALUES (?,?,?,?,?,?,?,?,?, 'timer')",
        (None, day, cat, sub, None, "", minutes, started_at, ended_at),
    )
    if sub:
        touch_subject(conn, sub)


def log_partial(conn, st: dict):
    """Record unlogged focus time (called on pause/reset/skip/session end)."""
    if st["phase"] != "focus" or not st.get("phase_started_at"):
        return
    elapsed = pomo_elapsed_ms(st)
    logged = int(st.get("logged_ms") or 0)
    unlogged = elapsed - logged
    if unlogged < 60000:
        return
    started = st.get("started_at") or st["phase_started_at"]
    log_period(conn, st, max(1, round(unlogged / 60000)), started, now_iso())
    st["logged_ms"] = elapsed


def notify_phase(phase: str):
    """System-level popup when a timer phase runs out."""
    if not shutil.which("notify-send"):
        return
    try:
        msg = "focus done" if phase == "focus" else "break over"
        subprocess.Popen(["notify-send", "-a", "grind", "grind", msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def pomo_tick(conn) -> dict:
    """Finalize a due phase; advances to the next one (auto-starts if configured)."""
    st = get_pomo_state(conn)
    if not st.get("running") or not st.get("started_at"):
        return st
    elapsed = pomo_elapsed_ms(st)
    duration = int(st["duration_min"]) * 60000
    if elapsed < duration:
        return st
    if st["phase"] == "focus":
        logged = int(st.get("logged_ms") or 0)
        rem_ms = max(60000, duration - logged)
        p_started = st["started_at"]
        p_ended = (parse_iso(st["started_at"]) + timedelta(milliseconds=rem_ms)).isoformat(timespec="seconds")
        log_period(conn, st, max(1, round(rem_ms / 60000)), p_started, p_ended)
    notify_phase(st["phase"])
    auto = pomo_config(conn)["auto_next"]
    st = new_phase(conn, st, "break" if st["phase"] == "focus" else "focus", running=auto)
    save_pomo_state(conn, st)
    conn.commit()
    return st


def pomo_payload(conn) -> dict:
    st = pomo_tick(conn)
    cfg = pomo_config(conn)
    elapsed = pomo_elapsed_ms(st)
    duration = int(st["duration_min"]) * 60000
    remaining = max(0, duration - elapsed)

    category = None
    if st.get("category_id"):
        row = conn.execute("SELECT id, name FROM categories WHERE id=?", (st["category_id"],)).fetchone()
        if row:
            category = {"id": row["id"], "name": row["name"]}

    subject = None
    if st.get("subject_id"):
        row = conn.execute(
            "SELECT s.id, s.name, s.state, g.name AS group_name FROM tracker_subjects s "
            "LEFT JOIN tracker_groups g ON g.id=s.group_id WHERE s.id=?",
            (st["subject_id"],),
        ).fetchone()
        if row:
            subject = {"id": row["id"], "name": row["name"], "state": row["state"], "group": row["group_name"]}

    today = conn.execute(
        "SELECT COALESCE(SUM(minutes),0), COUNT(*) FROM pomo_periods WHERE substr(started_at,1,10)=?",
        (today_str(),),
    ).fetchone()
    today_min = today[0]
    if st["phase"] == "focus" and st.get("phase_started_at") and st["phase_started_at"][:10] == today_str():
        unlogged = pomo_elapsed_ms(st) - int(st.get("logged_ms") or 0)
        if unlogged > 0:
            today_min += round(unlogged / 60000)

    return {
        "phase": st["phase"],
        "running": bool(st["running"]),
        "ended": not st["running"] and remaining <= 0,
        "elapsed_ms": elapsed,
        "remaining_ms": remaining,
        "duration_min": int(st["duration_min"]),
        "focus_min": cfg["focus_min"],
        "break_min": cfg["break_min"],
        "auto_next": cfg["auto_next"],
        "category": category,
        "subject": subject,
        "today": {"focused_min": today_min, "periods": today[1]},
    }


def pomo_action(conn, action: str, body: dict):
    st = pomo_tick(conn)
    if action == "start":
        elapsed = pomo_elapsed_ms(st)
        if elapsed >= int(st["duration_min"]) * 60000 or not st.get("phase_started_at"):
            log_partial(conn, st)
            fresh = dict(st)
            fresh["elapsed_ms"] = 0
            fresh["running"] = False
            st = new_phase(conn, fresh, st["phase"], running=True)
            st["block_category_id"] = st.get("category_id")
            st["block_subject_id"] = st.get("subject_id")
            st["block_day_id"] = current_day_id(conn)
        else:
            st["running"] = True
            st["started_at"] = now_iso()
            if not int(st.get("elapsed_ms") or 0):
                # fresh paused phase: stamp picker category + active day onto it
                st["block_category_id"] = st.get("category_id")
                st["block_subject_id"] = st.get("subject_id")
                st["block_day_id"] = current_day_id(conn)
        save_pomo_state(conn, st)
    elif action == "pause":
        if st["running"]:
            log_partial(conn, st)
            st["elapsed_ms"] = pomo_elapsed_ms(st)
            st["running"] = False
            st["started_at"] = None
        save_pomo_state(conn, st)
    elif action == "reset":
        log_partial(conn, st)
        st["elapsed_ms"] = 0
        st["running"] = False
        st["started_at"] = None
        st["phase_started_at"] = None
        st["logged_ms"] = 0
        save_pomo_state(conn, st)
    elif action == "skip":
        log_partial(conn, st)
        st = new_phase(conn, st, "break" if st["phase"] == "focus" else "focus", running=False)
        save_pomo_state(conn, st)
    elif action == "set":
        if "category_id" in body:
            cid = body["category_id"]
            if cid:
                if not conn.execute("SELECT 1 FROM categories WHERE id=?", (cid,)).fetchone():
                    raise ValueError("category not found")
            st["category_id"] = int(cid) if cid else None
        if "subject_id" in body:
            sid = body["subject_id"]
            if sid:
                if not conn.execute("SELECT 1 FROM tracker_subjects WHERE id=?", (sid,)).fetchone():
                    raise ValueError("subject not found")
                st["subject_id"] = int(sid)
            else:
                st["subject_id"] = None
        save_pomo_state(conn, st)
    elif action == "config":
        cfg = pomo_config(conn)
        focus = body.get("focus_min", cfg["focus_min"])
        brk = body.get("break_min", cfg["break_min"])
        if not (isinstance(focus, int) and 1 <= focus <= 600):
            raise ValueError("focus_min must be 1..600")
        if not (isinstance(brk, int) and 1 <= brk <= 600):
            raise ValueError("break_min must be 1..600")
        set_setting(conn, "pomo_focus_min", focus)
        set_setting(conn, "pomo_break_min", brk)
        set_setting(conn, "pomo_auto_next", 1 if body.get("auto_next") else 0)
        if not st["running"] and not st["elapsed_ms"]:
            st["duration_min"] = focus if st["phase"] == "focus" else brk
            save_pomo_state(conn, st)
        conn.commit()
    else:
        raise ValueError("unknown action")
    conn.commit()
    return pomo_payload(conn)


# ---------------------------------------------------------------- import (pomofocus)

def parse_pomofocus(text: str):
    """Parse pasted 'Focus Time Detail' rows: date / HH:MM ~ HH:MM / project / minutes."""
    records, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if PF_DATE_RE.match(line):
            cur = {"date": line, "range": None, "project": None}
            continue
        if cur is None:
            continue
        if cur["range"] is None and PF_RANGE_RE.match(line):
            cur["range"] = line
            continue
        if cur["range"] is not None and line.isdigit():
            cur["minutes"] = int(line)
            records.append(cur)
            cur = None
            continue
        if cur["project"] is None:
            cur["project"] = line
    out = []
    for r in records:
        if not r.get("range") or "minutes" not in r:
            continue
        d = datetime.strptime(r["date"], "%d-%b-%Y")
        m = PF_RANGE_RE.match(r["range"])
        sh, sm, eh, em = (int(g) for g in m.groups())
        start = d.replace(hour=sh, minute=sm)
        end = d.replace(hour=eh, minute=em)
        if end < start:
            end += timedelta(days=1)
        out.append({
            "project": (r.get("project") or "").strip() or "Unallocated",
            "minutes": r["minutes"],
            "start": start.isoformat(timespec="seconds"),
            "end": end.isoformat(timespec="seconds"),
        })
    return out


def ensure_category(conn, name: str) -> int:
    name = name.strip() or "Unallocated"
    conn.execute("INSERT OR IGNORE INTO categories (name, position) VALUES (?, COALESCE((SELECT MAX(position)+1 FROM categories),1))", (name,))
    row = conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
    return row["id"]


def import_pomofocus(conn, text: str, dry_run: bool) -> dict:
    recs = parse_pomofocus(text)
    if dry_run:
        return {"parsed": len(recs), "imported": 0, "skipped": 0, "preview": recs[:50]}
    imported = skipped = 0
    cat_cache = {}
    for r in recs:
        if r["project"] not in cat_cache:
            cat_cache[r["project"]] = ensure_category(conn, r["project"])
        cur = conn.execute(
            "INSERT OR IGNORE INTO pomo_periods (category_id, task_label, minutes, started_at, ended_at, source) "
            "VALUES (?,?,?,?,?, 'import')",
            (cat_cache[r["project"]], "", r["minutes"], r["start"], r["end"]),
        )
        if cur.rowcount:
            imported += 1
        else:
            skipped += 1
    conn.commit()
    return {"parsed": len(recs), "imported": imported, "skipped": skipped, "preview": []}


# ---------------------------------------------------------------- tracker

TRACKER_KINDS = {
    "book":    {"label": "pages",   "total": None},
    "video":   {"label": "min",     "total": None},
    "article": {"label": "%",       "total": 100},
    "course":  {"label": "lessons", "total": None},
    "other":   {"label": "units",   "total": None},
}
TRACKER_STATES = ("todo", "doing", "paused", "done", "dropped")


def tracker_stale_days(conn) -> int:
    try:
        return max(1, min(365, int(get_setting(conn, "tracker_stale_days", 14))))
    except (TypeError, ValueError):
        return 14


def touch_subject(conn, subject_id):
    if subject_id:
        conn.execute("UPDATE tracker_subjects SET last_touched=? WHERE id=?", (now_iso(), subject_id))


def tracker_subject_payload(s, focus_min: float, stale_days: int, today) -> dict:
    """One subject row plus staleness fields."""
    idle = (today - date.fromisoformat(s["last_touched"][:10])).days if s["last_touched"] else None
    snoozed = bool(s["snooze_until"]) and s["snooze_until"] >= today.isoformat()
    stale = s["state"] == "doing" and idle is not None and idle >= stale_days and not snoozed
    return {
        "id": s["id"], "group_id": s["group_id"], "name": s["name"],
        "kind": s["kind"], "url": s["url"], "unit_label": s["unit_label"],
        "unit_total": s["unit_total"], "unit_done": s["unit_done"] or 0,
        "state": s["state"], "started_at": s["started_at"], "finished_at": s["finished_at"],
        "last_touched": s["last_touched"], "snooze_until": s["snooze_until"],
        "snooze_reason": s["snooze_reason"], "is_next": bool(s["is_next"]), "notes": s["notes"],
        "days_idle": idle, "stale": stale, "snoozed": snoozed,
        "focused_min": focus_min,
    }


def tracker_payload(conn) -> dict:
    today = date.today()
    stale_days = tracker_stale_days(conn)
    focus = {r["subject_id"]: r["m"] for r in conn.execute(
        "SELECT subject_id, SUM(minutes) AS m FROM pomo_periods "
        "WHERE subject_id IS NOT NULL GROUP BY subject_id")}

    groups = []
    totals = {"active": 0, "stale": 0, "units_left": 0.0, "focused_min": 0}
    for g in conn.execute("SELECT id, name, position FROM tracker_groups ORDER BY position"):
        subs = [
            tracker_subject_payload(s, focus.get(s["id"], 0), stale_days, today)
            for s in conn.execute(
                "SELECT * FROM tracker_subjects WHERE group_id=? ORDER BY position, id", (g["id"],)
            )
        ]
        counted = [s for s in subs if s["state"] not in ("dropped", "paused")]
        done_n = sum(s["state"] == "done" for s in counted)
        for s in subs:
            if s["state"] == "doing":
                totals["active"] += 1
            if s["stale"]:
                totals["stale"] += 1
            if s["state"] not in ("dropped", "paused") and s["unit_total"] is not None:
                totals["units_left"] += max(0, s["unit_total"] - s["unit_done"])
            totals["focused_min"] += s["focused_min"]
        groups.append({
            "id": g["id"], "name": g["name"], "position": g["position"],
            "pct": round(100 * done_n / len(counted)) if counted else None,
            "done": done_n, "count": len(counted),
            "subjects": subs,
        })
    return {"stale_days": stale_days, "focus": get_setting(conn, "tracker_focus", ""), "groups": groups, "totals": totals}


def tracker_progress(conn, subject_id: int, body: dict) -> dict:
    s = conn.execute("SELECT * FROM tracker_subjects WHERE id=?", (subject_id,)).fetchone()
    if not s:
        raise ValueError("subject not found")
    if "set" in body:
        target = float(body["set"])
        delta = target - (s["unit_done"] or 0)
    else:
        delta = float(body.get("delta") or 0)
    if not delta:
        return {"id": subject_id, "unit_done": s["unit_done"] or 0, "delta": 0}
    new_done = max(0.0, (s["unit_done"] or 0) + delta)
    if s["unit_total"] is not None:
        new_done = min(new_done, float(s["unit_total"]))
    applied = round(new_done - (s["unit_done"] or 0), 4)
    now = now_iso()
    conn.execute(
        "UPDATE tracker_subjects SET unit_done=?, last_touched=?, "
        "started_at=COALESCE(started_at, ?), "
        "state=CASE WHEN state='todo' THEN 'doing' ELSE state END WHERE id=?",
        (new_done, now, now, subject_id),
    )
    conn.execute(
        "INSERT INTO tracker_progress_log (subject_id, logged_at, delta, unit_done_after, note) "
        "VALUES (?,?,?,?,?)",
        (subject_id, now, applied, new_done, (body.get("note") or "").strip()),
    )
    conn.commit()
    return {"id": subject_id, "unit_done": new_done, "delta": applied}


def tracker_set_next(conn, subject_id: int, group_id: int, on: bool):
    if on:
        conn.execute("UPDATE tracker_subjects SET is_next=0 WHERE group_id=?", (group_id,))
    conn.execute("UPDATE tracker_subjects SET is_next=? WHERE id=?", (1 if on else 0, subject_id))


def tracker_subject_history(conn, subject_id: int, limit: int = 30) -> list:
    rows = conn.execute(
        "SELECT logged_at, delta, unit_done_after, note FROM tracker_progress_log "
        "WHERE subject_id=? ORDER BY id DESC LIMIT ?", (subject_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- summary

def pomo_summary(conn) -> dict:
    rows = conn.execute("SELECT minutes, started_at FROM pomo_periods ORDER BY started_at").fetchall()
    total_min = sum(r["minutes"] for r in rows)
    by_day, by_hour = {}, {}
    for r in rows:
        d = r["started_at"][:10]
        by_day[d] = by_day.get(d, 0) + r["minutes"]
        by_hour[int(r["started_at"][11:13])] = by_hour.get(int(r["started_at"][11:13]), 0) + r["minutes"]

    dates = sorted(by_day)
    streaks, run, prev = [], 0, None
    for d in dates:
        run = run + 1 if prev and (parse_iso(d) - parse_iso(prev)).days == 1 else 1
        streaks.append(run)
        prev = d
    best_streak = max(streaks) if streaks else 0
    today = date.today()
    cur_streak = 0
    d = today if today.isoformat() in by_day else today - timedelta(days=1)
    while d.isoformat() in by_day:
        cur_streak += 1
        d -= timedelta(days=1)

    weeks, months = [], []
    monday = today - timedelta(days=today.weekday())
    for i in range(7, -1, -1):
        ws = monday - timedelta(weeks=i)
        we = ws + timedelta(days=7)
        m = sum(v for k, v in by_day.items() if ws.isoformat() <= k < we.isoformat())
        weeks.append({"label": ws.strftime("%b %d"), "min": m})
    mstart = today.replace(day=1)
    for i in range(11, -1, -1):
        y, mo = mstart.year, mstart.month - i
        while mo <= 0:
            mo += 12
            y -= 1
        key = f"{y:04d}-{mo:02d}"
        months.append({"label": date(y, mo, 1).strftime("%b"), "min": sum(v for k, v in by_day.items() if k[:7] == key)})
    months = [m for m in months if m["min"] > 0]

    daily = conn.execute(
        "SELECT substr(p.started_at,1,10) AS d, "
        "CASE WHEN p.subject_id IS NOT NULL THEN COALESCE(g.name, 'learning') "
        "ELSE COALESCE(c.name,'Unallocated') END AS bucket, SUM(p.minutes) AS m "
        "FROM pomo_periods p "
        "LEFT JOIN categories c ON c.id=p.category_id "
        "LEFT JOIN tracker_subjects s ON s.id=p.subject_id "
        "LEFT JOIN tracker_groups g ON g.id=s.group_id "
        "WHERE substr(p.started_at,1,10)>=? GROUP BY d, bucket",
        ((date.today() - timedelta(days=6)).isoformat(),),
    ).fetchall()
    by_day = {}
    for r in daily:
        e = by_day.setdefault(r["d"], {"cats": {}, "total": 0})
        e["cats"][r["bucket"]] = e["cats"].get(r["bucket"], 0) + r["m"]
        e["total"] += r["m"]
    daily_categories = []
    for i in range(6, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        e = by_day.get(d) or {"cats": {}, "total": 0}
        daily_categories.append({
            "date": d,
            "label": date.fromisoformat(d).strftime("%a"),
            "total": e["total"],
            "cats": [{"name": k, "min": v} for k, v in sorted(e["cats"].items(), key=lambda kv: -kv[1])],
        })

    return {
        "total_min": total_min,
        "total_periods": len(rows),
        "days_accessed": len(dates),
        "streak_current": cur_streak,
        "streak_best": best_streak,
        "avg_per_focus_day": round(total_min / len(dates)) if dates else 0,
        "daily_categories": daily_categories,
        "weeks": weeks,
        "months": months,
        "hours": [{"h": h, "min": by_hour.get(h, 0)} for h in range(24)],
    }


# ---------------------------------------------------------------- day card (png)

FONT_DIR = "/usr/share/fonts/julietaula-montserrat-fonts"


def _hm(minutes: int) -> str:
    if not minutes:
        return "0m"
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"


def _card_font(size: int, bold: bool):
    from PIL import ImageFont
    name = "Montserrat-Bold.otf" if bold else "Montserrat-Medium.otf"
    try:
        return ImageFont.truetype(f"{FONT_DIR}/{name}", size)
    except Exception:
        try:
            return ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", size)
        except Exception:
            return ImageFont.load_default()


_RETRO_FONT_PATH = None


def _retro_font(size: int):
    """Archivo Black for retro cards, unpacked once from the embedded TTF."""
    global _RETRO_FONT_PATH
    import base64
    import os
    import tempfile
    from PIL import ImageFont
    if _RETRO_FONT_PATH is None:
        _RETRO_FONT_PATH = os.path.join(tempfile.gettempdir(), "grind-archivo-black.ttf")
        try:
            with open(_RETRO_FONT_PATH, "wb") as f:
                f.write(base64.b64decode(ARCHIVO_TTF_B64))
        except Exception:
            pass
    try:
        return ImageFont.truetype(_RETRO_FONT_PATH, size)
    except Exception:
        return _card_font(size, True)


def _card_stats(conn, started_cond: str, done_cond: str, arg):
    """Focused minutes, tasks done, and top category for a period filter."""
    focused = conn.execute(
        "SELECT COALESCE(SUM(minutes),0) FROM pomo_periods WHERE " + started_cond,
        (arg,),
    ).fetchone()[0]
    tasks_done = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE done_at IS NOT NULL AND " + done_cond,
        (arg,),
    ).fetchone()[0]
    top = conn.execute(
        "SELECT COALESCE(c.name,'Unallocated') AS name, SUM(p.minutes) AS m "
        "FROM pomo_periods p LEFT JOIN categories c ON c.id=p.category_id "
        "WHERE " + started_cond + " GROUP BY name ORDER BY m DESC LIMIT 1",
        (arg,),
    ).fetchone()
    return focused, tasks_done, top


def _card_small(tasks_done: int, top) -> str:
    small = f"{tasks_done} tasks"
    if top and top["m"]:
        small += f"  ·  {top['name']}"
    return small


def _card_render(big: str, small: str, retro: bool = False, label: str = "") -> bytes:
    """1080x1080 card. Classic: minimalist centered value. Retro: valve-1998 bronze frame."""
    import io
    from PIL import Image, ImageDraw

    W = H = 1080
    if not retro:
        img = Image.new("RGB", (W, H), "#161618")
        d = ImageDraw.Draw(img)
        f_big = _card_font(190, bold=True)
        f_small = _card_font(46, bold=False)

        def center(text, font, y, fill):
            w = d.textlength(text, font=font)
            d.text(((W - w) / 2, y), text, font=font, fill=fill)

        center(big, f_big, H / 2 - 155, "#eaeaec")
        center(small, f_small, H / 2 + 105, "#9b9b9e")
    else:
        img = Image.new("RGB", (W, H), "#0b0a08")
        d = ImageDraw.Draw(img)

        # bronze frame
        d.rectangle([28, 28, W - 28, H - 28], outline="#a85e00", width=6)

        # header band with fake vertical gradient
        band_top, band_bot = 34, 152
        for y in range(band_top, band_bot):
            t = (y - band_top) / (band_bot - band_top)
            r = int(0xE0 + (0xA8 - 0xE0) * t)
            g = int(0x82 + (0x5A - 0x82) * t)
            b = int(0x0F + (0x00 - 0x0F) * t)
            d.line([(34, y), (W - 34, y)], fill=(r, g, b))
        d.line([(34, band_top), (W - 34, band_top)], fill="#f2ae5c", width=2)
        d.line([(34, band_bot - 4), (W - 34, band_bot - 4)], fill="#5c3100", width=4)

        f_brand = _retro_font(42)
        f_label = _retro_font(26)
        d.text((64, (band_top + band_bot) / 2), "LOCK IN", font=f_brand, fill="#1c0e00", anchor="lm")
        if label:
            d.text((W - 64, (band_top + band_bot) / 2), label, font=f_label, fill="#3a1e00", anchor="rm")

        # big value with extruded shadow
        size = 170
        f_big = _retro_font(size)
        big_u = big.upper()
        while size > 60 and d.textlength(big_u, font=f_big) > W - 240:
            size -= 10
            f_big = _retro_font(size)
        cy = H / 2 - 60
        for off, col in ((6, "#000000"), (3, "#553000"), (0, "#e0912a")):
            d.text((W / 2 + off, cy + off), big_u, font=f_big, fill=col, anchor="mm")

        f_cap = _retro_font(38)
        d.text((W / 2, cy + 175), "F O C U S E D", font=f_cap, fill="#8a8478", anchor="mm")

        d.line([(170, cy + 240), (W - 170, cy + 240)], fill="#2c2820", width=3)

        f_small = _retro_font(40)
        d.text((W / 2, cy + 300), small, font=f_small, fill="#c4bfb4", anchor="mm")

        f_foot = _retro_font(20)
        d.text((W / 2, H - 90), "LOCK IN SOFTWARE  ·  EST. 1998", font=f_foot, fill="#45413a", anchor="mm")

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


MONTHS = ("january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december")


def card_png(conn, retro: bool = False) -> bytes:
    """1080x1080 minimalist day card: total focused, tasks done, top category."""
    focused, tasks_done, top = _card_stats(
        conn, "substr(started_at,1,10)=?", "substr(done_at,1,10)=?", today_str())
    return _card_render(_hm(focused), _card_small(tasks_done, top), retro=retro,
                        label=date.today().strftime("%d %b %Y").upper())


def card_png_week(conn, retro: bool = False) -> bytes:
    """1080x1080 minimalist week card: rolling last 7 days."""
    since = (date.today() - timedelta(days=6)).isoformat()
    focused, tasks_done, top = _card_stats(
        conn, "substr(started_at,1,10)>=?", "substr(done_at,1,10)>=?", since)
    return _card_render(_hm(focused), "last 7 days  ·  " + _card_small(tasks_done, top),
                        retro=retro, label="LAST 7 DAYS")


def card_png_month(conn, retro: bool = False) -> bytes:
    """1080x1080 minimalist month card: named calendar month, month-to-date."""
    now = date.today()
    focused, tasks_done, top = _card_stats(
        conn, "substr(started_at,1,7)=?", "substr(done_at,1,7)=?",
        f"{now.year:04d}-{now.month:02d}")
    return _card_render(_hm(focused), MONTHS[now.month - 1] + "  ·  " + _card_small(tasks_done, top),
                        retro=retro, label=MONTHS[now.month - 1].upper() + f" {now.year}")


# ---------------------------------------------------------------- embedded webfonts
# Embedded Archivo Black TTF (base64) for the retro PNG cards.
ARCHIVO_TTF_B64 = "AAEAAAASAQAABAAgRFNJRwAAAAEAAWNkAAAACEdERUYMLwwzAAFVcAAAAI5HUE9T9cbxvAABVgAAAAkSR1NVQqgqqSoAAV8UAAAETk9TLzJ3mYm1AAABqAAAAGBjbWFwyMKXPQAACJgAAAbiY3Z0IDLSAdYAAB2QAAAAgGZwZ209HI58AAAPfAAADW1nYXNwAAAAEAABVWgAAAAIZ2x5ZqGL54wAACFoAAEkTmhlYWQJStjJAAABLAAAADZoaGVhCTEGpAAAAWQAAAAkaG10eCb/Pt0AAAIIAAAGjmxvY2FCGfiSAAAeEAAAA1ZtYXhwAw4OPwAAAYgAAAAgbmFtZV+zwRYAAUW4AAAGAHBvc3QWe79rAAFLuAAACa1wcmVwz5RlaQAAHOwAAACjAAEAAAABAYlo0d5lXw889QAHA+gAAAAAzsROdwAAAADVq0Uz/xj+9QaoA70AAAAHAAIAAAAAAAAAAQAAA27/LgAABrr/GP8eBqgAAQAAAAAAAAAAAAAAAAAAAZ0AAQAAAaoAYQAHADIAAwACACoAPACLAAAAoQ1tAAMAAQADAowBkAAFAAACigJYAAAASwKKAlgAAAFeADIBPAAAAgsKAwICAgILBAAAAAcAAAAAAAAAAAAAAABVS1dOAEAAAPsCA27/LgAABAsBOCAAAJMAAAAAAhACsAAAACAAAwLuAAAAAAAAAU0AAAFNAAADCgAKAwoACgMKAAoDCgAKAwoACgMKAAoDCgAKAwoACgMKAAoDCgAKAwoACgPo//ED6P/xAwoASgMKAC0DCgAtAwoALQMKAC0DCgAtAwoALQMKAEoDCgABAwoASgMKAAEC0gBKAtIASgLSAEoC0gBKAtIASgLSAEoC0gBKAtIASgLSAEoC0gBKApsASgNBAC0DQQAtA0EALQNBAC0DQQAtA0EASgNBAAEDQQBKAYUAVAQEAFQBhQBUAYUACgGF//gBhQAJAYUAVAGFAAQBhQAlAYUAVAGF/+UCmwAXApsAFwNBAEoDQQBKApsASgKbAEoCmwBKApsASgKbAEoCmwAAA7AAPANBAEoDQQBKA0EASgNBAEoDLgBKA0EASgNBAC0DQQAtA0EALQNBAC0DQQAtA0EALQNBAC0DQQAtA0EALQNBAC0DQQAtA+gAIwLSAEoC0gBKA0EALQMKAEoDCgBKAwoASgMKAEoC0gArAtIAKwLSACsC0gArAtIAKwLSACsC0gAXAtIAFwLSABcC0gAXAtIAFwNBAEoDQQBKA0EASgNBAEoDQQBKA0EASgNBAEoDQQBKA0EASgNBAEoDQQBKAwoAEQPoAAUD6AAFA+gABQPoAAUD6AAFAwoABAMKAAcDCgAHAwoABwMKAAcDCgAHAtIAGALSABgC0gAYAtIAGAKbAB4CmwAeApsAHgKbAB4CmwAeApsAHgKbAB4CmwAeApsAHgKbAB4CmwAeA+gAHgPoAB4CmwA9ApsAJAKbACQCmwAkApsAJAKbACQCmwAkApsAJAKbACQDTAAkApsAJAKbACQCmwAkApsAJAKbACQCmwAkApsAJAKbACQCmwAkApsAJAKbACQBhQAIApsABQKbAAUCmwAFApsABQKbAAUCmwA8Apv//gKb/9UBTQBDAU0AQwFNAEMBTf/uAU3/2wFN/+wBTf/oApwAQwFNAAgBTQBDAU3/yAFN/9cBTf/XApsAPAKbADwCmwA8AU0AQwFNAEMB9ABDAU0AQwIWAEMBTf/xA+gAPAKbADwCmwA8A1oAUQKbADwCmwA8AqkAQwKbADwCmwAkApsAJAKbACQCmwAkApsAJAKbACQCmwAkApsAJAKbACQCmwAkApsAJAPoAB0CmwA9ApsAPQKbACQBvAA8AbwANQG8ABMBvAA8AmMAIAJjACACYwAgAmMAIAJjACACYwAgApsAQwGGAAgBvAAbAbwAGwJxABsBvAAbAbwAGwKbADoCmwA6ApsAOgKbADoCmwA6ApsAOgKbADoCmwA6ApsAOgKbADoCmwA6AmMAAAOwAAADsAAAA7AAAAOwAAADsAAAApsABAJjAAACYwAAAmMAAAJjAAACYwAAAiwAFAIsABQCLAAUAiwAFAKbAAgCmwAIAZAADAGQAAwBtAArAwoAGgNWAB8CmwA6AsMAFgKbACkCmwBpApsAMQKbACoCmwAXApsAJwKbACkCmwAtApsAKQKbACQBkAAeAZAAEAGQAA8Ap/9OA+gAJgPoACYD6AAcA+gAJgPoACAD6AAmA+gAOQIsAF8BFv/+AU0ARwH0AFgBTQA9AU0APAPoAD0BTQAvAlwALwFNAC8ClAAUAU0APQJjACUCYwAuAfQAOwEWADkBTQA8ARb//gH0AAAB9AAAAfQAAQGFAB4BhQAoAYUAQgGFAEIBhQA3AYUAIgLuAAAD6AAAAfQAAAFNACcBTQAnApsAPAKbAD0BTQALAU0ADAH0ACwB9AApAfQALAEWACwBFgAsARYAMAEWACwBTQAAApsAJAKUAEUCmwBFApsAFwKbACQDQQBKApsAHwa6ADwCmwAfApsAAgFNAEcAbv9WAlgAPgJYAKUCWADZApQAQgKUAEIClABEApQAQgKUAEIClABCApQARAKUAEMClABCApQAQgKUAEIClAAnApQAJwKUADICRwAJAs8ARQEP//8DVgAfAwoAGgM3ABUCyQAdAiX/3wKbADoB9AAJA9MAsQPoAC4D6AAAApQAOAH0AC4D6AAqAfQALgPoACoD6AAqAfQALgH0AC4DCgCCAuQAFAN5AEsDUgBtApsANgMgACwDIAAsA7YAEgPoAC4BkAAyARYATQEWAE4CSgA1ApsARQKbAEUCWAAsBUEASgKUADcCWABLATAAOwIMADsDeQBDAAD/GAH0AAABTQAe/+7/3QAv/93/7ABbABf/8wAJAEgAOv/IAAAAAAAAAAMAAAADAAAAHAABAAAAAATqAAMAAQAAABwABATOAAAAeABAAAUAOAAAAA0ALwA5AH4BfwGSAf8CGwLHAskC3QMmA5QDqQO8A8AehR7zIBUgHiAiICYgMCAzIDogPCA+IEQgfyCkIKcgrCEFIRMhFiEiISYhLiFeIZUhqCICIgYiDyISIhUiGiIfIikiKyJIImEiZSMCIxAjISXK+wL//wAAAAAADQAgADAAOgCgAZIB+gIYAsYCyQLYAyYDlAOpA7wDwB6AHvIgEyAXICAgJiAwIDIgOSA8ID4gRCB/IKMgpyCsIQUhEyEWISIhJiEuIVshkCGoIgIiBiIPIhEiFSIZIh4iKSIrIkgiYCJkIwIjECMgJcr7Af//AAH/9QAAAOEAAAAA/8QAAAAAAAD+0gAA/nT9ef1l/VP9UAAAAAAAAAAAAADhBuFL4WXhD+Dy4Pzg2uCN4LTgsuCp4IfgfeB+4GngTOBl38cAAN/b33bfbd9lAADfSAAAAADfR99G3yQAAAAA3pTebAAA27oGBwABAAAAAAB0AAAAkAEYAAAC1ALeAuQAAALkAAAAAAAAAAAAAALkAu4C8AL0AwIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC4gAAAAAAAAAAAuQAAALkAuYAAAAAAAAC4gLkAAAAAALiAAAAAAAAAAMBLQE0ATABVAF6AYYBNQE/AUABJgFhASsBRAExATcBKgE2AWgBZQFnATIBhQAEABEAEgAYABwAJgAnACwALwA6ADwAPgBEAEUASwBXAFkAWgBeAGQAaQB0AHUAegB7AIABPQEnAT4BlQE4AaMAhACRAJIAmACcAKYApwCsAK8AugC8AL8AxQDGAM0A2QDbANwA4ADoAO0A+AD5AP4A/wEEATsBjgE8AW0BUQEvAVIBWgFTAVsBjwGIAaEBiQEKAUYBbgFFAYoBpQGNAWsBHAEdAZwBdwGHASgBnwEbAQsBRwEgAR8BIQEzAAkABQAHAA4ACAAMAA8AFQAjAB0AIAAhADYAMQAzADQAGQBKAFAATABOAFUATwFjAFMAbgBqAGwAbQB8AFgA5gCJAIUAhwCOAIgAjACPAJUAowCdAKAAoQC1ALEAswC0AJkAzADSAM4A0ADXANEBZADVAPIA7gDwAPEBAADaAQIACgCKAAYAhgALAIsAEwCTABYAlgAXAJcAFACUABoAmgAbAJsAJACkAB4AngAiAKIAJQClAB8AnwApAKkAKACoACsAqwAqAKoALgCuAC0ArQA5ALkANwC3ADIAsgA4ALgANQCwADAAtgA7ALsAPQC9AL4APwDAAEEAwgBAAMEAQgDDAEMAxABGAMcASADKAEcAyQDIAEkAywBSANQATQDPAFEA0wBWANgAWwDdAF0A3wBcAN4AXwDhAGIA5ABhAOMAYADiAGcA6wBmAOoAZQDpAHMA9wBwAPQAawDvAHIA9gBvAPMAcQD1AHcA+wB9AQEAfgCBAQUAgwEHAIIBBgDnAA0AjQAQAJAAVADWAGMA5QBoAOwBoAGeAZ0BogGnAaYBqAGkAHkA/QB2APoAeAD8AH8BAwFDAUIBQQE5AU0BTwFQAU4BSwFMAUoBkQGSASkBgAF9AX4BfwGBAYIBdQFiAVwBdgFvAXkBZgFeAWoBaQFgAV8ABgH4AAAACQD3AAMAAAAAAAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADAS0BNAEwAVQBegGGATUBPwFAASYBYQErAUQBMQE3AREBEgETARQBFQEWARcBGAEZARoBKgE2AWgBZQFnATIBhQAEABEAEgAYABwAJgAnACwALwA6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AFMAbwBmAHQAdwBhAHIAZQAgAGkAcwAgAGwAaQBjAGUAbgBzAGUAZAAgAHUAbgBkAGUAcgAgAHQAaABlACAAUwBJAEwAIABPAHAAZQBuACAARgBvAG4AdAAgAEwAaQBjAGUAbgBzAGUALAAgAFYAZQByAHMAaQBvAG4AIAAxAC4AMQAuACAAVABoAGkAcwAgAGwAaQBjAGUAbgBzAGUAIABpAHMAIABhAHYAYQBpAGwAYQBiAGwAZQAgAHcAaQB0AGgAIABhACAARgBBAFEAIABhAHQAOgAgAGgAdAB0AHAAOgAvAC8AcwBjAHIAaQBwAHQAcwAuAHMAaQBsAC4AbwByAGcALwBPAEYATABoAHQAdABwADoALwAvAHMAYwByAGkAcAB0AHMALgBzAGkAbAAuAG8AcgBnAC8ATwBGAEwAAgAAAAAAAP+1ADIAAAAAAAAAAAAAAAAAAAAAAAAAAAGqAAABAgACAAMAJADJAQMAxwBiAK0BBAEFAGMBBgCuAJABBwAlACYA/QD/AGQBCAEJACcA6QEKAQsAKABlAQwBDQDIAMoBDgDLAQ8BEAApACoA+AERARIBEwArARQBFQAsARYAzAEXAM0AzgD6AM8BGAEZARoALQEbAC4BHAAvAR0BHgEfASAA4gAwADEBIQEiASMBJABmADIA0AElANEAZwDTASYBJwCRASgArwCwADMA7QA0ADUBKQEqASsANgEsAOQA+wEtAS4ANwEvATABMQEyADgA1AEzANUAaADWATQBNQE2ATcBOAA5ADoBOQE6ATsBPAA7ADwA6wE9ALsBPgA9AT8A5gFAAEQAaQFBAGsAbABqAUIBQwBuAUQAbQCgAUUARQBGAP4BAABvAUYBRwBHAOoBSAEBAEgAcAFJAUoAcgBzAUsAcQFMAU0ASQBKAPkBTgFPAVAASwFRAVIATADXAHQBUwB2AHcAdQFUAVUBVgFXAE0BWABOAVkBWgBPAVsBXAFdAV4A4wBQAFEBXwFgAWEBYgFjAHgAUgB5AWQAewB8AHoBZQFmAKEBZwB9ALEAUwDuAFQAVQFoAWkBagBWAWsA5QD8AWwBbQCJAW4AVwFvAXABcQFyAFgAfgFzAIAAgQB/AXQBdQF2AXcBeABZAFoBeQF6AXsBfABbAFwA7AF9ALoBfgBdAX8A5wGAAMAAwQCdAJ4BgQGCAYMBhACbABMAFAAVABYAFwAYABkAGgAbABwBhQGGAYcAvAD0APUA9gGIAYkBigGLAA0APwDDAIcAHQAPAKsABAGMAKMABgARACIAogAFAAoAHgASAEIBjQGOAF4AYAA+AEAACwAMAY8AswCyABABkACpAKoAvgC/AMUAtAC1ALYBkQC3AMQBkgCEAL0ABwGTAKYA9wGUAZUAhQCWAZYBlwGYAZkBmgAOAO8A8AC4ACAAjwAhAB8AlQCUAJMApwBhAKQAkgGbAJwBnAGdAJoAmQClAZ4AmAGfAAgAxgGgAaEBogGjAaQBpQGmAacAuQAjAAkAiACGAIsAigCMAagAgwBfAOgBqQCCAMIBqgGrAEEBrAGtAa4BrwGwAbEAjQDbAOEA3gDYAI4A3ABDAN8A2gDgAN0A2QGyBE5VTEwGQWJyZXZlB0FtYWNyb24HQW9nb25lawpBcmluZ2FjdXRlB0FFYWN1dGULQ2NpcmN1bWZsZXgKQ2RvdGFjY2VudAZEY2Fyb24GRGNyb2F0BkVicmV2ZQZFY2Fyb24KRWRvdGFjY2VudAdFbWFjcm9uB0VvZ29uZWsLR2NpcmN1bWZsZXgMR2NvbW1hYWNjZW50Ckdkb3RhY2NlbnQESGJhcgtIY2lyY3VtZmxleAJJSgZJYnJldmUHSW1hY3JvbgdJb2dvbmVrBkl0aWxkZQtKY2lyY3VtZmxleAxLY29tbWFhY2NlbnQGTGFjdXRlBkxjYXJvbgxMY29tbWFhY2NlbnQETGRvdAZOYWN1dGUGTmNhcm9uDE5jb21tYWFjY2VudANFbmcGT2JyZXZlDU9odW5nYXJ1bWxhdXQHT21hY3JvbgtPc2xhc2hhY3V0ZQZSYWN1dGUGUmNhcm9uDFJjb21tYWFjY2VudAZTYWN1dGULU2NpcmN1bWZsZXgMU2NvbW1hYWNjZW50BFRiYXIGVGNhcm9uB3VuaTAxNjIHdW5pMDIxQQZVYnJldmUNVWh1bmdhcnVtbGF1dAdVbWFjcm9uB1VvZ29uZWsFVXJpbmcGVXRpbGRlBldhY3V0ZQtXY2lyY3VtZmxleAlXZGllcmVzaXMGV2dyYXZlC1ljaXJjdW1mbGV4BllncmF2ZQZaYWN1dGUKWmRvdGFjY2VudAZhYnJldmUHYW1hY3Jvbgdhb2dvbmVrCmFyaW5nYWN1dGUHYWVhY3V0ZQtjY2lyY3VtZmxleApjZG90YWNjZW50BmRjYXJvbgZlYnJldmUGZWNhcm9uCmVkb3RhY2NlbnQHZW1hY3Jvbgdlb2dvbmVrC2djaXJjdW1mbGV4DGdjb21tYWFjY2VudApnZG90YWNjZW50BGhiYXILaGNpcmN1bWZsZXgGaWJyZXZlAmlqB2ltYWNyb24HaW9nb25lawZpdGlsZGULamNpcmN1bWZsZXgMa2NvbW1hYWNjZW50DGtncmVlbmxhbmRpYwZsYWN1dGUGbGNhcm9uDGxjb21tYWFjY2VudARsZG90Bm5hY3V0ZQtuYXBvc3Ryb3BoZQZuY2Fyb24MbmNvbW1hYWNjZW50A2VuZwZvYnJldmUNb2h1bmdhcnVtbGF1dAdvbWFjcm9uC29zbGFzaGFjdXRlBnJhY3V0ZQZyY2Fyb24McmNvbW1hYWNjZW50BnNhY3V0ZQtzY2lyY3VtZmxleAxzY29tbWFhY2NlbnQFbG9uZ3MEdGJhcgZ0Y2Fyb24HdW5pMDE2Mwd1bmkwMjFCBnVicmV2ZQ11aHVuZ2FydW1sYXV0B3VtYWNyb24HdW9nb25lawV1cmluZwZ1dGlsZGUGd2FjdXRlC3djaXJjdW1mbGV4CXdkaWVyZXNpcwZ3Z3JhdmULeWNpcmN1bWZsZXgGeWdyYXZlBnphY3V0ZQp6ZG90YWNjZW50B3VuaTIwN0YHdW5pMDM5NAd1bmkwM0E5B3VuaTAzQkMHdW5pMDBCOQd1bmkwMEIyB3VuaTAwQjMJb25lZWlnaHRoDHRocmVlZWlnaHRocwtmaXZlZWlnaHRocwxzZXZlbmVpZ2h0aHMJZXhjbGFtZGJsDXVuZGVyc2NvcmVkYmwHdW5pMjAzRQd1bmkyMDE1B3VuaTAwQUQNcXVvdGVyZXZlcnNlZAd1bmkwMEEwBEV1cm8EbGlyYQZwZXNldGEHdW5pMjIxOQd1bmkyMjE1C2VxdWl2YWxlbmNlCmludGVncmFsYnQKaW50ZWdyYWx0cAxpbnRlcnNlY3Rpb24HdW5pMjEyNgd1bmkyMjA2B3VuaTAwQjUKb3J0aG9nb25hbA1yZXZsb2dpY2Fsbm90B2Fycm93dXAKYXJyb3dyaWdodAlhcnJvd2Rvd24JYXJyb3dsZWZ0CWFycm93Ym90aAlhcnJvd3VwZG4MYXJyb3d1cGRuYnNl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"

# 1998 valve site assets (web.archive.org capture of valvesoftware.com), embedded.
CERAMIC_GIF_B64 = "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"
ARROW_GIF_B64 = "R0lGODlhCQAMAID/AKVKGMDAwCH5BAEAAAEALAAAAAAJAAwAQAISjI+ggKEN22KJxmjl06tm8xkFADs7ADs="


# ---------------------------------------------------------------- pages

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>lock in overlay</title>
<style>
html, body {
    margin: 0;
    background: #281800 url("__CERAMIC__") repeat;
    font-family: verdana, "trebuchet ms", arial, helvetica, sans-serif;
    color: #d8a248;
  }
  #card {
    margin: 8px;
    padding: 0;
    background: transparent;
    border: none;
    border-radius: 0;
    box-shadow: none;
    overflow: hidden;
    height: calc(100% - 16px);
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
  }
  .top {
    flex: none;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    padding: 0 2px 8px;
    border-bottom: 1px solid #4a2a00;
  }
  .top .brand2 { font-size: 15px; font-weight: 700; letter-spacing: 1px; color: #d8a248; text-shadow: none; }
  .top .tstats { font-size: 11px; font-style: italic; color: #a87830; }

  .pomo {
    text-align: center;
    padding: 18px 0 12px;
    margin-bottom: 4px;
  }
  .ptime {
    font-size: 44px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    letter-spacing: 1px;
    line-height: 1.1;
    color: #d8a248;
    text-shadow: none;
  }
  .pomo.running .ptime { color: #f0d0a0; }
  .pomo.paused .ptime { color: #d8a248; }
  .pomo.ended .ptime { color: #bb0000; animation: blink 1.1s ease-in-out infinite; }
  @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  .pphase {
    font-size: 11px;
    font-style: italic;
    color: #a87830;
    letter-spacing: 1px;
    margin-top: 3px;
  }
  .pomo.running .pphase { color: #f0d0a0; }
  .pomo.ended .pphase { color: #bb0000; }

  .listwrap { flex: 1 1 auto; overflow: hidden; }
  ul { list-style: none; margin: 0; padding: 0; }
  li {
    display: flex;
    align-items: baseline;
    gap: 9px;
    padding: 3px 0;
    font-size: 15px;
    line-height: 1.4;
  }
  .box {
    flex: none;
    width: 12px;
    text-align: left;
    font-size: 14px;
  }
  .doing .box {
    color: transparent;
    background: url("__ARROW__") no-repeat left center;
    height: 14px;
  }
  .doing .label { color: #f0d0a0; font-weight: 700; }
  .todo .box { color: #d8a248; }
  .todo .label { color: #d8a248; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
  .doing .label { animation: pulse 1.6s ease-in-out infinite; }

  .pfoot {
    flex: none;
    padding-top: 8px;
    border-top: none;
    text-align: center;
  }
  .pfocus { font-size: 13px; font-weight: 700; color: #f0d0a0; letter-spacing: 0; text-shadow: none; }
  .pdone { display: flex; align-items: center; justify-content: center; gap: 10px; margin-top: 6px; }
  .pdone .seg { flex: 1; height: 2px; background: #4a2a00; position: relative; box-shadow: none; }
  .pdone .seg i { position: absolute; left: 0; top: 0; bottom: 0; background: #d8a248; }
  .pdone b { font-size: 11px; font-weight: 700; color: #a87830; font-variant-numeric: tabular-nums; }
  </style>
</head>
<body>
<div class="card" id="card"></div>
<script>
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  let data = null, pomo = null, deadline = 0, wasEnded = null, rang = null;

  function fmtClock(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    return Math.floor(s / 3600) + ":" + String(Math.floor(s / 60) % 60).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }
  function fmtHM(min) {
    if (!min) return "0m";
    const h = Math.floor(min / 60), m = min % 60;
    return (h ? h + "h" : "") + (m ? (h ? " " : "") + m + "m" : (h ? "" : "0m"));
  }

  let actx = null;
  function bell() {
    try {
      actx = actx || new (window.AudioContext || window.webkitAudioContext)();
      if (actx.state === "suspended") actx.resume();
      const t0 = actx.currentTime + 0.01;
      [0, 0.30, 0.60].forEach((off, i) => {
        const o = actx.createOscillator(), g = actx.createGain();
        o.type = "sine";
        o.frequency.value = i === 2 ? 1318.5 : 880;
        g.gain.setValueAtTime(0.0001, t0 + off);
        g.gain.exponentialRampToValueAtTime(0.2, t0 + off + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + off + 0.24);
        o.connect(g); g.connect(actx.destination);
        o.start(t0 + off); o.stop(t0 + off + 0.27);
      });
    } catch (e) {}
  }

  function pomoHtml() {
    if (!pomo) return "";
    const cls = pomo.ended ? "ended" : pomo.running ? "running" : "paused";
    return `<div class="pomo ${cls}">
      <div class="ptime">${fmtClock(pomo.running ? Math.max(0, deadline - Date.now()) : pomo.remaining_ms)}</div>
      <div class="pphase">${pomo.ended ? pomo.phase + " done" : pomo.phase}</div>
    </div>`;
  }

  function pfootHtml() {
    if (!pomo) return "";
    const total = data.stats.total, done = data.stats.done;
    const pct = total ? Math.round(100 * done / total) : 0;
    const lw = Math.min(pct, 50) * 2, rw = Math.max(0, pct - 50) * 2;
    return `<div class="pfocus">${fmtHM(pomo.today.focused_min)}</div>
      <div class="pdone"><span class="seg"><i style="width:${lw}%"></i></span><b>${done}/${total}</b><span class="seg"><i style="width:${rw}%"></i></span></div>`;
  }

  function renderTimer() {
    const el = document.querySelector(".ptime");
    if (!el || !pomo) return;
    const ms = pomo.running ? Math.max(0, deadline - Date.now()) : pomo.remaining_ms;
    el.textContent = fmtClock(ms);
    if (pomo.running && ms <= 0 && !rang) { rang = true; bell(); }
  }

  async function tick() {
    try {
      const [td, tp] = await Promise.all([fetch("/api/todo"), fetch("/api/pomodoro")]);
      data = await td.json();
      const prevEnded = wasEnded;
      pomo = await tp.json();
      wasEnded = pomo.ended;
      rang = false;
      deadline = Date.now() + pomo.remaining_ms;
      if (pomo.ended && prevEnded === false) bell();
      const pct = data.stats.total ? Math.round(100 * data.stats.done / data.stats.total) : 0;
      const shown = data.items
        .filter(i => i.state !== "done")
        .sort((a, b) => (b.state === "doing") - (a.state === "doing"))
        .slice(0, 4);
      let html = `
        <div class="top"><span class="brand2">lock tf in</span><span class="tstats">${data.stats.done} / ${data.stats.total} done &middot; ${pct}%</span></div>
        ${pomoHtml()}
        <div class="listwrap"><ul>` + shown.map(i =>
          `<li class="${i.state}"><span class="box">&middot;</span><span class="label">${esc(i.text)}</span></li>`
        ).join("") + `</ul></div>
        <div class="pfoot">${pfootHtml()}</div>`;
      document.getElementById("card").innerHTML = html;
    } catch (e) { /* keep last render */ }
  }
  tick();
  setInterval(tick, 1500);
  setInterval(renderTimer, 250);
</script>
</body>
</html>"""

MANAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>lock in</title>
<style>
:root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 20px 15% 40px;
    background: #281800 url("__CERAMIC__") repeat;
    color: #d8a248;
    font-family: verdana, "trebuchet ms", arial, helvetica, sans-serif;
    font-size: 13px;
  }
  a { color: #bb0000; }
  a:visited { color: #880000; }
  a:active, a:hover { color: #ff0000; }

  /* ---------- masthead: plain text ---------- */
  #mast { display: flex; align-items: baseline; gap: 12px; padding: 0 0 4px; }
  .brand { font-size: 22px; font-weight: 700; color: #d8a248; letter-spacing: 1px; }
  .tag { font-size: 10px; color: #a87830; font-style: italic; }
  .tag b { color: #a87830; font-weight: 400; }
  .ver { margin-left: auto; font-size: 10px; color: #a87830; text-align: right; line-height: 1.5; }

  /* ---------- nav: text links ---------- */
  .nav { margin: 6px 0 38px; }
  .nlinks button {
    background: none; border: none; padding: 0; margin-right: 14px; cursor: pointer;
    color: #bb0000; font-family: inherit; font-size: 13px; text-decoration: underline;
  }
  .nlinks button:hover { color: #ff0000; }
  .nlinks button.cur { color: #f0d0a0; font-weight: 700; text-decoration: none; cursor: default; }

  /* ---------- section headers: plain bold text ---------- */
  .btitle {
    display: flex; align-items: baseline; justify-content: space-between;
    margin: 0 0 6px; padding: 0;
    background: none; border: none; box-shadow: none;
    color: #d8a248; font-size: 17px; font-weight: 700; letter-spacing: 0.5px;
    text-transform: none; text-shadow: none;
  }
  .btitle .rt { font-size: 11px; font-weight: 400; font-style: italic; letter-spacing: 0; color: #a87830; }

  /* ---------- sections: no boxes ---------- */
  .card {
    background: none; border: none; box-shadow: none; border-radius: 0;
    padding: 0; margin: 0 0 38px;
  }
  .hint { color: #a87830; font-size: 12px; font-style: italic; margin-bottom: 10px; }
  .ctitle { font-size: 13px; font-weight: 700; color: #d8a248; margin: 20px 0 6px; }
  .st, .gmeta, .gpct { color: #a87830; font-size: 11px; font-style: italic; }
  .sep { color: #8a5a10; }

  /* ---------- buttons: plain links ---------- */
  .tool, button.std {
    background: none; border: none; padding: 0; cursor: pointer;
    color: #bb0000; font-family: inherit; font-size: 13px;
    text-decoration: underline; text-transform: none; letter-spacing: 0;
    text-shadow: none; box-shadow: none;
  }
  .tool:hover, button.std:hover { color: #ff0000; }
  .tool.pri { font-weight: 700; }
  .tool.danger { color: #bb0000; }
  .tool.danger:hover { color: #ff0000; }
  .tool.addreveal { font-size: 15px; }

  /* ---------- inputs: period form fields ---------- */
  .editor input[type=text], .editor textarea, .tinput, textarea.ta,
  .addsub input[type=text], .tedit input[type=text], .tedit textarea,
  .addbar input, .catrow input[type=text] {
    width: 100%; box-sizing: border-box;
    background: #1a0e00;
    border: 1px solid; border-color: #000 #6a4a10 #6a4a10 #000;
    border-radius: 0;
    color: #d8a248;
    padding: 4px 7px; font-size: 13px; font-family: inherit; margin-bottom: 6px;
    box-shadow: none; outline: none;
  }
  .editor input[type=text]:focus, .editor textarea:focus, .tinput:focus, textarea.ta:focus,
  .addsub input[type=text]:focus, .tedit input[type=text]:focus, .tedit textarea:focus,
  .addbar input:focus, .catrow input[type=text]:focus {
    border-color: #000 #bb0000 #bb0000 #000;
    color: #f0d0a0;
  }
  select.tinput { appearance: auto; }
  .editor textarea, .tedit textarea { min-height: 60px; resize: vertical; }
  textarea.ta { min-height: 110px; line-height: 1.6; }
  .editor label, .lbl {
    font-size: 12px; color: #a87830; font-style: italic; font-weight: 400;
    display: block; margin-bottom: 2px; text-transform: none; letter-spacing: 0; text-shadow: none;
  }

  /* ---------- task list: indented text with middots ---------- */
  .task { border: none; margin: 0; }
  .task[draggable="true"] { cursor: default; }
  .row {
    display: flex; align-items: baseline; gap: 9px;
    padding: 2.5px 0; margin: 0 0 0 16px;
    background: none;
  }
  .task.alt .row { background: none; }
  #list .task:hover .row { background: none; }
  .glyph {
    flex: none; width: 12px; text-align: left; cursor: pointer; user-select: none;
    font-family: inherit; font-weight: 400; font-size: 14px; line-height: 1;
    background: none; border: none; animation: none;
  }
  .todo .glyph { color: #d8a248; }
  .doing .glyph {
    color: transparent;
    background: url("__ARROW__") no-repeat left center;
    width: 12px; height: 14px;
  }
  .done .glyph, .paused .glyph, .dropped .glyph { color: #8a6228; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
  .ttext { flex: 1; font-size: 13px; cursor: pointer; text-shadow: none; }
  .todo .ttext { color: #d8a248; }
  .doing .ttext { color: #f0d0a0; font-weight: 700; }
  .done .ttext { color: #8a6228; text-decoration: line-through; }
  .hasnotes { font-size: 11px; color: #a87830; margin-left: 5px; font-style: italic; }
  .delbtn { background: none; border: none; color: #8a6228; cursor: pointer; font-size: 12px; opacity: 0.5; padding: 0; }
  .task:hover .delbtn, table.t tr:hover td .delbtn { opacity: 1; }
  .delbtn:hover { color: #ff0000; }
  .editor { padding: 2px 0 10px 37px; display: none; }
  .editor.open { display: block; }
  .tedit { display: grid; grid-template-columns: 1fr; gap: 4px; }
  .tedit .full { grid-column: 1 / -1; }
  .saved { color: #a87830; font-size: 12px; font-style: italic; margin-left: 8px; opacity: 0; transition: opacity .4s; }
  .saved.show { opacity: 1; }

  .addbar { display: flex; gap: 8px; margin: 8px 0 0 16px; }
  .addbar input { flex: 1; margin-bottom: 0; }
  .addreveal { margin: 6px 0 0 16px; }
  .empty { color: #a87830; padding: 6px 0 6px 16px; font-size: 13px; font-style: italic; }
  .donehead {
    display: block; padding: 3px 0 3px 16px; cursor: pointer; user-select: none;
    color: #bb0000; font-size: 13px; background: none;
    text-transform: none; letter-spacing: 0; font-weight: 400;
  }
  .donehead:hover { color: #ff0000; }
  .donehead .arrow { font-size: 11px; }
  .catrow { display: flex; gap: 6px; margin-top: 6px; align-items: center; }
  .catrow input[type=text] { flex: 1; min-width: 0; margin-bottom: 0; }
  .catrow .tool { flex: none; }

  /* ---------- day notes: plain italic text ---------- */
  #notebox {
    width: 100%; box-sizing: border-box; min-height: 110px;
    background: transparent; border: none; outline: none; resize: vertical;
    box-shadow: none;
    color: #d8a248; font-size: 13px; line-height: 1.8;
    font-family: inherit; font-style: italic; padding: 0 0 0 16px;
  }
  #notebox::placeholder { color: #8a5a10; font-style: italic; }
  .noterow { display: flex; justify-content: flex-start; margin: 2px 0 0 16px; }

  /* ---------- timer: plain text ---------- */
  .tbox {
    border: none; background: none; box-shadow: none; border-radius: 0;
    min-height: 46vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 4px; padding: 20px;
  }
  .tphase { font-size: 12px; font-weight: 700; color: #a87830; letter-spacing: 2px; text-transform: lowercase; text-shadow: none; }
  .tctrl { display: flex; gap: 16px; margin-top: 14px; flex-wrap: wrap; justify-content: center; }
  .tsum { margin-top: 14px; font-size: 13px; color: #a87830; }
  .tsum b { color: #f0d0a0; font-weight: 700; }
  .ttime { font-size: 44px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: 1px; line-height: 1.1; color: #d8a248; text-shadow: none; }
  .tbox.run .ttime { color: #f0d0a0; text-shadow: none; }
  .tbox.end .ttime { color: #bb0000; animation: blink 1.1s ease-in-out infinite; }
  @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
  details.setdrop { margin-top: 14px; }
  details.setdrop summary {
    cursor: pointer; text-align: center; color: #bb0000; font-size: 13px;
    text-decoration: underline; padding: 4px; border: none; background: none;
    box-shadow: none; list-style: none; font-family: inherit; text-transform: none; letter-spacing: 0;
  }
  details.setdrop summary::-webkit-details-marker { display: none; }
  details.setdrop summary:hover { color: #ff0000; }
  details.setdrop[open] summary { color: #f0d0a0; text-decoration: none; }
  .setbody { padding: 14px 4px 4px; display: grid; grid-template-columns: 1fr 1fr; gap: 0 24px; }
  .setcol { min-width: 0; }
  .setcol .ctitle { margin-top: 0; }
  .prow { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; }
  .tinput { width: auto; margin-bottom: 0; }
  .meta { color: #a87830; font-size: 13px; line-height: 1.7; }
  .meta b { color: #f0d0a0; font-weight: 700; }

  /* ---------- log: plain table ---------- */
  table.t { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.t th {
    text-align: left; color: #a87830; font-size: 12px; font-weight: 700;
    padding: 3px 8px 3px 0; border: none; text-transform: none; letter-spacing: 0; text-shadow: none; font-style: italic;
  }
  table.t td { padding: 2.5px 8px 2.5px 0; border: none; color: #d8a248; }
  table.t td.num, table.t th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .imp { color: #a87830; font-size: 12px; margin-top: 8px; white-space: pre-line; }

  /* ---------- collapsed sections (log) ---------- */
  details.logdrop { margin-bottom: 26px; }
  details.logdrop > summary {
    display: block; list-style: none; cursor: pointer;
    margin: 0; padding: 0; background: none; border: none; box-shadow: none;
    color: #bb0000; font-size: 17px; font-weight: 700; letter-spacing: 0.5px;
    text-transform: none; text-shadow: none;
  }
  details.logdrop > summary::-webkit-details-marker { display: none; }
  details.logdrop > summary:hover { color: #ff0000; }
  details.logdrop[open] > summary { margin-bottom: 10px; }
  details.logdrop > .inner { padding: 0 0 0 16px; }

  /* ---------- summary: plain text ---------- */
  .statsgrid { display: inline; margin: 0; }
  .stat {
    display: inline; background: none; border: none; box-shadow: none;
    padding: 0; margin: 0; text-align: left;
  }
  .stat b { display: inline; font-size: 13px; font-weight: 700; color: #f0d0a0; font-family: inherit; text-shadow: none; }
  .stat span { display: inline; font-size: 11px; color: #a87830; font-style: italic; margin: 0 2px 0 4px; text-transform: none; letter-spacing: 0; }
  .stat::after { content: " ·  "; color: #8a5a10; }
  .statline { margin: 3px 0 0 16px; font-size: 13px; }
  .statline b { color: #f0d0a0; font-weight: 700; }
  .taskmenu { position: fixed; top: 0; left: 0; right: auto; z-index: 100; }

  /* ---------- tracker: indented text ---------- */
  .group { border: none; background: none; box-shadow: none; margin: 0 0 22px; }
  .ghead { display: flex; align-items: baseline; gap: 8px; padding: 0; cursor: pointer; user-select: none; background: none; }
  .ghead:hover { background: none; }
  .ghead .delbtn { opacity: 0; }
  .ghead:hover .delbtn { opacity: 1; }
  .garrow { font-size: 11px; color: #a87830; flex: none; }
  .gname { font-weight: 700; font-size: 14px; color: #d8a248; text-transform: none; letter-spacing: 0; text-shadow: none; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 260px; }
  .gname:hover { color: #f0d0a0; }
  .gbar { display: none; }
  .gpct { flex: none; }
  .gmeta { flex: none; }
  .gbody { padding: 0; border: none; }
  .subj { border: none; }
  .subj:last-of-type { border: none; }
  .srow { display: flex; align-items: baseline; gap: 8px; padding: 2.5px 0; margin: 0; background: none; }
  .subj.alt .srow { background: none; }
  .subj:hover .srow { background: none; }
  .subj .glyph { width: 12px; font-size: 13px; }
  .star { background: none; border: none; cursor: pointer; color: #8a5a10; font-size: 13px; padding: 0; width: 14px; flex: none; line-height: 1; opacity: 0.6; }
  .star.on { color: #f0d0a0; opacity: 1; text-shadow: none; }
  .srow:hover .star { opacity: 1; }
  .star:hover { color: #f0d0a0; }
  .kebab { background: none; border: none; cursor: pointer; color: #a87830; font-size: 13px; padding: 0 2px; flex: none; line-height: 1; opacity: 0.6; }
  .srow:hover .kebab { opacity: 1; }
  .kebab:hover { color: #ff0000; }
  .sname { font-size: 13px; cursor: pointer; flex: 1 1 140px; min-width: 120px; font-weight: 400; text-shadow: none; }
  .todo .sname { color: #d8a248; }
  .doing .sname { color: #f0d0a0; font-weight: 700; }
  .paused .sname { color: #a87830; font-style: italic; }
  .done .sname { color: #8a6228; text-decoration: line-through; }
  .dropped .sname { color: #6a4a10; text-decoration: line-through; }
  .slink { color: #bb0000; font-size: 11px; margin-left: 5px; }
  .slink:hover { color: #ff0000; }
  .badge {
    background: none; border: none; padding: 0; font-weight: 400;
    font-size: 11px; font-style: italic; text-transform: none; letter-spacing: 0;
    color: #a87830; cursor: default; white-space: nowrap; flex: none; font-family: inherit;
  }
  .badge.stale { color: #bb0000; }
  .badge.zzz { color: #a87830; }
  .badge.hold { color: #a87830; }
  .qadd {
    background: none; border: none; padding: 0; cursor: pointer;
    color: #bb0000; font-family: inherit; font-size: 12px; text-decoration: underline; flex: none;
  }
  .qadd:hover { color: #ff0000; }
  .snoozebar { display: flex; gap: 10px; padding: 2px 0 4px 37px; align-items: center; font-size: 12px; color: #a87830; flex-wrap: wrap; }

  /* ---------- menus: minimal dropdown ---------- */
  .menu {
    position: absolute; right: 8px; top: 20px; z-index: 60; min-width: 132px;
    background: #1a0e00;
    border: 1px solid; border-color: #000 #6a4a10 #6a4a10 #000;
    border-radius: 0;
    box-shadow: 3px 3px 0 #000;
    padding: 3px;
  }
  .menuhead {
    font-size: 11px; color: #a87830; padding: 3px 8px 4px; margin-bottom: 2px;
    border-bottom: 1px solid #3a2400; font-style: italic; font-weight: 400;
    text-transform: none; letter-spacing: 0; text-shadow: none; font-variant-numeric: normal;
  }
  .menuitem {
    display: block; width: 100%; text-align: left; padding: 3px 8px;
    font-size: 13px; color: #bb0000; background: none; border: none;
    border-radius: 0; cursor: pointer; font-family: inherit; text-decoration: underline;
  }
  .menuitem:hover { background: none; color: #ff0000; }
  .menuitem.danger { color: #bb0000; }
  .menuitem.danger:hover { background: none; color: #ff0000; }
  .addsub { display: flex; gap: 6px; margin-top: 6px; align-items: center; }
  .addsub input[type=text] { flex: 1; min-width: 0; margin-bottom: 0; }

  .hidden { display: none !important; }
  </style>
</head>
<body>
<div id="mast">
  <span class="brand">lock tf in</span>
  <span class="tag"><b>est. black mesa research facility.</b></span>
  <span class="ver">v1.0 &middot; build 1998<br><span id="hlwords">&nbsp;</span></span>
</div>
<nav class="nav">
  <span class="nlinks">
    <button data-v="tasks" class="cur" onclick="switchView('tasks')">tasks</button>
    <button data-v="tracker" onclick="switchView('tracker')">tracker</button>
    <button data-v="timer" onclick="switchView('timer')">timer</button>
    <button data-v="summary" onclick="switchView('summary')">summary</button>
    <button data-v="log" onclick="switchView('log')">log</button>
  </span>
</nav>

<div id="view-tasks">
  <div class="card">
    <div class="btitle"><span>tasks</span><span class="rt" id="taskcount"></span></div>
    <div id="list"></div>
    <div class="addbar hidden" id="addbar">
      <input type="text" id="newtext" placeholder="new task... (Enter)" onkeydown="taskKey(event)">
      <button class="tool pri" onclick="addTask()">add</button>
    </div>
    <button class="tool addreveal" id="addreveal" title="add task" onclick="revealAdd()">&#43;</button>
  </div>
  <div class="card">
    <div class="btitle"><span>day notes</span><span class="rt">lock the fuck in lil bitch</span></div>
    <div id="notewrap">
      <textarea id="notebox" placeholder="one goal. lock in." oninput="noteChanged()"></textarea>
      <div class="noterow"><span class="saved" id="notesaved">saved &#10003;</span></div>
    </div>
  </div>
</div>

<div id="view-timer" class="hidden">
  <div class="tbox" id="tbox">
    <div class="tphase" id="tphase">focus</div>
    <div class="ttime" id="ttime">&ndash;</div>
    <div class="tctrl">
      <button class="tool pri" id="btn-toggle" onclick="pomoToggle()">start</button>
      <button class="tool" onclick="pomoAct('reset')">reset</button>
      <button class="tool" onclick="pomoAct('skip')">skip</button>
    </div>
    <div class="tsum" id="tsum">&nbsp;</div>
  </div>
  <details class="setdrop" id="setdrop">
    <summary>settings</summary>
    <div class="setbody">
      <div class="setcol">
        <div class="ctitle">timer</div>
        <div class="prow" style="margin-bottom:8px">
          <span class="lbl" style="margin:0 6px 0 0">focus</span><input class="tinput" id="cfg-focus" type="number" min="1" max="600" style="width:60px; margin-bottom:0">
          <span class="lbl" style="margin:0 6px 0 10px">break</span><input class="tinput" id="cfg-break" type="number" min="1" max="600" style="width:60px; margin-bottom:0">
        </div>
        <label class="lbl" style="margin:0 0 8px; text-transform:none; letter-spacing:0; font-size:12.5px; color:#9b9b9e"><input type="checkbox" id="cfg-auto"> auto-start next phase</label>
        <div class="prow">
          <button class="tool" onclick="saveCfg()">save</button>
          <span class="saved" id="cfgmsg"></span>
        </div>
      </div>
      <div class="setcol">
        <div class="ctitle">context</div>
        <div class="lbl">category</div>
        <select class="tinput" id="cat" onchange="pomoSetCat(this.value ? +this.value : null)"></select>
        <div class="catrow">
          <input type="text" id="cat-new" placeholder="new category..." onkeydown="if(event.key==='Enter')addCat()">
          <button class="tool pri" title="add category" onclick="addCat()">+</button>
          <button class="tool" title="rename selected category" onclick="renameCat()">&#9998;</button>
          <button class="tool danger" title="delete selected category" onclick="delCat()">&#128465;</button>
        </div>
        <div class="lbl" style="margin-top:8px">subject</div>
        <select class="tinput" id="subj" onchange="pomoSetSubj(this.value ? +this.value : null)"></select>
      </div>
    </div>
  </details>
</div>

<div id="view-summary" class="hidden">
  <div class="card">
    <div class="btitle"><span>summary</span></div>
    <div class="statsgrid" id="statgrid"></div>
    <div class="ctitle">last 7 days by category</div>
    <div id="ch-daily"></div>
    <div class="ctitle" style="margin-top:22px">weekly</div>
    <div class="bars" id="ch-weeks"></div>
    <div class="ctitle" style="margin-top:22px">monthly</div>
    <div id="ch-months"></div>
    <div class="statline" style="margin:26px 0 4px">export: <a href="/card.png" target="_blank">today</a> &middot; <a href="/card-week.png" target="_blank">week</a> &middot; <a href="/card-month.png" target="_blank">month</a> &mdash; retro: <a href="/card.png?style=retro" target="_blank">today</a> &middot; <a href="/card-week.png?style=retro" target="_blank">week</a> &middot; <a href="/card-month.png?style=retro" target="_blank">month</a></div>
  </div>
</div>

<div id="view-log" class="hidden">
  <details class="card logdrop">
    <summary>import from pomofocus</summary>
    <div class="inner">
      <textarea class="ta" id="imptext" placeholder="23-Sep-2026&#10;14:39 ~ 16:56&#10;clickhouse&#10;113"></textarea>
      <div class="prow">
        <button class="tool" onclick="doImport(true)">preview</button>
        <button class="tool pri" onclick="doImport(false)">import</button>
      </div>
      <div class="imp" id="impout"></div>
    </div>
  </details>
  <details class="card logdrop">
    <summary>periods</summary>
    <div class="inner">
      <div id="logtable" style="max-height:480px; overflow-y:auto"></div>
    </div>
  </details>
</div>

<div id="view-tracker" class="hidden">
  <div class="prow" style="margin-bottom:12px; align-items:center">
    <span style="flex:1"></span>
    <button class="tool pri" title="add group" onclick="addGroup()">+</button>
  </div>
  <div id="trkgroups"></div>
</div>

<div class="menu taskmenu" id="taskmenu" hidden>
  <div class="menuhead">set state</div>
  <button class="menuitem" onclick="setTaskState(taskMenuId,'doing')">working on it</button>
  <button class="menuitem" onclick="setTaskState(taskMenuId,'todo')">not working on it</button>
  <button class="menuitem" onclick="setTaskState(taskMenuId,'done')">done</button>
  <div class="menuhead" style="margin-top:6px">task</div>
  <button class="menuitem danger" onclick="closeTaskMenu(); delTask(taskMenuId)">delete</button>
</div>

<script>
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const GLYPH = {todo: "&middot;", doing: "&middot;", done: "&middot;"};
  const $ = id => document.getElementById(id);
  let view = "tasks";
  let tasks = [], openId = null;
  let doneCollapsed = localStorage.getItem("obstodo_done_collapsed") !== "0";
  let pomo = null, logData = null, sumData = null, tickTimer = null, deadline = 0;
  let wasEnded = null;

  async function api(path, method="GET", body) {
    const opt = {method, headers: {"Content-Type": "application/json"}};
    if (body !== undefined) opt.body = JSON.stringify(body);
    const r = await fetch(path, opt);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
    return r.status === 204 ? null : r.json();
  }

  function fmtClock(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    return Math.floor(s / 3600) + ":" + String(Math.floor(s / 60) % 60).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }
  function fmtHM(min) {
    if (!min) return "0m";
    const h = Math.floor(min / 60), m = min % 60;
    return (h ? h + "h " : "") + (m ? m + "m" : (h ? "" : "0m"));
  }

  // ---------- views
  function switchView(v) {
    view = v;
    document.querySelectorAll(".nlinks button").forEach(b => b.classList.toggle("cur", b.dataset.v === v));
    ["tasks","tracker","timer","summary","log"].forEach(x => $("view-" + x).classList.toggle("hidden", x !== v));
    if (v === "tasks") load(true);
    if (v === "timer") { if (!tickTimer) tickTimer = setInterval(renderTime, 250); }
    else if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
    if (v === "summary") loadSummary();
    if (v === "log") loadLog();
    if (v === "tracker") loadTracker();
  }

  // ---------- tasks view
  async function load(keepOpen=false) {
    const cur = await (await fetch("/api/todo")).json();
    tasks = cur.items;
    if (!keepOpen) openId = null;
    renderTasks();
  }

  const TASK_CAP = 4;
  let tasksExpanded = localStorage.getItem("lockin_tasks_expanded") === "1";

  function renderTasks() {
    const open = tasks.filter(t => t.state !== "done");
    const done = tasks.filter(t => t.state === "done");
    $("taskcount").textContent = tasks.length ? `${done.length}/${tasks.length} done` : "";
    const list = $("list");
    if (!tasks.length) {
      list.innerHTML = `<div class="empty">no tasks yet</div>`;
      return;
    }
    let z = 0, html = "";
    const visible = tasksExpanded ? open : open.slice(0, TASK_CAP);
    html += visible.map(t => taskHtml(t, z++)).join("");
    if (open.length > TASK_CAP) {
      html += `<div class="donehead" onclick="toggleTasksExpand()">` +
        `<span class="arrow">${tasksExpanded ? "&#9662;" : "&#9656;"}</span> ` +
        (tasksExpanded ? `show fewer tasks` : `show more (${open.length - TASK_CAP} hidden)`) + `</div>`;
    }
    if (done.length) {
      html += `<div class="donehead" onclick="toggleDone()">` +
        `<span class="arrow">${doneCollapsed ? "&#9656;" : "&#9662;"}</span> done (${done.length})</div>`;
      if (!doneCollapsed) html += done.map(t => taskHtml(t, z++)).join("");
    }
    list.innerHTML = html;
  }

  function toggleTasksExpand() {
    tasksExpanded = !tasksExpanded;
    localStorage.setItem("lockin_tasks_expanded", tasksExpanded ? "1" : "0");
    renderTasks();
  }

  function taskHtml(t, z = 0) {
    return `
      <div class="task ${t.state}${z % 2 ? " alt" : ""}" draggable="true" ondragstart="onDragStart(${t.id}, event)" ondragover="onDragOver(event)" ondrop="onDrop(${t.id}, event)" oncontextmenu="taskMenu(${t.id}, event)">
        <div class="row">
          <span class="glyph" title="cycle state" onclick="cycleState(${t.id},'${t.state}')">${GLYPH[t.state]}</span>
          <span class="ttext" onclick="toggleEditor(${t.id})">${esc(t.text)}${t.details ? '<span class="hasnotes">&#9998;</span>' : ""}</span>
          <button class="delbtn" onclick="delTask(${t.id})">&#10005;</button>
        </div>
        <div class="editor ${openId === t.id ? "open" : ""}" id="ed-${t.id}">
          <label>text</label>
          <input type="text" id="txt-${t.id}" value="${esc(t.text)}">
          <label>details</label>
          <textarea id="det-${t.id}" placeholder="notes, commands, links...">${esc(t.details)}</textarea>
          <button class="tool" onclick="saveTask(${t.id})">save</button>
          <span class="saved" id="ok-${t.id}">saved &#10003;</span>
        </div>
      </div>`;
  }

  function toggleDone() {
    doneCollapsed = !doneCollapsed;
    localStorage.setItem("obstodo_done_collapsed", doneCollapsed ? "1" : "0");
    renderTasks();
  }

  function toggleEditor(id) {
    openId = openId === id ? null : id;
    renderTasks();
    if (openId === id) $(`txt-${id}`).focus();
  }

  async function cycleState(id, state) {
    const next = {todo: "doing", doing: "done", done: "todo"}[state];
    await api(`/api/tasks/${id}`, "PATCH", {state: next});
    await load(true);
  }

  let taskMenuId = null;
  function taskMenu(id, ev) {
    ev.preventDefault();
    ev.stopPropagation();
    taskMenuId = id;
    const m = $("taskmenu");
    m.hidden = false;
    m.style.left = Math.min(ev.clientX, window.innerWidth - 180) + "px";
    m.style.top = Math.min(ev.clientY, window.innerHeight - 150) + "px";
    setTimeout(() => document.addEventListener("click", closeTaskMenu, {once: true}), 0);
  }
  function closeTaskMenu() { const m = $("taskmenu"); if (m) m.hidden = true; }
  async function setTaskState(id, state) {
    closeTaskMenu();
    await api(`/api/tasks/${id}`, "PATCH", {state});
    await load(true);
  }

  async function saveTask(id) {
    const text = $(`txt-${id}`).value.trim();
    const details = $(`det-${id}`).value;
    if (!text) return;
    await api(`/api/tasks/${id}`, "PATCH", {text, details});
    const ok = $(`ok-${id}`);
    ok.classList.add("show");
    setTimeout(() => ok.classList.remove("show"), 1200);
    await load(true);
  }

  async function delTask(id) {
    await api(`/api/tasks/${id}`, "DELETE");
    await load(true);
  }

  let noteTimer = null, noteLoaded = false;
  function revealAdd() {
    $("addreveal").classList.add("hidden");
    $("addbar").classList.remove("hidden");
    $("newtext").focus();
  }
  function hideAdd() {
    $("addbar").classList.add("hidden");
    $("newtext").value = "";
    $("addreveal").classList.remove("hidden");
  }
  function taskKey(ev) {
    if (ev.key === "Enter") addTask();
    else if (ev.key === "Escape") hideAdd();
  }
  async function addTask() {
    const el = $("newtext");
    const text = el.value.trim();
    if (!text) { hideAdd(); return; }
    await api("/api/tasks", "POST", {text});
    await load(true);
    hideAdd();
  }
  function loadNote() {
    if (noteLoaded) return;
    noteLoaded = true;
    fetch("/api/note").then(r => r.json()).then(n => { $("notebox").value = n.text || ""; }).catch(() => {});
  }
  function noteChanged() {
    clearTimeout(noteTimer);
    noteTimer = setTimeout(saveNote, 600);
  }
  async function saveNote() {
    try {
      await api("/api/note", "POST", {text: $("notebox").value});
      const el = $("notesaved");
      el.classList.add("show");
      setTimeout(() => el.classList.remove("show"), 1100);
    } catch (e) {}
  }

  // ---------- timer view
  let actx = null;
  function bell() {
    try {
      actx = actx || new (window.AudioContext || window.webkitAudioContext)();
      if (actx.state === "suspended") actx.resume();
      const t0 = actx.currentTime + 0.01;
      [0, 0.30, 0.60].forEach((off, i) => {
        const o = actx.createOscillator(), g = actx.createGain();
        o.type = "sine";
        o.frequency.value = i === 2 ? 1318.5 : 880;
        g.gain.setValueAtTime(0.0001, t0 + off);
        g.gain.exponentialRampToValueAtTime(0.2, t0 + off + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + off + 0.24);
        o.connect(g); g.connect(actx.destination);
        o.start(t0 + off); o.stop(t0 + off + 0.27);
      });
    } catch (e) {}
  }
  document.addEventListener("click", function unlock() {
    try {
      actx = actx || new (window.AudioContext || window.webkitAudioContext)();
      actx.resume();
    } catch (e) {}
  }, {once: true});

  function startPomoPoll() {
    pollPomo();
    setInterval(pollPomo, 1000);
  }
  async function pollPomo() {
    try {
      pomo = await api("/api/pomodoro");
      deadline = Date.now() + pomo.remaining_ms;
      renderPomo();
      updateTitle();
      if (pomo.ended && wasEnded === false) bell();
      wasEnded = pomo.ended;
    } catch (e) {}
  }

  function updateTitle() {
    if (!pomo) return;
    const ms = pomo.running ? Math.max(0, deadline - Date.now()) : pomo.remaining_ms;
    document.title = fmtClock(ms) + " · " + fmtHM(pomo.today.focused_min) + " · grind";
  }

  function renderTime() {
    if (!pomo) return;
    const ms = pomo.running ? Math.max(0, deadline - Date.now()) : pomo.remaining_ms;
    $("ttime").textContent = fmtClock(ms);
  }

  async function pomoToggle() {
    pomo = await api("/api/pomodoro", "POST", {action: pomo.running ? "pause" : "start"});
    deadline = Date.now() + pomo.remaining_ms;
    wasEnded = pomo.ended;
    renderPomo();
    renderTasks();
  }

  async function pomoAct(action) {
    pomo = await api("/api/pomodoro", "POST", {action});
    deadline = Date.now() + pomo.remaining_ms;
    wasEnded = pomo.ended;
    renderPomo();
  }
  function cfgMsg(text, ok) {
    const el = $("cfgmsg");
    el.textContent = text;
    el.style.color = ok ? "" : "#ff6b6b";
    el.classList.add("show");
    setTimeout(() => el.classList.remove("show"), 2500);
  }
  function syncCfgInputs() {
    if (!pomo) return;
    $("cfg-focus").value = pomo.focus_min;
    $("cfg-break").value = pomo.break_min;
    $("cfg-auto").checked = pomo.auto_next;
  }
  $("setdrop").addEventListener("toggle", function () {
    if (this.open) syncCfgInputs();
  });
  async function saveCfg() {
    const focus = parseInt($("cfg-focus").value, 10);
    const brk = parseInt($("cfg-break").value, 10);
    if (!Number.isInteger(focus) || focus < 1 || focus > 600
        || !Number.isInteger(brk) || brk < 1 || brk > 600) {
      cfgMsg("focus and break must be 1..600", false);
      return;
    }
    try {
      pomo = await api("/api/pomodoro", "POST", {action: "config",
        focus_min: focus, break_min: brk, auto_next: $("cfg-auto").checked});
    } catch (e) {
      cfgMsg(e.message || String(e), false);
      return;
    }
    renderPomo();
    cfgMsg("saved \u2713", true);
  }

  function renderPomo() {
    if (!pomo) return;
    const tt = $("ttime");
    if (!tt) return;
    const box = $("tbox");
    box.className = "tbox " + (pomo.ended ? "end" : pomo.running ? "run" : "");
    $("tphase").textContent = pomo.ended ? pomo.phase + " done" : pomo.phase;
    renderTime();
    $("btn-toggle").textContent = pomo.running ? "pause" : pomo.ended || pomo.remaining_ms <= 0 ? "start next" : "start";
    $("tsum").innerHTML = `today <b>${fmtHM(pomo.today.focused_min)}</b>`;
    loadCats();
    loadSubjects();
  }
  let catSig = "";
  async function loadCats() {
    const cats = await api("/api/categories");
    const sel = $("cat");
    if (!sel) return;
    const sig = JSON.stringify(cats) + "|" + (pomo && pomo.category ? pomo.category.id : "");
    if (sig === catSig) return;
    catSig = sig;
    const cur = pomo && pomo.category ? pomo.category.id : null;
    sel.innerHTML = `<option value="">Unallocated</option>` + cats.filter(c => c.id).map(c =>
      `<option value="${c.id}" ${c.id === cur ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  }
  async function pomoSetCat(v) {
    pomo = await api("/api/pomodoro", "POST", {action: "set", category_id: v});
    catSig = "";
    loadCats();
  }
  async function addCat() {
    const el = $("cat-new");
    const name = el.value.trim();
    if (!name) return;
    try {
      const c = await api("/api/categories", "POST", {name});
      el.value = "";
      await pomoSetCat(c.id);
    } catch (e) {
      alert(e.message || String(e));
    }
  }
  async function renameCat() {
    const sel = $("cat");
    const id = sel.value ? +sel.value : null;
    if (!id) return;
    const old = sel.options[sel.selectedIndex].text;
    const name = (prompt("rename category", old) || "").trim();
    if (!name || name === old) return;
    try {
      await api(`/api/categories/${id}`, "PATCH", {name});
    } catch (e) {
      alert(e.message || String(e));
      return;
    }
    catSig = "";
    loadCats();
  }
  async function delCat() {
    const sel = $("cat");
    const id = sel.value ? +sel.value : null;
    if (!id) return;
    const name = sel.options[sel.selectedIndex].text;
    if (!confirm(`delete "${name}"? its focus periods become Unallocated.`)) return;
    await api(`/api/categories/${id}`, "DELETE");
    await pomoSetCat(null);
  }
  let subsSig = "";
  async function loadSubjects() {
    const sel = $("subj");
    if (!sel) return;
    let data;
    try { data = await api("/api/tracker"); } catch (e) { return; }
    const opts = [];
    data.groups.forEach(g => {
      const ss = g.subjects.filter(s => s.state !== "dropped");
      if (!ss.length) return;
      opts.push(`<optgroup label="${esc(g.name)}">` + ss.map(s =>
        `<option value="${s.id}">${esc(s.name)}</option>`).join("") + `</optgroup>`);
    });
    const curId = pomo && pomo.subject ? pomo.subject.id : "";
    const sig = JSON.stringify(opts) + "|" + curId;
    if (sig === subsSig) return;
    subsSig = sig;
    sel.innerHTML = `<option value="">none</option>` + opts.join("");
    sel.value = curId;
  }
  async function pomoSetSubj(v) {
    pomo = await api("/api/pomodoro", "POST", {action: "set", subject_id: v});
    subsSig = "";
    loadSubjects();
  }

  // ---------- summary view
  async function loadSummary() {
    sumData = await api("/api/pomodoro/summary");
    const s = sumData;
    const stats = [
      [fmtHM(s.total_min), "total focused"],
      [fmtHM(s.avg_per_focus_day), "average focused"],
      [s.streak_current + "d", "current streak"],
    ];
    $("statgrid").innerHTML = stats.map(([v, l]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");
    drawDaily($("ch-daily"), s.daily_categories);
    drawBars($("ch-weeks"), s.weeks.map(w => ({l: w.label, v: w.min})));
    drawMonthRows($("ch-months"), s.months.map(m => ({l: m.label, v: m.min})));
  }
  function drawDaily(el, days) {
    const active = days.filter(d => d.total);
    if (!active.length) { el.innerHTML = `<div class="empty" style="padding:4px 0 4px 16px">no focus in the last 7 days</div>`; return; }
    el.innerHTML = days.map(d => {
      const cats = d.cats.length ? ` <span class="st">(` + d.cats.map(c => esc(c.name) + " " + fmtHM(c.min)).join(", ") + `)</span>` : "";
      return `<div class="statline"><b>${esc(d.label)}</b> ${d.total ? fmtHM(d.total) : "&mdash;"}${cats}</div>`;
    }).join("");
  }
  function drawMonthRows(el, months) {
    if (!months.length) { el.innerHTML = `<div class="empty" style="padding:4px 0 4px 16px">no months tracked yet</div>`; return; }
    el.innerHTML = months.map(m =>
      `<div class="statline"><b>${esc(m.l)}</b> ${m.v ? fmtHM(m.v) : "&mdash;"}</div>`
    ).join("");
  }
  function drawBars(el, data) {
    if (!data.length) { el.innerHTML = `<div class="empty">no data</div>`; return; }
    el.innerHTML = data.map(d =>
      `<div class="statline"><b>${esc(d.l)}</b> ${d.v ? fmtHM(d.v) : "&mdash;"}</div>`
    ).join("");
  }

  // ---------- drag to reorder tasks
  let dragId = null;
  function onDragStart(id, ev) {
    dragId = id;
    if (ev.dataTransfer) ev.dataTransfer.effectAllowed = "move";
  }
  function onDragOver(ev) { ev.preventDefault(); }
  async function onDrop(targetId, ev) {
    ev.preventDefault();
    if (dragId === null || dragId === targetId) return;
    const vis = tasks.filter(t => t.state !== "done").map(t => t.id);
    const from = vis.indexOf(dragId), to = vis.indexOf(targetId);
    dragId = null;
    if (from < 0 || to < 0) return;
    vis.splice(to, 0, vis.splice(from, 1)[0]);
    await api("/api/tasks/reorder", "POST", {order: vis});
    await load(true);
  }

  // ---------- log view
  async function loadLog() {
    logData = await api("/api/pomodoro/log?days=60");
    const rows = logData.periods.map(p => {
      const d = p.started_at.slice(0, 10), st = p.started_at.slice(11, 16), en = p.ended_at.slice(11, 16);
      return `<tr>
        <td>${esc(d)}</td><td>${st} ~ ${en}</td>
        <td>${esc(p.category_name || "Unallocated")}</td>
        <td class="num">${p.minutes}</td>
        <td class="num"><button class="delbtn" onclick="delPeriod(${p.id})">&#10005;</button></td>
      </tr>`;
    }).join("");
    $("logtable").innerHTML = `<table class="t">
      <tr><th>date</th><th>range</th><th>category</th><th class="num">min</th><th></th></tr>${rows || `<tr><td colspan="5" class="empty">no periods</td></tr>`}</table>`;
  }
  async function delPeriod(id) {
    await api(`/api/pomodoro/periods/${id}`, "DELETE");
    await loadLog();
  }
  async function doImport(dry) {
    const out = $("impout");
    const text = $("imptext").value;
    if (!text.trim()) { out.textContent = "nothing to import"; return; }
    const res = await api("/api/pomodoro/import", "POST", {text, dry_run: dry});
    if (dry) {
      out.textContent = `parsed ${res.parsed} rows.\\n` + res.preview.map(r =>
        `${r.start.slice(0, 10)}  ${r.start.slice(11, 16)}~${r.end.slice(11, 16)}  ${r.project.padEnd(18)} ${r.minutes}m`).join("\\n") +
        (res.parsed > res.preview.length ? `\\n... and ${res.parsed - res.preview.length} more` : "");
    } else {
      out.textContent = `imported ${res.imported}, skipped ${res.skipped} (duplicates) of ${res.parsed} parsed.`;
      await loadLog();
    }
  }

  // ---------- tracker view
  let trk = null, trkEditorId = null, trkSnoozeId = null, trkMenuId = null;
  let trkCollapsed = JSON.parse(localStorage.getItem("obstodo_trk_collapsed") || "[]");
  let trkDoneCollapsed = localStorage.getItem("obstodo_trk_done") !== "0";
  let trkDropCollapsed = localStorage.getItem("obstodo_trk_drop") !== "0";
  let trkHoldCollapsed = localStorage.getItem("lockin_trk_hold") !== "0";
  const TSTATE_GLYPH = {todo: "&middot;", doing: "&middot;", done: "&middot;", paused: "&middot;", dropped: "&middot;"};

  function fmtDate(iso) {
    if (!iso) return "";
    const dt = new Date(iso + "T00:00:00");
    return isNaN(dt) ? iso : dt.toLocaleDateString(undefined, {month: "short", day: "numeric"});
  }
  function findSubj(id) {
    for (const g of trk.groups) {
      const s = g.subjects.find(x => x.id === id);
      if (s) return s;
    }
    return null;
  }

  async function loadTracker() {
    trk = await api("/api/tracker");
    renderTracker();
    if (trkEditorId !== null && !findSubj(trkEditorId)) trkEditorId = null;
  }

  function renderTracker() {
    if (!trk) return;
    $("trkgroups").innerHTML = trk.groups.length
      ? trk.groups.map(groupHtml).join("")
      : `<div class="empty">no groups yet, hit + to add one (e.g. stream processing)</div>`;
  }

  function groupHtml(g) {
    const collapsed = trkCollapsed.includes(g.id);
    let body = "";
    if (!collapsed) {
      const openSubs = g.subjects.filter(s => s.state !== "done" && s.state !== "dropped" && s.state !== "paused");
      const doneSubs = g.subjects.filter(s => s.state === "done");
      const holdSubs = g.subjects.filter(s => s.state === "paused");
      const dropSubs = g.subjects.filter(s => s.state === "dropped");
      let z = 0;
      body = openSubs.map(s => subjectHtml(g, s, z++)).join("");
      if (holdSubs.length) {
        body += `<div class="donehead" onclick="toggleTrkHold()"><span class="arrow">${trkHoldCollapsed ? "&#9656;" : "&#9662;"}</span> on hold (${holdSubs.length})</div>`;
        if (!trkHoldCollapsed) body += holdSubs.map(s => subjectHtml(g, s, z++)).join("");
      }
      if (doneSubs.length) {
        body += `<div class="donehead" onclick="toggleTrkDone()"><span class="arrow">${trkDoneCollapsed ? "&#9656;" : "&#9662;"}</span> done (${doneSubs.length})</div>`;
        if (!trkDoneCollapsed) body += doneSubs.map(s => subjectHtml(g, s, z++)).join("");
      }
      if (dropSubs.length) {
        body += `<div class="donehead" onclick="toggleTrkDrop()"><span class="arrow">${trkDropCollapsed ? "&#9656;" : "&#9662;"}</span> dropped (${dropSubs.length})</div>`;
        if (!trkDropCollapsed) body += dropSubs.map(s => subjectHtml(g, s)).join("");
      }
      body += addSubHtml(g.id);
    }
    return `<div class="group">
      <div class="ghead" draggable="true" ondragstart="gDragStart(${g.id}, event)" ondragover="gDragOver(event)" ondrop="gDrop(${g.id}, event)" onclick="toggleGroup(${g.id})">
        <span class="garrow">${collapsed ? "&#9656;" : "&#9662;"}</span>
        <span class="gname" title="click to rename" onclick="renameGroup(${g.id}, event)">${esc(g.name)}</span>
        <div class="gbar"><i style="width:${g.pct === null ? 0 : g.pct}%"></i></div>
        <span class="gpct">${g.pct === null ? "&ndash;" : g.pct + "%"}</span>
        <span class="gmeta">${g.done}/${g.count} done</span>
        <button class="delbtn" title="delete group" onclick="delGroup(${g.id}, event)">&#128465;</button>
      </div>
      <div class="gbody">${body}</div>
    </div>`;
  }

  function addSubHtml(gid) {
    return `<button class="tool addreveal" id="addbtn-${gid}" title="add subject" onclick="revealAddSub(${gid})">&#43;</button>
      <div class="addsub hidden" id="addsub-${gid}">
        <input type="text" id="ns-${gid}" placeholder="add subject..." onkeydown="if(event.key==='Enter')addSubject(${gid});else if(event.key==='Escape')hideAddSub(${gid})">
        <button class="tool pri" onclick="addSubject(${gid})">add</button>
      </div>`;
  }
  function revealAddSub(gid) {
    $(`addbtn-${gid}`).classList.add("hidden");
    $(`addsub-${gid}`).classList.remove("hidden");
    $(`ns-${gid}`).focus();
  }
  function hideAddSub(gid) {
    $(`addsub-${gid}`).classList.add("hidden");
    $(`ns-${gid}`).value = "";
    $(`addbtn-${gid}`).classList.remove("hidden");
  }

  function subjectHtml(g, s, z = 0) {
    const open = trkEditorId === s.id;
    let badge = "";
    if (s.state === "paused") {
      badge = `<span class="badge hold">on hold</span>`;
    } else if (s.stale) {
      badge = `<button class="badge stale" title="last touched ${s.last_touched ? s.last_touched.slice(0, 10) : "never"}, click to snooze" onclick="toggleSnooze(${s.id}, event)">&#9888; ${s.days_idle}d</button>`;
    } else if (s.snoozed) {
      badge = `<span class="badge zzz" title="${esc(s.snooze_reason || "flag snoozed")}">zzz until ${fmtDate(s.snooze_until)}</span>`;
    }
    const star = `<button class="star ${s.is_next ? "on" : ""}" title="${s.is_next ? "unstar (next up)" : "mark as next up"}" onclick="toggleNext(${s.id}, ${s.is_next ? 0 : 1}, event)">${s.is_next ? "&#9733;" : "&#9734;"}</button>`;
    const stateGlyph = `<span class="glyph" title="${s.state}, click to cycle" onclick="cycleSubjState(${s.id}, '${s.state}', event)">${TSTATE_GLYPH[s.state] || "&middot;"}</span>`;
    const link = s.url ? `<a class="slink" href="${esc(s.url)}" target="_blank" rel="noopener" title="open resource">&#8599;</a>` : "";
    const kebab = `<button class="kebab" title="more" onclick="toggleSubjMenu(${s.id}, event)">&#8943;</button>`;
    const snoozeBar = trkSnoozeId === s.id
      ? `<div class="snoozebar">snooze flag:
          <button class="qadd" onclick="snoozeSubj(${s.id}, 7, event)">1w</button>
          <button class="qadd" onclick="snoozeSubj(${s.id}, 14, event)">2w</button>
          <button class="qadd" onclick="snoozeSubj(${s.id}, 30, event)">1m</button>
          <button class="qadd" onclick="snoozeSubj(${s.id}, 0, event)">other&hellip;</button>
          ${s.snoozed ? `<button class="qadd" onclick="unsnoozeSubj(${s.id}, event)">clear</button>` : ""}
        </div>`
      : "";
    return `<div class="subj ${s.state}${z % 2 ? " alt" : ""}">
      <div class="srow" draggable="true" ondragstart="sDragStart(${s.id}, event)" ondragover="sDragOver(event)" ondrop="sDrop(${g.id}, ${s.id}, event)">
        ${star}${stateGlyph}
        <span class="sname" onclick="toggleSubjEditor(${s.id})">${esc(s.name)}${link}</span>
        ${badge}
        ${kebab}
      </div>
      ${snoozeBar}
      ${trkMenuId === s.id ? subjMenuHtml(s) : ""}
      ${open ? subjEditorHtml(s) : ""}
    </div>`;
  }

  function subjMenuHtml(s) {
    const items = [];
    if (s.state !== "done" && s.state !== "dropped") {
      items.push(s.state === "paused"
        ? `<button class="menuitem" onclick="subjState(${s.id}, 'doing', event)">resume</button>`
        : `<button class="menuitem" onclick="subjState(${s.id}, 'paused', event)">on hold</button>`);
      items.push(`<button class="menuitem" onclick="subjState(${s.id}, 'dropped', event)">drop this</button>`);
    }
    if (s.state === "dropped") {
      items.push(`<button class="menuitem" onclick="subjState(${s.id}, 'todo', event)">restore</button>`);
    }
    items.push(`<button class="menuitem danger" onclick="delSubject(${s.id}, event)">delete</button>`);
    const head = s.focused_min ? `<div class="menuhead">${fmtHM(s.focused_min)} focused</div>` : "";
    return `<div class="menu">${head}${items.join("")}</div>`;
  }

  function subjEditorHtml(s) {
    return `<div class="editor open">
      <div class="tedit">
        <div><label>name</label><input type="text" id="te-name-${s.id}" value="${esc(s.name)}"></div>
        <div><label>resource link (url)</label><input type="text" id="te-url-${s.id}" value="${esc(s.url)}" placeholder="https://..."></div>
        <div><label>notes</label><textarea id="te-notes-${s.id}">${esc(s.notes)}</textarea></div>
        <div style="display:flex; align-items:center; gap:8px">
          <button class="tool pri" onclick="saveSubj(${s.id})">save</button>
          <span class="saved" id="te-ok-${s.id}">saved &#10003;</span>
        </div>
      </div>
    </div>`;
  }

  function toggleSubjEditor(id) {
    trkEditorId = trkEditorId === id ? null : id;
    trkMenuId = null;
    if (trkEditorId === null) trkSnoozeId = null;
    renderTracker();
  }

  async function addGroup() {
    const name = (prompt("group name (e.g. stream processing)") || "").trim();
    if (!name) return;
    await api("/api/tracker/groups", "POST", {name});
    await loadTracker();
  }
  async function renameGroup(id, ev) {
    ev.stopPropagation();
    const g = trk.groups.find(x => x.id === id);
    const name = (prompt("rename group", g.name) || "").trim();
    if (!name || name === g.name) return;
    await api(`/api/tracker/groups/${id}`, "PATCH", {name});
    await loadTracker();
  }
  async function delGroup(id, ev) {
    ev.stopPropagation();
    const g = trk.groups.find(x => x.id === id);
    const n = g.subjects.length;
    if (!confirm(`delete "${g.name}" and its ${n} subject${n === 1 ? "" : "s"}?`)) return;
    await api(`/api/tracker/groups/${id}`, "DELETE");
    if (trkEditorId !== null && !findSubj(trkEditorId)) trkEditorId = null;
    await loadTracker();
  }
  function toggleGroup(id) {
    const i = trkCollapsed.indexOf(id);
    if (i >= 0) trkCollapsed.splice(i, 1); else trkCollapsed.push(id);
    localStorage.setItem("obstodo_trk_collapsed", JSON.stringify(trkCollapsed));
    renderTracker();
  }
  async function addSubject(gid) {
    const el = $(`ns-${gid}`);
    const name = el.value.trim();
    if (!name) { hideAddSub(gid); return; }
    await api("/api/tracker/subjects", "POST", {group_id: gid, name});
    hideAddSub(gid);
    await loadTracker();
  }
  async function patchSubj(id, fields) {
    await api(`/api/tracker/subjects/${id}`, "PATCH", fields);
    await loadTracker();
  }
  function subjState(id, state, ev) {
    if (ev) ev.stopPropagation();
    trkMenuId = null;
    patchSubj(id, {state});
  }
  function cycleSubjState(id, state, ev) {
    ev.stopPropagation();
    const next = {todo: "doing", doing: "done", done: "todo", paused: "doing", dropped: "todo"}[state];
    patchSubj(id, {state: next});
  }
  function toggleNext(id, on, ev) {
    ev.stopPropagation();
    patchSubj(id, {is_next: !!on});
  }
  async function delSubject(id, ev) {
    ev.stopPropagation();
    trkMenuId = null;
    const s = findSubj(id);
    if (!s || !confirm(`delete "${s.name}"? its progress log and focus-time link go too.`)) return;
    await api(`/api/tracker/subjects/${id}`, "DELETE");
    trkEditorId = null;
    await loadTracker();
  }
  function toggleSubjMenu(id, ev) {
    ev.stopPropagation();
    trkMenuId = trkMenuId === id ? null : id;
    renderTracker();
    if (trkMenuId !== null) {
      setTimeout(() => document.addEventListener("click", closeSubjMenu, {once: true}), 0);
    }
  }
  function closeSubjMenu() {
    if (trkMenuId !== null) {
      trkMenuId = null;
      renderTracker();
    }
  }
  async function saveSubj(id) {
    const name = $(`te-name-${id}`).value.trim();
    if (!name) return;
    await patchSubj(id, {
      name,
      url: $(`te-url-${id}`).value.trim(),
      notes: $(`te-notes-${id}`).value,
    });
    flashSaved(id);
  }
  function flashSaved(id) {
    const el = $(`te-ok-${id}`);
    if (!el) return;
    el.classList.add("show");
    setTimeout(() => el.classList.remove("show"), 1200);
  }
  function toggleSnooze(id, ev) {
    ev.stopPropagation();
    trkSnoozeId = trkSnoozeId === id ? null : id;
    renderTracker();
  }
  async function snoozeSubj(id, days, ev) {
    ev.stopPropagation();
    let body;
    if (days > 0) body = {days};
    else {
      const d = parseInt(prompt("snooze for how many days?", "21") || "", 10);
      if (!Number.isInteger(d)) return;
      body = {days: d};
    }
    const reason = prompt("reason (optional, shows on the badge)", "") || "";
    body.reason = reason;
    await api(`/api/tracker/subjects/${id}/snooze`, "POST", body);
    trkSnoozeId = null;
    await loadTracker();
  }
  async function unsnoozeSubj(id, ev) {
    ev.stopPropagation();
    await api(`/api/tracker/subjects/${id}/snooze`, "POST", {clear: true});
    trkSnoozeId = null;
    await loadTracker();
  }
  function toggleTrkHold() {
    trkHoldCollapsed = !trkHoldCollapsed;
    localStorage.setItem("lockin_trk_hold", trkHoldCollapsed ? "1" : "0");
    renderTracker();
  }
  function toggleTrkDone() {
    trkDoneCollapsed = !trkDoneCollapsed;
    localStorage.setItem("obstodo_trk_done", trkDoneCollapsed ? "1" : "0");
    renderTracker();
  }
  function toggleTrkDrop() {
    trkDropCollapsed = !trkDropCollapsed;
    localStorage.setItem("obstodo_trk_drop", trkDropCollapsed ? "1" : "0");
    renderTracker();
  }

  // ---------- drag to reorder tracker groups
  let gDragId = null;
  function gDragStart(id, ev) {
    gDragId = id;
    if (ev.dataTransfer) ev.dataTransfer.effectAllowed = "move";
  }
  function gDragOver(ev) { ev.preventDefault(); }
  async function gDrop(targetId, ev) {
    ev.preventDefault();
    ev.stopPropagation();
    if (gDragId === null || gDragId === targetId) return;
    const order = trk.groups.map(g => g.id);
    const from = order.indexOf(gDragId), to = order.indexOf(targetId);
    gDragId = null;
    if (from < 0 || to < 0) return;
    order.splice(to, 0, order.splice(from, 1)[0]);
    await api("/api/tracker/groups/reorder", "POST", {order});
    await loadTracker();
  }

  let sDragId = null;
  function sDragStart(id, ev) {
    sDragId = id;
    if (ev.dataTransfer) ev.dataTransfer.effectAllowed = "move";
  }
  function sDragOver(ev) { ev.preventDefault(); }
  async function sDrop(gid, targetId, ev) {
    ev.preventDefault();
    ev.stopPropagation();
    if (sDragId === null || sDragId === targetId) return;
    const g = trk.groups.find(x => x.id === gid);
    if (!g) return;
    const order = g.subjects.map(s => s.id);
    const from = order.indexOf(sDragId), to = order.indexOf(targetId);
    sDragId = null;
    if (from < 0 || to < 0) return;
    order.splice(to, 0, order.splice(from, 1)[0]);
    await api("/api/tracker/subjects/reorder", "POST", {order});
    await loadTracker();
  }

  document.addEventListener("keydown", e => {
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (e.code === "Space" && !e.altKey && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      if (pomo) pomoToggle();
      return;
    }
    const map = {Digit1: "tasks", Digit2: "tracker", Digit3: "timer", Digit4: "summary", Digit5: "log",
                 Numpad1: "tasks", Numpad2: "tracker", Numpad3: "timer", Numpad4: "summary", Numpad5: "log"};
    if (e.ctrlKey && !e.metaKey && map[e.code]) {
      e.preventDefault();
      switchView(map[e.code]);
    }
  });

  // ---------- half-life flavor: fresh pair every load
  const HL_PHRASES = [
    "resonance cascade", "black mesa", "lambda complex", "xen borderworld",
    "headcrab containment", "crowbar certified", "anomalous materials",
    "unforeseen consequences", "hazard course", "surface tension",
    "blast pit", "on a rail", "gordon freeman", "g-man sighted",
    "hev mark iv", "sector c", "test chamber", "bounce path",
    "station lambda", "vortigaunt approved", "nihilanth rising",
  ];
  $("hlwords").textContent = HL_PHRASES[Math.floor(Math.random() * HL_PHRASES.length)];

  load();
  loadNote();
  startPomoPoll();
</script>
</body>
</html>"""

ARROW_URI = "data:image/gif;base64," + ARROW_GIF_B64
CERAMIC_URI = "data:image/gif;base64," + CERAMIC_GIF_B64

PAGE = PAGE.replace("__ARROW__", ARROW_URI)
PAGE = PAGE.replace("__CERAMIC__", CERAMIC_URI)
MANAGE = MANAGE.replace("__ARROW__", ARROW_URI)
MANAGE = MANAGE.replace("__CERAMIC__", CERAMIC_URI)


# ---------------------------------------------------------------- handler

def make_handler(db_path: str):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                raise ValueError("invalid JSON body")

        def _path_id(self, pattern: str):
            m = re.match(pattern, self.path)
            return int(m.group(1)) if m else None

        def do_GET(self):
            conn = get_db(db_path)
            path = self.path.split("?", 1)[0]
            try:
                if path == "/":
                    self._send(200, PAGE.encode())
                elif path in ("/card.png", "/card-week.png", "/card-month.png"):
                    q = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&")) if "?" in self.path else {}
                    retro = q.get("style") == "retro"
                    if path == "/card.png":
                        self._send(200, card_png(conn, retro), "image/png")
                    elif path == "/card-week.png":
                        self._send(200, card_png_week(conn, retro), "image/png")
                    else:
                        self._send(200, card_png_month(conn, retro), "image/png")
                elif path == "/manage":
                    self._send(200, MANAGE.encode())
                elif path == "/api/todo":
                    self._json(200, todo_payload(conn))
                elif path == "/api/note":
                    self._json(200, {"text": get_setting(conn, "scratchpad", "")})
                elif path == "/api/pomodoro":
                    self._json(200, pomo_payload(conn))
                elif path == "/api/pomodoro/log":
                    q = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&")) if "?" in self.path else {}
                    days_n = max(1, min(3650, int(q.get("days", 60))))
                    since = (date.today() - timedelta(days=days_n)).isoformat()
                    periods = conn.execute(
                        "SELECT p.*, c.name AS category_name FROM pomo_periods p "
                        "LEFT JOIN categories c ON c.id=p.category_id "
                        "WHERE substr(p.started_at,1,10)>=? ORDER BY p.started_at DESC LIMIT 500",
                        (since,),
                    ).fetchall()
                    dayrows = conn.execute(
                        "SELECT substr(started_at,1,10) AS d, SUM(minutes) AS m, COUNT(*) AS n "
                        "FROM pomo_periods WHERE substr(started_at,1,10)>=? GROUP BY d ORDER BY d DESC",
                        (since,),
                    ).fetchall()
                    self._json(200, {
                        "periods": [dict(p) for p in periods],
                        "days": [{"date": r["d"], "min": r["m"], "periods": r["n"]} for r in dayrows],
                    })
                elif path == "/api/pomodoro/summary":
                    self._json(200, pomo_summary(conn))
                elif path == "/api/categories":
                    cats = []
                    for c in conn.execute(
                        "SELECT c.id, c.name, COALESCE(SUM(p.minutes),0) AS total_min "
                        "FROM categories c LEFT JOIN pomo_periods p ON p.category_id=c.id "
                        "GROUP BY c.id ORDER BY c.position"
                    ):
                        cats.append({"id": c["id"], "name": c["name"], "total_min": c["total_min"]})
                    unalloc = conn.execute(
                        "SELECT COALESCE(SUM(minutes),0) FROM pomo_periods WHERE category_id IS NULL"
                    ).fetchone()[0]
                    cats.insert(0, {"id": None, "name": "Unallocated", "total_min": unalloc})
                    self._json(200, cats)
                elif path == "/api/tracker":
                    self._json(200, tracker_payload(conn))
                elif path == "/api/tracker/settings":
                    self._json(200, {"stale_days": tracker_stale_days(conn),
                                     "focus": get_setting(conn, "tracker_focus", "")})
                else:
                    m = re.match(r"^/api/tracker/subjects/(\d+)/history$", path)
                    if m:
                        sid = int(m.group(1))
                        if not conn.execute("SELECT 1 FROM tracker_subjects WHERE id=?", (sid,)).fetchone():
                            return self._json(404, {"error": "subject not found"})
                        self._json(200, {"history": tracker_subject_history(conn, sid)})
                    else:
                        self._json(404, {"error": "not found"})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            finally:
                conn.close()

        def do_POST(self):
            conn = get_db(db_path)
            try:
                body = self._body()
                if self.path == "/api/note":
                    text = str(body.get("text") or "")[:20000]
                    set_setting(conn, "scratchpad", text)
                    conn.commit()
                    self._json(200, {"saved": True})
                elif self.path == "/api/tasks":
                    text = (body.get("text") or "").strip()
                    if not text:
                        return self._json(400, {"error": "text required"})
                    pos = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM tasks").fetchone()[0]
                    cur = conn.execute(
                        "INSERT INTO tasks (day_id, text, details, position) VALUES (?,?,?,?)",
                        (current_day_id(conn), text, (body.get("details") or "").strip(), pos),
                    )
                    conn.commit()
                    self._json(201, {"id": cur.lastrowid})
                elif self.path == "/api/tasks/reorder":
                    order = [int(i) for i in (body.get("order") or [])]
                    if order:
                        marks = ",".join("?" for _ in order)
                        owned = conn.execute(
                            f"SELECT COUNT(*) FROM tasks WHERE id IN ({marks})", order,
                        ).fetchone()[0]
                        if owned != len(set(order)):
                            return self._json(400, {"error": "order must contain only task ids"})
                        base = conn.execute("SELECT COALESCE(MAX(position),0) FROM tasks").fetchone()[0]
                        for n, tid in enumerate(order):
                            conn.execute("UPDATE tasks SET position=? WHERE id=?", (base + 1 + n, tid))
                        conn.commit()
                    self._json(200, {"ordered": len(order)})
                elif self.path == "/api/pomodoro":
                    action = body.get("action")
                    self._json(200, pomo_action(conn, action, body))
                elif self.path == "/api/pomodoro/import":
                    self._json(200, import_pomofocus(conn, body.get("text") or "", bool(body.get("dry_run"))))
                elif self.path == "/api/categories":
                    name = (body.get("name") or "").strip()
                    if not name:
                        return self._json(400, {"error": "name required"})
                    if conn.execute("SELECT 1 FROM categories WHERE name=?", (name,)).fetchone():
                        return self._json(400, {"error": "category already exists"})
                    pos = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM categories").fetchone()[0]
                    cur = conn.execute("INSERT INTO categories (name, position) VALUES (?,?)", (name, pos))
                    conn.commit()
                    self._json(201, {"id": cur.lastrowid, "name": name})
                elif self.path == "/api/tracker/groups":
                    name = (body.get("name") or "").strip()
                    if not name:
                        return self._json(400, {"error": "name required"})
                    pos = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM tracker_groups").fetchone()[0]
                    cur = conn.execute("INSERT INTO tracker_groups (name, position) VALUES (?,?)", (name, pos))
                    conn.commit()
                    self._json(201, {"id": cur.lastrowid, "name": name})
                elif self.path == "/api/tracker/subjects":
                    gid = body.get("group_id")
                    if not conn.execute("SELECT 1 FROM tracker_groups WHERE id=?", (gid,)).fetchone():
                        return self._json(404, {"error": "group not found"})
                    name = (body.get("name") or "").strip()
                    if not name:
                        return self._json(400, {"error": "name required"})
                    kind = body.get("kind") or "other"
                    if kind not in TRACKER_KINDS:
                        return self._json(400, {"error": "kind must be one of " + "|".join(TRACKER_KINDS)})
                    total = body.get("unit_total")
                    if total in (None, ""):
                        total = TRACKER_KINDS[kind]["total"]
                    else:
                        try:
                            total = float(total)
                            if total <= 0:
                                raise ValueError
                        except (TypeError, ValueError):
                            return self._json(400, {"error": "unit_total must be a positive number"})
                    pos = conn.execute(
                        "SELECT COALESCE(MAX(position),0)+1 FROM tracker_subjects WHERE group_id=?", (gid,),
                    ).fetchone()[0]
                    cur = conn.execute(
                        "INSERT INTO tracker_subjects (group_id, name, kind, url, unit_total, unit_label, notes, position) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (gid, name, kind, (body.get("url") or "").strip(), total,
                         TRACKER_KINDS[kind]["label"], (body.get("notes") or "").strip(), pos),
                    )
                    conn.commit()
                    self._json(201, {"id": cur.lastrowid})
                elif self.path == "/api/tracker/settings":
                    if "focus" in body:
                        focus = str(body.get("focus") or "").strip()[:300]
                        set_setting(conn, "tracker_focus", focus)
                        conn.commit()
                        return self._json(200, {"focus": focus})
                    try:
                        n = int(body.get("stale_days"))
                    except (TypeError, ValueError):
                        return self._json(400, {"error": "stale_days must be an integer"})
                    if not 1 <= n <= 365:
                        return self._json(400, {"error": "stale_days must be 1..365"})
                    set_setting(conn, "tracker_stale_days", n)
                    conn.commit()
                    self._json(200, {"stale_days": n})
                else:
                    m = re.match(r"^/api/tracker/subjects/(\d+)/progress$", self.path)
                    if m:
                        self._json(200, tracker_progress(conn, int(m.group(1)), body))
                        return
                    m = re.match(r"^/api/tracker/subjects/(\d+)/snooze$", self.path)
                    if m:
                        s = conn.execute("SELECT * FROM tracker_subjects WHERE id=?", (m.group(1),)).fetchone()
                        if not s:
                            return self._json(404, {"error": "subject not found"})
                        if body.get("clear"):
                            conn.execute(
                                "UPDATE tracker_subjects SET snooze_until=NULL, snooze_reason='' WHERE id=?",
                                (s["id"],),
                            )
                        else:
                            if "days" in body:
                                try:
                                    days = int(body["days"])
                                except (TypeError, ValueError):
                                    return self._json(400, {"error": "days must be an integer"})
                                if not 1 <= days <= 365:
                                    return self._json(400, {"error": "days must be 1..365"})
                                until = (date.today() + timedelta(days=days)).isoformat()
                            else:
                                until = str(body.get("until") or "").strip()
                                try:
                                    if not 0 <= (date.fromisoformat(until) - date.today()).days <= 365:
                                        raise ValueError
                                except ValueError:
                                    return self._json(400, {"error": "until must be a valid future date"})
                            reason = (body.get("reason") or "").strip()
                            conn.execute(
                                "UPDATE tracker_subjects SET snooze_until=?, snooze_reason=? WHERE id=?",
                                (until, reason, s["id"]),
                            )
                        conn.commit()
                        self._json(200, {"id": s["id"]})
                        return
                    if self.path == "/api/tracker/groups/reorder":
                        order = [int(i) for i in (body.get("order") or [])]
                        if not order:
                            return self._json(400, {"error": "order required"})
                        marks = ",".join("?" for _ in order)
                        owned = conn.execute(
                            f"SELECT COUNT(*) FROM tracker_groups WHERE id IN ({marks})", order,
                        ).fetchone()[0]
                        if owned != len(set(order)):
                            return self._json(400, {"error": "order must contain only group ids"})
                        for n, gid in enumerate(order):
                            conn.execute("UPDATE tracker_groups SET position=? WHERE id=?", (n + 1, gid))
                        conn.commit()
                        self._json(200, {"ordered": len(order)})
                        return
                    if self.path == "/api/tracker/subjects/reorder":
                        order = [int(i) for i in (body.get("order") or [])]
                        if not order:
                            return self._json(400, {"error": "order required"})
                        marks = ",".join("?" for _ in order)
                        owned = conn.execute(
                            f"SELECT COUNT(*) FROM tracker_subjects WHERE id IN ({marks})", order,
                        ).fetchone()[0]
                        if owned != len(set(order)):
                            return self._json(400, {"error": "order must contain only subject ids"})
                        for n, sid in enumerate(order):
                            conn.execute("UPDATE tracker_subjects SET position=? WHERE id=?", (n + 1, sid))
                        conn.commit()
                        self._json(200, {"ordered": len(order)})
                        return
                    self._json(404, {"error": "not found"})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            finally:
                conn.close()

        def do_PATCH(self):
            conn = get_db(db_path)
            try:
                task_id = self._path_id(r"^/api/tasks/(\d+)$")
                if task_id:
                    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                    if not task:
                        return self._json(404, {"error": "task not found"})
                    body = self._body()
                    updates, params = [], []
                    if "text" in body:
                        text = (body["text"] or "").strip()
                        if not text:
                            return self._json(400, {"error": "text cannot be empty"})
                        updates.append("text=?"); params.append(text)
                    if "details" in body:
                        updates.append("details=?"); params.append(str(body["details"]))
                    if "state" in body:
                        if body["state"] not in ("todo", "doing", "done"):
                            return self._json(400, {"error": "state must be todo|doing|done"})
                        updates.append("state=?"); params.append(body["state"])
                        if body["state"] == "done":
                            updates.append("done_at=COALESCE(done_at, ?)")
                            params.append(now_iso())
                        else:
                            updates.append("done_at=?")
                            params.append(None)
                    if not updates:
                        return self._json(400, {"error": "nothing to update"})
                    params.append(task_id)
                    conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id=?", params)
                    conn.commit()
                    return self._json(200, {"id": task_id})
                cat_id = self._path_id(r"^/api/categories/(\d+)$")
                if cat_id:
                    if not conn.execute("SELECT 1 FROM categories WHERE id=?", (cat_id,)).fetchone():
                        return self._json(404, {"error": "category not found"})
                    name = (self._body().get("name") or "").strip()
                    if not name:
                        return self._json(400, {"error": "name cannot be empty"})
                    try:
                        conn.execute("UPDATE categories SET name=? WHERE id=?", (name, cat_id))
                        conn.commit()
                    except sqlite3.IntegrityError:
                        return self._json(400, {"error": "category already exists"})
                    return self._json(200, {"id": cat_id, "name": name})
                trk_group_id = self._path_id(r"^/api/tracker/groups/(\d+)$")
                if trk_group_id:
                    if not conn.execute("SELECT 1 FROM tracker_groups WHERE id=?", (trk_group_id,)).fetchone():
                        return self._json(404, {"error": "group not found"})
                    name = (self._body().get("name") or "").strip()
                    if not name:
                        return self._json(400, {"error": "name cannot be empty"})
                    conn.execute("UPDATE tracker_groups SET name=? WHERE id=?", (name, trk_group_id))
                    conn.commit()
                    return self._json(200, {"id": trk_group_id, "name": name})
                trk_subj_id = self._path_id(r"^/api/tracker/subjects/(\d+)$")
                if trk_subj_id:
                    s = conn.execute("SELECT * FROM tracker_subjects WHERE id=?", (trk_subj_id,)).fetchone()
                    if not s:
                        return self._json(404, {"error": "subject not found"})
                    body = self._body()
                    updates, params = [], []
                    if "name" in body:
                        name = (body["name"] or "").strip()
                        if not name:
                            return self._json(400, {"error": "name cannot be empty"})
                        updates.append("name=?"); params.append(name)
                    if "kind" in body:
                        if body["kind"] not in TRACKER_KINDS:
                            return self._json(400, {"error": "kind must be one of " + "|".join(TRACKER_KINDS)})
                        updates.append("kind=?"); params.append(body["kind"])
                    if "url" in body:
                        updates.append("url=?"); params.append((body["url"] or "").strip())
                    if "unit_label" in body:
                        updates.append("unit_label=?"); params.append((body["unit_label"] or "").strip())
                    if "position_at" in body:
                        updates.append("position_at=?"); params.append(str(body["position_at"]).strip())
                    if "notes" in body:
                        updates.append("notes=?"); params.append(str(body["notes"]))
                    if "unit_total" in body:
                        t = body["unit_total"]
                        if t in (None, ""):
                            updates.append("unit_total=?"); params.append(None)
                        else:
                            try:
                                t = float(t)
                                if t <= 0:
                                    raise ValueError
                            except (TypeError, ValueError):
                                return self._json(400, {"error": "unit_total must be a positive number"})
                            updates.append("unit_total=?"); params.append(t)
                    if "unit_done" in body:
                        return self._json(400, {"error": "use /progress to change progress"})
                    if "state" in body:
                        if body["state"] not in TRACKER_STATES:
                            return self._json(400, {"error": "state must be " + "|".join(TRACKER_STATES)})
                        new_state = body["state"]
                        updates.append("state=?"); params.append(new_state)
                        updates.append("last_touched=?"); params.append(now_iso())
                        if new_state == "doing":
                            updates.append("started_at=COALESCE(started_at, ?)"); params.append(now_iso())
                        if new_state == "done":
                            updates.append("finished_at=COALESCE(finished_at, ?)"); params.append(now_iso())
                        else:
                            updates.append("finished_at=?"); params.append(None)
                    if "is_next" in body:
                        want = bool(body["is_next"])
                        updates.append("is_next=?"); params.append(1 if want else 0)
                        if want:
                            conn.execute("UPDATE tracker_subjects SET is_next=0 WHERE group_id=?", (s["group_id"],))
                    if not updates:
                        return self._json(400, {"error": "nothing to update"})
                    params.append(trk_subj_id)
                    conn.execute(f"UPDATE tracker_subjects SET {', '.join(updates)} WHERE id=?", params)
                    conn.commit()
                    return self._json(200, {"id": trk_subj_id})
                self._json(404, {"error": "not found"})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            finally:
                conn.close()

        def do_DELETE(self):
            conn = get_db(db_path)
            try:
                task_id = self._path_id(r"^/api/tasks/(\d+)$")
                if task_id:
                    if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                        return self._json(404, {"error": "task not found"})
                    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                    conn.commit()
                    return self._send(204, b"")
                period_id = self._path_id(r"^/api/pomodoro/periods/(\d+)$")
                if period_id:
                    if not conn.execute("SELECT 1 FROM pomo_periods WHERE id=?", (period_id,)).fetchone():
                        return self._json(404, {"error": "period not found"})
                    conn.execute("DELETE FROM pomo_periods WHERE id=?", (period_id,))
                    conn.commit()
                    return self._send(204, b"")
                cat_id = self._path_id(r"^/api/categories/(\d+)$")
                if cat_id:
                    if not conn.execute("SELECT 1 FROM categories WHERE id=?", (cat_id,)).fetchone():
                        return self._json(404, {"error": "category not found"})
                    conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
                    st = get_pomo_state(conn)
                    if st.get("category_id") == cat_id:
                        st["category_id"] = None
                        save_pomo_state(conn, st)
                    if st.get("block_category_id") == cat_id:
                        st["block_category_id"] = None
                        save_pomo_state(conn, st)
                    conn.commit()
                    return self._send(204, b"")
                trk_group_id = self._path_id(r"^/api/tracker/groups/(\d+)$")
                if trk_group_id:
                    if not conn.execute("SELECT 1 FROM tracker_groups WHERE id=?", (trk_group_id,)).fetchone():
                        return self._json(404, {"error": "group not found"})
                    conn.execute("DELETE FROM tracker_groups WHERE id=?", (trk_group_id,))
                    conn.commit()
                    return self._json(200, {"deleted": trk_group_id})
                trk_subj_id = self._path_id(r"^/api/tracker/subjects/(\d+)$")
                if trk_subj_id:
                    if not conn.execute("SELECT 1 FROM tracker_subjects WHERE id=?", (trk_subj_id,)).fetchone():
                        return self._json(404, {"error": "subject not found"})
                    conn.execute("DELETE FROM tracker_subjects WHERE id=?", (trk_subj_id,))
                    st = get_pomo_state(conn)
                    if st.get("subject_id") == trk_subj_id:
                        st["subject_id"] = None
                        save_pomo_state(conn, st)
                    if st.get("block_subject_id") == trk_subj_id:
                        st["block_subject_id"] = None
                        save_pomo_state(conn, st)
                    conn.commit()
                    return self._send(204, b"")
                self._json(404, {"error": "not found"})
            finally:
                conn.close()

        def log_message(self, *args):
            pass

    return Handler


def start_ticker(db_path: str):
    """Finalize due phases (record + notify) even when no page is polling."""
    def loop():
        while True:
            time.sleep(2)
            try:
                conn = get_db(db_path)
                try:
                    pomo_tick(conn)
                finally:
                    conn.close()
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True, name="pomo-ticker").start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(Path(__file__).resolve().parent.parent / "todolist.txt"),
                    help="seed file used only when the database is empty")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent / "todo.db"))
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    init_db(args.db, args.file)
    start_ticker(args.db)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.db))
    print(f"overlay  -> http://localhost:{args.port}")
    print(f"manage   -> http://localhost:{args.port}/manage")
    print(f"database -> {args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
