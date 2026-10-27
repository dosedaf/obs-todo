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
        counted = [s for s in subs if s["state"] != "dropped"]
        done_n = sum(s["state"] == "done" for s in counted)
        for s in subs:
            if s["state"] == "doing":
                totals["active"] += 1
            if s["stale"]:
                totals["stale"] += 1
            if s["state"] != "dropped" and s["unit_total"] is not None:
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


def _card_render(big: str, small: str) -> bytes:
    """1080x1080 minimalist card: big centered value, small context line."""
    import io
    from PIL import Image, ImageDraw

    W = H = 1080
    img = Image.new("RGB", (W, H), "#161618")
    d = ImageDraw.Draw(img)
    f_big = _card_font(190, bold=True)
    f_small = _card_font(46, bold=False)

    def center(text, font, y, fill):
        w = d.textlength(text, font=font)
        d.text(((W - w) / 2, y), text, font=font, fill=fill)

    center(big, f_big, H / 2 - 155, "#eaeaec")
    center(small, f_small, H / 2 + 105, "#9b9b9e")

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


MONTHS = ("january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december")


def card_png(conn) -> bytes:
    """1080x1080 minimalist day card: total focused, tasks done, top category."""
    focused, tasks_done, top = _card_stats(
        conn, "substr(started_at,1,10)=?", "substr(done_at,1,10)=?", today_str())
    return _card_render(_hm(focused), _card_small(tasks_done, top))


def card_png_week(conn) -> bytes:
    """1080x1080 minimalist week card: rolling last 7 days."""
    since = (date.today() - timedelta(days=6)).isoformat()
    focused, tasks_done, top = _card_stats(
        conn, "substr(started_at,1,10)>=?", "substr(done_at,1,10)>=?", since)
    return _card_render(_hm(focused), "last 7 days  ·  " + _card_small(tasks_done, top))


def card_png_month(conn) -> bytes:
    """1080x1080 minimalist month card: named calendar month, month-to-date."""
    now = date.today()
    focused, tasks_done, top = _card_stats(
        conn, "substr(started_at,1,7)=?", "substr(done_at,1,7)=?",
        f"{now.year:04d}-{now.month:02d}")
    return _card_render(_hm(focused), MONTHS[now.month - 1] + "  ·  " + _card_small(tasks_done, top))


# ---------------------------------------------------------------- pages

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>grind overlay</title>
<style>
  html, body {
    margin: 0;
    background: transparent;
    font-family: "Inter", "Segoe UI", system-ui, sans-serif;
    color: #eaeaec;
  }
  .card {
    margin: 8px;
    padding: 14px 18px;
    background: rgba(20, 20, 22, 0.78);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 14px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45);
    overflow: hidden;
    height: calc(100% - 16px);
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
  }

  .stats {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 14px;
    color: #9b9b9e;
    margin-bottom: 6px;
  }

  .pomo {
    text-align: center;
    padding: 14px 0 12px;
    margin-bottom: 4px;
  }
  .ptime {
    font-size: 46px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.5px;
    line-height: 1.1;
  }
  .pomo.running .ptime { color: #ffffff; }
  .pomo.paused .ptime { color: #9b9b9e; }
  .pomo.ended .ptime { color: #ffffff; animation: blink 1.1s ease-in-out infinite; }
  @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  .pphase {
    font-size: 10.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 2.5px;
    color: #9b9b9e;
    margin-top: 3px;
  }
  .pomo.running .pphase { color: #ffffff; }
  .pomo.ended .pphase { color: #ffffff; }

  .listwrap { flex: 1 1 auto; overflow: hidden; }
  ul { list-style: none; margin: 0; padding: 0; }
  li {
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 5px 0;
    font-size: 20px;
    line-height: 1.4;
  }
  .box {
    flex: none;
    width: 22px;
    text-align: center;
    font-size: 18px;
  }
  .doing .box { color: #ffffff; animation: pulse 1.6s ease-in-out infinite; }
  .doing .label { color: #ffffff; font-weight: 600; }
  .todo .box { color: #57575a; }
  .todo .label { color: #9b9b9e; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

  .pfoot {
    flex: none;
    text-align: center;
    padding-top: 10px;
  }
  .pfocus { font-size: 13px; font-weight: 600; color: #ffffff; letter-spacing: 0.5px; }
  .pdone { display: flex; align-items: center; justify-content: center; gap: 10px; margin-top: 7px; }
  .pdone .seg { flex: 1; height: 1px; background: rgba(255,255,255,0.16); position: relative; }
  .pdone .seg i { position: absolute; left: 0; top: -1px; height: 3px; border-radius: 1.5px; background: #ffffff; }
  .pdone b { font-size: 11px; font-weight: 600; color: #9b9b9e; font-variant-numeric: tabular-nums; }
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
      let html = `
        <div class="stats"><span>${data.stats.done} / ${data.stats.total} done</span><span>${pct}%</span></div>
        ${pomoHtml()}
        <div class="listwrap"><ul>` + data.items.filter(i => i.state !== "done").map(i =>
          `<li class="${i.state}"><span class="box">${i.state === "doing" ? "&#9679;" : "&#9675;"}</span><span class="label">${esc(i.text)}</span></li>`
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
<title>grind</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0 auto;
    background: #161618;
    color: #eaeaec;
    font-family: "Inter", "Segoe UI", system-ui, sans-serif;
    padding: 18px 22px 26px;
    max-width: 860px;
    font-size: 14px;
  }
  .nav {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 18px; padding-bottom: 10px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
  }
  .brand { font-size: 15px; font-weight: 700; letter-spacing: 0.3px; }
  .nlinks button {
    background: none; border: none; cursor: pointer;
    color: #9b9b9e; font-size: 13px; margin-left: 18px; padding: 2px 0;
    border-bottom: 1px solid transparent; font-family: inherit;
  }
  .nlinks button:hover { color: #eaeaec; }
  .nlinks button.cur { color: #ffffff; border-bottom-color: #ffffff; font-weight: 600; }

  .hint { color: #9b9b9e; font-size: 12.5px; margin-bottom: 14px; }
  .card {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 12px;
    padding: 10px 14px;
  }
  .tool, button.std {
    padding: 6px 12px; border-radius: 9px; border: 1px solid rgba(255,255,255,0.12);
    background: none; color: #9b9b9e; cursor: pointer; font-size: 12.5px;
    font-family: inherit;
  }
  .tool:hover, button.std:hover { color: #eaeaec; border-color: rgba(255,255,255,0.30); }
  .tool.pri { background: #eaeaec; color: #161618; border-color: #eaeaec; font-weight: 600; }
  .tool.pri:hover { background: #ffffff; }
  .tool.danger { color: #57575a; }
  .tool.danger:hover { color: #ffffff; border-color: #ffffff; }
  .task { border-bottom: 1px solid rgba(255,255,255,0.06); }
  .task[draggable="true"] { cursor: grab; }
  .task[draggable="true"]:active { cursor: grabbing; }
  .task:last-child { border-bottom: none; }
  .row { display: flex; align-items: center; gap: 10px; padding: 8px 0; }
  .statebtn {
    flex: none; width: 28px; height: 28px; border-radius: 8px;
    border: 1px solid rgba(255,255,255,0.14); background: none; cursor: pointer; font-size: 14px;
  }
  .todo .statebtn { color: #57575a; }
  .doing .statebtn { color: #ffffff; }
  .done .statebtn { color: #9b9b9e; }
  .ttext { flex: 1; font-size: 14.5px; cursor: pointer; }
  .todo .ttext { color: #9b9b9e; }
  .doing .ttext { color: #ffffff; font-weight: 600; }
  .done .ttext { color: #57575a; text-decoration: line-through; }
  .hasnotes { font-size: 10px; color: #57575a; margin-left: 6px; }
  .delbtn { background: none; border: none; color: #57575a; cursor: pointer; font-size: 14px; }
  .delbtn:hover { color: #ffffff; }
  .editor { padding: 4px 0 14px 38px; display: none; }
  .editor.open { display: block; }
  .editor input[type=text], .editor textarea, .tinput, textarea.ta {
    width: 100%; box-sizing: border-box;
    background: rgba(0,0,0,0.35);
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 8px; color: #eaeaec;
    padding: 8px 10px; font-size: 13.5px; font-family: inherit; margin-bottom: 8px;
  }
  .editor textarea, textarea.ta { min-height: 70px; resize: vertical; }
  textarea.ta { min-height: 130px; }
  .editor label, .lbl { font-size: 10.5px; color: #57575a; text-transform: uppercase; letter-spacing: 1px; display: block; margin-bottom: 4px; }
  .addbar { display: flex; gap: 8px; margin-top: 12px; }
  .addbar input {
    flex: 1; background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.14);
    border-radius: 9px; color: #eaeaec; padding: 9px 12px; font-size: 14px; font-family: inherit;
  }
  .addreveal { margin-top: 10px; padding: 4px 12px; font-size: 14px; line-height: 1.3; color: #57575a; }
  .addreveal:hover { color: #eaeaec; }
  .tviewtabs { display: flex; gap: 2px; margin-bottom: 10px; }
  .tviewtab {
    background: none; border: none; color: #57575a; cursor: pointer; font-size: 12.5px;
    padding: 4px 11px; border-radius: 8px; font-family: inherit;
  }
  .tviewtab:hover { color: #9b9b9e; }
  .tviewtab.cur { background: rgba(255,255,255,0.06); color: #eaeaec; font-weight: 600; }
  #notebox {
    width: 100%; box-sizing: border-box; min-height: 55vh;
    background: none; border: none; outline: none; resize: vertical;
    color: #eaeaec; font-size: 14.5px; line-height: 1.65; font-family: inherit; padding: 2px;
  }
  #notebox::placeholder { color: #3f3f43; }
  .noterow { display: flex; justify-content: flex-end; margin-top: 4px; }
  .empty { color: #57575a; padding: 16px 0; text-align: center; font-size: 13px; }
  .donehead {
    display: flex; align-items: center; gap: 7px;
    padding: 10px 2px 6px; cursor: pointer; user-select: none;
    color: #57575a; font-size: 10.5px; text-transform: uppercase;
    letter-spacing: 1.5px; font-weight: 700;
  }
  .donehead:hover { color: #9b9b9e; }
  .donehead .arrow { font-size: 9px; }
  .catrow { display: flex; gap: 6px; margin-top: 8px; align-items: center; }
  .catrow input[type=text] {
    flex: 1; min-width: 0; margin-bottom: 0;
    background: rgba(0,0,0,0.35);
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 9px; color: #eaeaec;
    padding: 7px 10px; font-size: 12.5px; font-family: inherit;
  }
  .catrow .tool { flex: none; padding: 6px 9px; }
  .saved { color: #ffffff; font-size: 11.5px; margin-left: 8px; opacity: 0; transition: opacity .4s; }
  .saved.show { opacity: 1; }
  .hidden { display: none !important; }

  .tbox {
    border: 1px solid rgba(255,255,255,0.08);
    background: rgba(255,255,255,0.02);
    border-radius: 16px;
    min-height: 58vh;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 4px;
    padding: 30px 20px;
  }
  .tphase {
    font-size: 10.5px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 3px; color: #9b9b9e;
  }
  .tctrl { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; justify-content: center; }
  .tsum { margin-top: 14px; font-size: 12.5px; color: #57575a; }
  .tsum b { color: #9b9b9e; font-weight: 600; }
  .ttime {
    font-size: 46px; font-weight: 700; font-variant-numeric: tabular-nums;
    letter-spacing: 1px; line-height: 1.1; color: #9b9b9e;
  }
  .tbox.run .ttime, .tbox.end .ttime { color: #ffffff; }
  .tbox.end .ttime { animation: blink 1.1s ease-in-out infinite; }
  @keyframes blink { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  details.setdrop { margin-top: 14px; }
  details.setdrop summary {
    cursor: pointer; text-align: center; color: #57575a; font-size: 12px;
    text-transform: uppercase; letter-spacing: 2px; font-weight: 700;
    padding: 8px; border: 1px solid rgba(255,255,255,0.08); border-radius: 10px;
    list-style: none;
  }
  details.setdrop summary::-webkit-details-marker { display: none; }
  details.setdrop summary:hover { color: #9b9b9e; }
  details.setdrop[open] summary { color: #9b9b9e; border-color: rgba(255,255,255,0.20); }
  .setbody { padding: 16px 4px 4px; display: grid; grid-template-columns: 1fr 1fr; gap: 0 18px; }
  .setcol { min-width: 0; }
  .setcol .ctitle { margin-top: 0; }
  .ctitle { font-size: 11px; color: #57575a; text-transform: uppercase; letter-spacing: 1.5px; font-weight: 700; margin: 4px 0 8px; }
  .prow { display: flex; gap: 7px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; }
  select.tinput { appearance: auto; }
  .meta { color: #9b9b9e; font-size: 12.5px; line-height: 1.7; }
  .meta b { color: #eaeaec; font-weight: 600; }
  table.t { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.t th { text-align: left; color: #57575a; font-size: 10.5px; text-transform: uppercase; letter-spacing: 1px; padding: 5px 6px; border-bottom: 1px solid rgba(255,255,255,0.10); }
  table.t td { padding: 6px; border-bottom: 1px solid rgba(255,255,255,0.05); color: #cfcfd2; }
  table.t td.num, table.t th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .imp { color: #9b9b9e; font-size: 12px; margin-top: 8px; white-space: pre-line; }
  .stat { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 14px 16px; text-align: center; }
  .stat b { display: block; font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .stat span { font-size: 10.5px; color: #57575a; text-transform: uppercase; letter-spacing: 1px; font-weight: 600; }
  .chart { margin-bottom: 18px; }
  .bars { display: flex; align-items: flex-end; gap: 3px; height: 110px; margin-bottom: 16px; }
  .bcol { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; position: relative; min-width: 0; }
  .bcol i { display: block; width: 100%; background: #3f3f43; border-radius: 3px 3px 0 0; min-height: 1px; }
  .bcol:hover i { background: #eaeaec; }
  .bcol b { font-size: 9.5px; color: #9b9b9e; font-weight: 600; margin-bottom: 2px; font-variant-numeric: tabular-nums; }
  .bcol s { text-decoration: none; font-size: 9px; color: #57575a; margin-top: 4px; position: absolute; bottom: -14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
  .legend { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }
  .legend span { font-size: 11px; color: #9b9b9e; display: inline-flex; align-items: center; gap: 5px; }
  .legend i { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
  .twisty { background: none; border: none; color: #57575a; cursor: pointer; font-size: 10px; padding: 0 2px; font-family: inherit; line-height: 1; }
  .twisty:hover { color: #eaeaec; }
  .cardkebab { position: absolute; top: 10px; right: 12px; font-size: 16px; }
  .cardmenu { top: 32px; right: 14px; min-width: 160px; }
  .focusline { font-size: 14.5px; color: #eaeaec; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .focusline.dim { color: #57575a; }
  .focusinput { flex: 1; min-width: 0; background: none; border: none; outline: none; border-bottom: 1px solid rgba(255,255,255,0.2); color: #eaeaec; font-size: 14.5px; font-family: inherit; padding: 2px 0; }
  .histplot { position: relative; height: 220px; margin: 20px 0 20px 30px; }
  .histgrid { position: absolute; left: 0; right: 0; top: 0; bottom: 20px; }
  .gline { position: absolute; left: 0; right: 0; border-top: 1px solid rgba(255,255,255,0.05); }
  .gline s { position: absolute; left: -30px; top: -6px; width: 24px; text-align: right; text-decoration: none; font-size: 9.5px; color: #57575a; font-variant-numeric: tabular-nums; }
  .hist { position: absolute; left: 0; right: 0; top: 0; bottom: 20px; display: flex; align-items: flex-end; gap: 4px; }
  .hcol { position: relative; flex: 1; min-width: 0; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; }
  .hcol b { font-size: 10px; color: #9b9b9e; font-weight: 600; margin-bottom: 4px; height: 13px; line-height: 13px; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .hstack { width: 100%; display: flex; flex-direction: column-reverse; border-radius: 3px 3px 0 0; overflow: hidden; background: #eaeaec; }
  .hstack i { display: block; width: 100%; min-height: 2px; }
  .hcol s { position: absolute; bottom: -20px; left: 0; right: 0; text-align: center; text-decoration: none; font-size: 9.5px; color: #57575a; }
  .mrow { display: flex; align-items: center; gap: 10px; padding: 3px 0; }
  .mrow s { text-decoration: none; flex: 0 0 34px; font-size: 11px; color: #57575a; }
  .mbar { flex: 1; height: 8px; border-radius: 4px; background: rgba(255,255,255,0.05); overflow: hidden; }
  .mbar i { display: block; height: 100%; background: #c9c9ce; border-radius: 4px; }
  .mrow b { flex: 0 0 56px; text-align: right; font-size: 11px; color: #9b9b9e; font-variant-numeric: tabular-nums; font-weight: 600; }

  .statsgrid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 16px; }

  /* tracker */
  .group { border: 1px solid rgba(255,255,255,0.07); border-radius: 10px; margin-bottom: 8px; background: rgba(255,255,255,0.015); }
  .ghead { display: flex; align-items: center; gap: 8px; padding: 8px 12px; cursor: pointer; user-select: none; border-radius: 10px 10px 0 0; }
  .ghead[draggable="true"] { cursor: grab; }
  .ghead[draggable="true"]:active { cursor: grabbing; }
  .ghead:hover { background: rgba(255,255,255,0.03); }
  .ghead .delbtn { opacity: 0; }
  .ghead:hover .delbtn { opacity: 1; }
  .garrow { font-size: 8px; color: #57575a; flex: none; }
  .gname { font-weight: 700; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 220px; }
  .gname:hover { color: #ffffff; }
  .gbar { flex: 1; height: 5px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden; min-width: 40px; }
  .gbar i { display: block; height: 100%; background: #eaeaec; border-radius: 3px; }
  .gpct { font-size: 11.5px; color: #eaeaec; font-variant-numeric: tabular-nums; width: 34px; text-align: right; font-weight: 700; flex: none; }
  .gmeta { font-size: 10.5px; color: #57575a; white-space: nowrap; flex: none; }
  .gbody { padding: 0 12px 8px; border-top: 1px solid rgba(255,255,255,0.04); border-radius: 0 0 10px 10px; }
  .subj { border-bottom: 1px solid rgba(255,255,255,0.04); position: relative; }
  .subj:last-of-type { border-bottom: none; }
  .srow { display: flex; align-items: center; gap: 7px; padding: 5px 0; }
  .srow[draggable="true"] { cursor: grab; }
  .srow[draggable="true"]:active { cursor: grabbing; }
  .star { background: none; border: none; cursor: pointer; color: #3f3f43; font-size: 13px; padding: 0; width: 16px; flex: none; line-height: 1; opacity: 0.35; }
  .star.on { color: #e8d44d; opacity: 1; }
  .srow:hover .star { opacity: 1; }
  .star:hover { color: #e8d44d; }
  .kebab { background: none; border: none; cursor: pointer; color: #9b9b9e; font-size: 14px; padding: 0 2px; flex: none; line-height: 1; opacity: 0.35; }
  .srow:hover .kebab { opacity: 1; }
  .kebab:hover { color: #ffffff; }
  .sname { font-size: 14.5px; cursor: pointer; flex: 1 1 140px; min-width: 120px; font-weight: 400; }
  .todo .sname { color: #9b9b9e; }
  .doing .sname { color: #ffffff; }
  .paused .sname { color: #57575a; }
  .done .sname { color: #57575a; text-decoration: line-through; }
  .dropped .sname { color: #3f3f43; text-decoration: line-through; }
  .slink { color: #57575a; text-decoration: none; font-size: 11px; margin-left: 5px; }
  .slink:hover { color: #eaeaec; }
  .subj .statebtn { width: 22px; height: 22px; font-size: 12px; border-radius: 7px; }
  .badge { font-size: 9.5px; padding: 1px 7px; border-radius: 8px; font-weight: 700; cursor: pointer; white-space: nowrap; flex: none; font-family: inherit; border: 1px solid transparent; }
  .badge.stale { background: rgba(255,107,107,0.14); color: #ff6b6b; border-color: rgba(255,107,107,0.35); }
  .badge.zzz { background: rgba(255,255,255,0.04); color: #9b9b9e; border-color: rgba(255,255,255,0.14); }
  .badge.hold { background: rgba(232,212,77,0.10); color: #b3a23a; border-color: rgba(232,212,77,0.3); }
  .qadd { background: none; border: 1px solid rgba(255,255,255,0.12); color: #9b9b9e; border-radius: 7px; font-size: 11px; padding: 2px 8px; cursor: pointer; font-family: inherit; flex: none; }
  .qadd:hover { color: #eaeaec; border-color: rgba(255,255,255,0.30); }
  .snoozebar { display: flex; gap: 6px; padding: 0 0 7px 25px; align-items: center; font-size: 11px; color: #57575a; flex-wrap: wrap; }
  .menu { position: absolute; right: 8px; top: 24px; z-index: 60; min-width: 132px; background: #222226; border: 1px solid rgba(255,255,255,0.12); border-radius: 10px; box-shadow: 0 10px 28px rgba(0,0,0,0.5); padding: 4px; }
  .menuhead { font-size: 10.5px; color: #57575a; padding: 5px 10px 6px; border-bottom: 1px solid rgba(255,255,255,0.07); margin-bottom: 3px; font-variant-numeric: tabular-nums; }
  .menuitem { display: block; width: 100%; text-align: left; padding: 6px 10px; font-size: 12.5px; color: #cfcfd2; background: none; border: none; border-radius: 6px; cursor: pointer; font-family: inherit; }
  .menuitem:hover { background: rgba(255,255,255,0.06); color: #ffffff; }
  .menuitem.danger { color: #ff6b6b; }
  .menuitem.danger:hover { background: rgba(255,107,107,0.12); color: #ff6b6b; }
  .addsub { display: flex; gap: 6px; margin-top: 8px; align-items: center; }
  .addsub input[type=text] {
    flex: 1; min-width: 0;
    background: none; border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px; color: #9b9b9e; padding: 6px 10px; font-size: 12.5px;
    font-family: inherit; margin-bottom: 0;
  }
  .addsub input[type=text]:focus { color: #eaeaec; border-color: rgba(255,255,255,0.22); outline: none; }
  .addsub .tool { padding: 5px 11px; }
  .tedit { display: grid; grid-template-columns: 1fr; gap: 6px 14px; }
  .tedit .full { grid-column: 1 / -1; }
  .tedit input[type=text], .tedit textarea {
    width: 100%; box-sizing: border-box;
    background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.14);
    border-radius: 8px; color: #eaeaec; padding: 8px 10px; font-size: 13px;
    font-family: inherit; margin-bottom: 0;
  }
  .tedit textarea { min-height: 54px; resize: vertical; }
</style>
</head>
<body>
<nav class="nav">
  <span class="brand">grind</span>
  <span class="nlinks">
    <button data-v="tasks" class="cur" onclick="switchView('tasks')">tasks</button>
    <button data-v="tracker" onclick="switchView('tracker')">tracker</button>
    <button data-v="timer" onclick="switchView('timer')">timer</button>
    <button data-v="summary" onclick="switchView('summary')">summary</button>
    <button data-v="log" onclick="switchView('log')">log</button>
  </span>
</nav>

<div id="view-tasks">
  <div class="tviewtabs">
    <button class="tviewtab cur" data-tt="tasks" onclick="switchTaskTab('tasks')">tasks</button>
    <button class="tviewtab" data-tt="text" onclick="switchTaskTab('text')">text</button>
  </div>
  <div class="card">
    <div id="list"></div>
    <div id="notewrap" class="hidden">
      <textarea id="notebox" placeholder="lock tf in bum ahh lil ugly ahh boi" oninput="noteChanged()"></textarea>
      <div class="noterow"><span class="saved" id="notesaved">saved &#10003;</span></div>
    </div>
    <div class="addbar hidden" id="addbar">
      <input type="text" id="newtext" placeholder="new task... (Enter)" onkeydown="taskKey(event)">
      <button class="tool pri" onclick="addTask()">add</button>
    </div>
    <button class="tool addreveal" id="addreveal" title="add task" onclick="revealAdd()">&#43;</button>
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
  <div class="statsgrid" id="statgrid"></div>
  <div class="prow" style="margin:18px 0 8px; align-items:center; gap:6px">
    <span class="ctitle" style="margin:0">tracker</span>
    <button class="twisty" id="trktwist" onclick="toggleTrkStats()">&#9656;</button>
  </div>
  <div class="statsgrid" id="trkstatgrid" style="margin-bottom:16px"></div>
  <div class="card" style="position:relative">
    <button class="kebab cardkebab" title="export" onclick="toggleCardMenu(event)">&#8943;</button>
    <div class="menu cardmenu" id="cardmenu" hidden>
      <button class="menuitem" onclick="exportCard('day')">export today</button>
      <button class="menuitem" onclick="exportCard('week')">export this week</button>
      <button class="menuitem" onclick="exportCard('month')">export this month</button>
    </div>
    <div class="ctitle">last 7 days by category</div>
    <div id="ch-daily"></div>
    <div class="ctitle" style="margin-top:22px">weekly</div>
    <div class="bars" id="ch-weeks"></div>
    <div class="ctitle" style="margin-top:22px">monthly</div>
    <div id="ch-months"></div>
  </div>
</div>

<div id="view-log" class="hidden">
  <div class="card">
    <div class="ctitle">import from pomofocus</div>
    <textarea class="ta" id="imptext" placeholder="23-Sep-2026&#10;14:39 ~ 16:56&#10;clickhouse&#10;113"></textarea>
    <div class="prow">
      <button class="tool" onclick="doImport(true)">preview</button>
      <button class="tool pri" onclick="doImport(false)">import</button>
    </div>
    <div class="imp" id="impout"></div>
  </div>
  <div class="card" style="margin-top:12px">
    <div class="ctitle">periods</div>
    <div id="logtable" style="max-height:480px; overflow-y:auto"></div>
  </div>
</div>

<div id="view-tracker" class="hidden">
  <div class="prow" style="margin-bottom:12px; align-items:center">
    <span id="trkfocus" class="focusline" ondblclick="editFocus()"></span>
    <span style="flex:1"></span>
    <button class="tool pri" title="add group" onclick="addGroup()">+</button>
  </div>
  <div id="trkgroups"></div>
</div>

<script>
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const GLYPH = {todo: "&#9675;", doing: "&#9679;", done: "&#10003;"};
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

  function renderTasks() {
    const list = $("list");
    if (!tasks.length) {
      list.innerHTML = `<div class="empty">no tasks yet</div>`;
      return;
    }
    const open = tasks.filter(t => t.state !== "done");
    const done = tasks.filter(t => t.state === "done");
    let html = open.map(taskHtml).join("");
    if (done.length) {
      html += `<div class="donehead" onclick="toggleDone()">` +
        `<span class="arrow">${doneCollapsed ? "&#9656;" : "&#9662;"}</span> done (${done.length})</div>`;
      if (!doneCollapsed) html += done.map(taskHtml).join("");
    }
    list.innerHTML = html;
  }

  function taskHtml(t) {
    return `
      <div class="task ${t.state}" draggable="true" ondragstart="onDragStart(${t.id}, event)" ondragover="onDragOver(event)" ondrop="onDrop(${t.id}, event)">
        <div class="row">
          <button class="statebtn" title="cycle state" onclick="cycleState(${t.id},'${t.state}')">${GLYPH[t.state]}</button>
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

  let taskTab = "tasks", noteTimer = null, noteLoaded = false;
  function switchTaskTab(mode) {
    taskTab = mode;
    document.querySelectorAll(".tviewtab").forEach(b => b.classList.toggle("cur", b.dataset.tt === mode));
    const isTasks = mode === "tasks";
    $("list").classList.toggle("hidden", !isTasks);
    $("notewrap").classList.toggle("hidden", isTasks);
    $("addreveal").classList.toggle("hidden", !isTasks);
    if (isTasks) { hideAdd(); renderTasks(); }
    else { hideAdd(); loadNote(); }
  }
  function revealAdd() {
    $("addreveal").classList.add("hidden");
    $("addbar").classList.remove("hidden");
    $("newtext").focus();
  }
  function hideAdd() {
    $("addbar").classList.add("hidden");
    $("newtext").value = "";
    if (taskTab === "tasks") $("addreveal").classList.remove("hidden");
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
    api("/api/tracker").then(t => {
      trkTotals = t.totals;
      renderTrkStats();
    }).catch(() => {});
  }
  function toggleCardMenu(ev) {
    ev.stopPropagation();
    const m = $("cardmenu");
    m.hidden = !m.hidden;
    if (!m.hidden) setTimeout(() => document.addEventListener("click", closeCardMenu, {once: true}), 0);
  }
  function closeCardMenu() { $("cardmenu").hidden = true; }
  function exportCard(kind) {
    closeCardMenu();
    window.open(kind === "day" ? "/card.png" : kind === "week" ? "/card-week.png" : "/card-month.png", "_blank");
  }
  let trkStatsOpen = false, trkTotals = null;
  function renderTrkStats() {
    if (!trkTotals) return;
    const all = [
      [trkTotals.active, "in progress"],
      [trkTotals.stale, "flagged stale"],
      [fmtHM(trkTotals.focused_min), "invested"],
    ];
    const show = trkStatsOpen ? all : [];
    $("trkstatgrid").innerHTML = show.map(([v, l]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");
    $("trktwist").innerHTML = trkStatsOpen ? "&#9662;" : "&#9656;";
  }
  function toggleTrkStats() { trkStatsOpen = !trkStatsOpen; renderTrkStats(); }
  function drawDaily(el, days) {
    const cats = [];
    days.forEach(d => d.cats.forEach(c => { if (!cats.includes(c.name)) cats.push(c.name); }));
    const shades = ["#c9c9ce", "#8f8f93", "#57575a", "#a8a8ac", "#3f3f43", "#6f6f73", "#dedee2", "#2e2e31"];
    const shade = name => shades[cats.indexOf(name) % shades.length];
    const legend = cats.length
      ? `<div class="legend">` + cats.map(c => `<span><i style="background:${shade(c)}"></i>${esc(c)}</span>`).join("") + `</div>`
      : `<div class="empty" style="padding:4px 0">no focus in the last 7 days</div>`;
    const MAXMIN = 360;
    const grid = [0, 2, 4, 6].map(h =>
      `<div class="gline" style="bottom:${(h / 6 * 100).toFixed(3)}%"><s>${h}h</s></div>`).join("");
    const cols = days.map(d => {
      const segs = d.cats.map(c =>
        `<i style="flex:${c.min}; background:${shade(c.name)}" title="${esc(c.name)}: ${fmtHM(c.min)}"></i>`).join("");
      const h = d.total ? Math.min(100, 100 * d.total / MAXMIN).toFixed(1) : 0;
      return `<div class="hcol">
        <b>${d.total ? fmtHM(d.total) : ""}</b>
        <div class="hstack" style="height:${h}%">${segs}</div>
        <s>${esc(d.label)}</s>
      </div>`;
    }).join("");
    el.innerHTML = legend + `<div class="histplot"><div class="histgrid">${grid}</div><div class="hist">${cols}</div></div>`;
  }
  function drawMonthRows(el, months) {
    if (!months.length) { el.innerHTML = `<div class="empty" style="padding:4px 0">no months tracked yet</div>`; return; }
    const max = Math.max(1, ...months.map(m => m.v));
    el.innerHTML = months.map(m =>
      `<div class="mrow"><s>${esc(m.l)}</s><div class="mbar"><i style="width:${Math.max(2, 100 * m.v / max).toFixed(1)}%"></i></div><b>${fmtHM(m.v)}</b></div>`
    ).join("");
  }
  function drawBars(el, data) {
    if (!data.length) { el.innerHTML = `<div class="empty">no data</div>`; return; }
    const max = Math.max(1, ...data.map(d => d.v));
    el.innerHTML = data.map(d => {
      const pct = Math.round(100 * d.v / max);
      return `<div class="bcol" title="${esc(d.l)}: ${fmtHM(d.v)}">
        ${d.v ? `<b>${fmtHM(d.v)}</b>` : ""}
        <i style="height:${pct}%"></i>
        <s>${esc(d.l)}</s>
      </div>`;
    }).join("");
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
  const TSTATE_GLYPH = {todo: "&#9675;", doing: "&#9679;", done: "&#10003;", paused: "&#10073;&#10073;", dropped: "&#10005;"};

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
    renderFocus();
    if (trkEditorId !== null && !findSubj(trkEditorId)) trkEditorId = null;
  }

  function renderFocus() {
    const el = $("trkfocus");
    if (!el || el.querySelector("input")) return;
    const f = (trk && trk.focus) || "";
    el.textContent = f || "set the focus for this week...";
    el.classList.toggle("dim", !f);
  }
  function editFocus() {
    const el = $("trkfocus");
    if (el.querySelector("input")) return;
    const cur = (trk && trk.focus) || "";
    const input = document.createElement("input");
    input.type = "text";
    input.className = "focusinput";
    input.value = cur;
    el.textContent = "";
    el.classList.remove("dim");
    el.appendChild(input);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    let done = false;
    const finish = async save => {
      if (done) return;
      done = true;
      const v = input.value.trim();
      if (save && v !== cur) {
        try {
          await api("/api/tracker/settings", "POST", {focus: v});
          if (trk) trk.focus = v;
        } catch (e) {}
      }
      renderFocus();
    };
    input.addEventListener("keydown", e => {
      e.stopPropagation();
      if (e.key === "Enter") finish(true);
      else if (e.key === "Escape") finish(false);
    });
    input.addEventListener("blur", () => finish(true));
    input.addEventListener("click", e => e.stopPropagation());
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
      const openSubs = g.subjects.filter(s => s.state !== "done" && s.state !== "dropped");
      const doneSubs = g.subjects.filter(s => s.state === "done");
      const dropSubs = g.subjects.filter(s => s.state === "dropped");
      body = openSubs.map(s => subjectHtml(g, s)).join("");
      if (doneSubs.length) {
        body += `<div class="donehead" onclick="toggleTrkDone()"><span class="arrow">${trkDoneCollapsed ? "&#9656;" : "&#9662;"}</span> done (${doneSubs.length})</div>`;
        if (!trkDoneCollapsed) body += doneSubs.map(s => subjectHtml(g, s)).join("");
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

  function subjectHtml(g, s) {
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
    const stateBtn = `<button class="statebtn" title="${s.state}, click to cycle" onclick="cycleSubjState(${s.id}, '${s.state}', event)">${TSTATE_GLYPH[s.state] || "&#9675;"}</button>`;
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
    return `<div class="subj ${s.state}">
      <div class="srow" draggable="true" ondragstart="sDragStart(${s.id}, event)" ondragover="sDragOver(event)" ondrop="sDrop(${g.id}, ${s.id}, event)">
        ${star}${stateBtn}
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

  load();
  startPomoPoll();
</script>
</body>
</html>"""


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
                elif path == "/card.png":
                    self._send(200, card_png(conn), "image/png")
                elif path == "/card-week.png":
                    self._send(200, card_png_week(conn), "image/png")
                elif path == "/card-month.png":
                    self._send(200, card_png_month(conn), "image/png")
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
