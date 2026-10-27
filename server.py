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

        f_brand = _retro_font(46)
        f_label = _retro_font(26)
        d.text((64, (band_top + band_bot) / 2), "GRIND", font=f_brand, fill="#1c0e00", anchor="lm")
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
        d.text((W / 2, H - 90), "GRIND SOFTWARE  ·  EST. 1998", font=f_foot, fill="#45413a", anchor="mm")

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
FONTS_CSS = """@font-face { font-family: 'Archivo Black'; font-style: normal; font-weight: 400; src: url(data:font/woff2;base64,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) format('woff2'); }
@font-face { font-family: 'Asap'; font-style: normal; font-weight: 400; src: url(data:font/woff2;base64,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) format('woff2'); }
@font-face { font-family: 'Asap'; font-style: normal; font-weight: 700; src: url(data:font/woff2;base64,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) format('woff2'); }
@font-face { font-family: 'IBM Plex Mono'; font-style: normal; font-weight: 600; src: url(data:font/woff2;base64,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) format('woff2'); }
@font-face { font-family: 'IBM Plex Mono'; font-style: normal; font-weight: 700; src: url(data:font/woff2;base64,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) format('woff2'); }"""

# Embedded Archivo Black TTF (base64) for the retro PNG cards.
ARCHIVO_TTF_B64 = "AAEAAAASAQAABAAgRFNJRwAAAAEAAWNkAAAACEdERUYMLwwzAAFVcAAAAI5HUE9T9cbxvAABVgAAAAkSR1NVQqgqqSoAAV8UAAAETk9TLzJ3mYm1AAABqAAAAGBjbWFwyMKXPQAACJgAAAbiY3Z0IDLSAdYAAB2QAAAAgGZwZ209HI58AAAPfAAADW1nYXNwAAAAEAABVWgAAAAIZ2x5ZqGL54wAACFoAAEkTmhlYWQJStjJAAABLAAAADZoaGVhCTEGpAAAAWQAAAAkaG10eCb/Pt0AAAIIAAAGjmxvY2FCGfiSAAAeEAAAA1ZtYXhwAw4OPwAAAYgAAAAgbmFtZV+zwRYAAUW4AAAGAHBvc3QWe79rAAFLuAAACa1wcmVwz5RlaQAAHOwAAACjAAEAAAABAYlo0d5lXw889QAHA+gAAAAAzsROdwAAAADVq0Uz/xj+9QaoA70AAAAHAAIAAAAAAAAAAQAAA27/LgAABrr/GP8eBqgAAQAAAAAAAAAAAAAAAAAAAZ0AAQAAAaoAYQAHADIAAwACACoAPACLAAAAoQ1tAAMAAQADAowBkAAFAAACigJYAAAASwKKAlgAAAFeADIBPAAAAgsKAwICAgILBAAAAAcAAAAAAAAAAAAAAABVS1dOAEAAAPsCA27/LgAABAsBOCAAAJMAAAAAAhACsAAAACAAAwLuAAAAAAAAAU0AAAFNAAADCgAKAwoACgMKAAoDCgAKAwoACgMKAAoDCgAKAwoACgMKAAoDCgAKAwoACgPo//ED6P/xAwoASgMKAC0DCgAtAwoALQMKAC0DCgAtAwoALQMKAEoDCgABAwoASgMKAAEC0gBKAtIASgLSAEoC0gBKAtIASgLSAEoC0gBKAtIASgLSAEoC0gBKApsASgNBAC0DQQAtA0EALQNBAC0DQQAtA0EASgNBAAEDQQBKAYUAVAQEAFQBhQBUAYUACgGF//gBhQAJAYUAVAGFAAQBhQAlAYUAVAGF/+UCmwAXApsAFwNBAEoDQQBKApsASgKbAEoCmwBKApsASgKbAEoCmwAAA7AAPANBAEoDQQBKA0EASgNBAEoDLgBKA0EASgNBAC0DQQAtA0EALQNBAC0DQQAtA0EALQNBAC0DQQAtA0EALQNBAC0DQQAtA+gAIwLSAEoC0gBKA0EALQMKAEoDCgBKAwoASgMKAEoC0gArAtIAKwLSACsC0gArAtIAKwLSACsC0gAXAtIAFwLSABcC0gAXAtIAFwNBAEoDQQBKA0EASgNBAEoDQQBKA0EASgNBAEoDQQBKA0EASgNBAEoDQQBKAwoAEQPoAAUD6AAFA+gABQPoAAUD6AAFAwoABAMKAAcDCgAHAwoABwMKAAcDCgAHAtIAGALSABgC0gAYAtIAGAKbAB4CmwAeApsAHgKbAB4CmwAeApsAHgKbAB4CmwAeApsAHgKbAB4CmwAeA+gAHgPoAB4CmwA9ApsAJAKbACQCmwAkApsAJAKbACQCmwAkApsAJAKbACQDTAAkApsAJAKbACQCmwAkApsAJAKbACQCmwAkApsAJAKbACQCmwAkApsAJAKbACQBhQAIApsABQKbAAUCmwAFApsABQKbAAUCmwA8Apv//gKb/9UBTQBDAU0AQwFNAEMBTf/uAU3/2wFN/+wBTf/oApwAQwFNAAgBTQBDAU3/yAFN/9cBTf/XApsAPAKbADwCmwA8AU0AQwFNAEMB9ABDAU0AQwIWAEMBTf/xA+gAPAKbADwCmwA8A1oAUQKbADwCmwA8AqkAQwKbADwCmwAkApsAJAKbACQCmwAkApsAJAKbACQCmwAkApsAJAKbACQCmwAkApsAJAPoAB0CmwA9ApsAPQKbACQBvAA8AbwANQG8ABMBvAA8AmMAIAJjACACYwAgAmMAIAJjACACYwAgApsAQwGGAAgBvAAbAbwAGwJxABsBvAAbAbwAGwKbADoCmwA6ApsAOgKbADoCmwA6ApsAOgKbADoCmwA6ApsAOgKbADoCmwA6AmMAAAOwAAADsAAAA7AAAAOwAAADsAAAApsABAJjAAACYwAAAmMAAAJjAAACYwAAAiwAFAIsABQCLAAUAiwAFAKbAAgCmwAIAZAADAGQAAwBtAArAwoAGgNWAB8CmwA6AsMAFgKbACkCmwBpApsAMQKbACoCmwAXApsAJwKbACkCmwAtApsAKQKbACQBkAAeAZAAEAGQAA8Ap/9OA+gAJgPoACYD6AAcA+gAJgPoACAD6AAmA+gAOQIsAF8BFv/+AU0ARwH0AFgBTQA9AU0APAPoAD0BTQAvAlwALwFNAC8ClAAUAU0APQJjACUCYwAuAfQAOwEWADkBTQA8ARb//gH0AAAB9AAAAfQAAQGFAB4BhQAoAYUAQgGFAEIBhQA3AYUAIgLuAAAD6AAAAfQAAAFNACcBTQAnApsAPAKbAD0BTQALAU0ADAH0ACwB9AApAfQALAEWACwBFgAsARYAMAEWACwBTQAAApsAJAKUAEUCmwBFApsAFwKbACQDQQBKApsAHwa6ADwCmwAfApsAAgFNAEcAbv9WAlgAPgJYAKUCWADZApQAQgKUAEIClABEApQAQgKUAEIClABCApQARAKUAEMClABCApQAQgKUAEIClAAnApQAJwKUADICRwAJAs8ARQEP//8DVgAfAwoAGgM3ABUCyQAdAiX/3wKbADoB9AAJA9MAsQPoAC4D6AAAApQAOAH0AC4D6AAqAfQALgPoACoD6AAqAfQALgH0AC4DCgCCAuQAFAN5AEsDUgBtApsANgMgACwDIAAsA7YAEgPoAC4BkAAyARYATQEWAE4CSgA1ApsARQKbAEUCWAAsBUEASgKUADcCWABLATAAOwIMADsDeQBDAAD/GAH0AAABTQAe/+7/3QAv/93/7ABbABf/8wAJAEgAOv/IAAAAAAAAAAMAAAADAAAAHAABAAAAAATqAAMAAQAAABwABATOAAAAeABAAAUAOAAAAA0ALwA5AH4BfwGSAf8CGwLHAskC3QMmA5QDqQO8A8AehR7zIBUgHiAiICYgMCAzIDogPCA+IEQgfyCkIKcgrCEFIRMhFiEiISYhLiFeIZUhqCICIgYiDyISIhUiGiIfIikiKyJIImEiZSMCIxAjISXK+wL//wAAAAAADQAgADAAOgCgAZIB+gIYAsYCyQLYAyYDlAOpA7wDwB6AHvIgEyAXICAgJiAwIDIgOSA8ID4gRCB/IKMgpyCsIQUhEyEWISIhJiEuIVshkCGoIgIiBiIPIhEiFSIZIh4iKSIrIkgiYCJkIwIjECMgJcr7Af//AAH/9QAAAOEAAAAA/8QAAAAAAAD+0gAA/nT9ef1l/VP9UAAAAAAAAAAAAADhBuFL4WXhD+Dy4Pzg2uCN4LTgsuCp4IfgfeB+4GngTOBl38cAAN/b33bfbd9lAADfSAAAAADfR99G3yQAAAAA3pTebAAA27oGBwABAAAAAAB0AAAAkAEYAAAC1ALeAuQAAALkAAAAAAAAAAAAAALkAu4C8AL0AwIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC4gAAAAAAAAAAAuQAAALkAuYAAAAAAAAC4gLkAAAAAALiAAAAAAAAAAMBLQE0ATABVAF6AYYBNQE/AUABJgFhASsBRAExATcBKgE2AWgBZQFnATIBhQAEABEAEgAYABwAJgAnACwALwA6ADwAPgBEAEUASwBXAFkAWgBeAGQAaQB0AHUAegB7AIABPQEnAT4BlQE4AaMAhACRAJIAmACcAKYApwCsAK8AugC8AL8AxQDGAM0A2QDbANwA4ADoAO0A+AD5AP4A/wEEATsBjgE8AW0BUQEvAVIBWgFTAVsBjwGIAaEBiQEKAUYBbgFFAYoBpQGNAWsBHAEdAZwBdwGHASgBnwEbAQsBRwEgAR8BIQEzAAkABQAHAA4ACAAMAA8AFQAjAB0AIAAhADYAMQAzADQAGQBKAFAATABOAFUATwFjAFMAbgBqAGwAbQB8AFgA5gCJAIUAhwCOAIgAjACPAJUAowCdAKAAoQC1ALEAswC0AJkAzADSAM4A0ADXANEBZADVAPIA7gDwAPEBAADaAQIACgCKAAYAhgALAIsAEwCTABYAlgAXAJcAFACUABoAmgAbAJsAJACkAB4AngAiAKIAJQClAB8AnwApAKkAKACoACsAqwAqAKoALgCuAC0ArQA5ALkANwC3ADIAsgA4ALgANQCwADAAtgA7ALsAPQC9AL4APwDAAEEAwgBAAMEAQgDDAEMAxABGAMcASADKAEcAyQDIAEkAywBSANQATQDPAFEA0wBWANgAWwDdAF0A3wBcAN4AXwDhAGIA5ABhAOMAYADiAGcA6wBmAOoAZQDpAHMA9wBwAPQAawDvAHIA9gBvAPMAcQD1AHcA+wB9AQEAfgCBAQUAgwEHAIIBBgDnAA0AjQAQAJAAVADWAGMA5QBoAOwBoAGeAZ0BogGnAaYBqAGkAHkA/QB2APoAeAD8AH8BAwFDAUIBQQE5AU0BTwFQAU4BSwFMAUoBkQGSASkBgAF9AX4BfwGBAYIBdQFiAVwBdgFvAXkBZgFeAWoBaQFgAV8ABgH4AAAACQD3AAMAAAAAAAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADAS0BNAEwAVQBegGGATUBPwFAASYBYQErAUQBMQE3AREBEgETARQBFQEWARcBGAEZARoBKgE2AWgBZQFnATIBhQAEABEAEgAYABwAJgAnACwALwA6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AFMAbwBmAHQAdwBhAHIAZQAgAGkAcwAgAGwAaQBjAGUAbgBzAGUAZAAgAHUAbgBkAGUAcgAgAHQAaABlACAAUwBJAEwAIABPAHAAZQBuACAARgBvAG4AdAAgAEwAaQBjAGUAbgBzAGUALAAgAFYAZQByAHMAaQBvAG4AIAAxAC4AMQAuACAAVABoAGkAcwAgAGwAaQBjAGUAbgBzAGUAIABpAHMAIABhAHYAYQBpAGwAYQBiAGwAZQAgAHcAaQB0AGgAIABhACAARgBBAFEAIABhAHQAOgAgAGgAdAB0AHAAOgAvAC8AcwBjAHIAaQBwAHQAcwAuAHMAaQBsAC4AbwByAGcALwBPAEYATABoAHQAdABwADoALwAvAHMAYwByAGkAcAB0AHMALgBzAGkAbAAuAG8AcgBnAC8ATwBGAEwAAgAAAAAAAP+1ADIAAAAAAAAAAAAAAAAAAAAAAAAAAAGqAAABAgACAAMAJADJAQMAxwBiAK0BBAEFAGMBBgCuAJABBwAlACYA/QD/AGQBCAEJACcA6QEKAQsAKABlAQwBDQDIAMoBDgDLAQ8BEAApACoA+AERARIBEwArARQBFQAsARYAzAEXAM0AzgD6AM8BGAEZARoALQEbAC4BHAAvAR0BHgEfASAA4gAwADEBIQEiASMBJABmADIA0AElANEAZwDTASYBJwCRASgArwCwADMA7QA0ADUBKQEqASsANgEsAOQA+wEtAS4ANwEvATABMQEyADgA1AEzANUAaADWATQBNQE2ATcBOAA5ADoBOQE6ATsBPAA7ADwA6wE9ALsBPgA9AT8A5gFAAEQAaQFBAGsAbABqAUIBQwBuAUQAbQCgAUUARQBGAP4BAABvAUYBRwBHAOoBSAEBAEgAcAFJAUoAcgBzAUsAcQFMAU0ASQBKAPkBTgFPAVAASwFRAVIATADXAHQBUwB2AHcAdQFUAVUBVgFXAE0BWABOAVkBWgBPAVsBXAFdAV4A4wBQAFEBXwFgAWEBYgFjAHgAUgB5AWQAewB8AHoBZQFmAKEBZwB9ALEAUwDuAFQAVQFoAWkBagBWAWsA5QD8AWwBbQCJAW4AVwFvAXABcQFyAFgAfgFzAIAAgQB/AXQBdQF2AXcBeABZAFoBeQF6AXsBfABbAFwA7AF9ALoBfgBdAX8A5wGAAMAAwQCdAJ4BgQGCAYMBhACbABMAFAAVABYAFwAYABkAGgAbABwBhQGGAYcAvAD0APUA9gGIAYkBigGLAA0APwDDAIcAHQAPAKsABAGMAKMABgARACIAogAFAAoAHgASAEIBjQGOAF4AYAA+AEAACwAMAY8AswCyABABkACpAKoAvgC/AMUAtAC1ALYBkQC3AMQBkgCEAL0ABwGTAKYA9wGUAZUAhQCWAZYBlwGYAZkBmgAOAO8A8AC4ACAAjwAhAB8AlQCUAJMApwBhAKQAkgGbAJwBnAGdAJoAmQClAZ4AmAGfAAgAxgGgAaEBogGjAaQBpQGmAacAuQAjAAkAiACGAIsAigCMAagAgwBfAOgBqQCCAMIBqgGrAEEBrAGtAa4BrwGwAbEAjQDbAOEA3gDYAI4A3ABDAN8A2gDgAN0A2QGyBE5VTEwGQWJyZXZlB0FtYWNyb24HQW9nb25lawpBcmluZ2FjdXRlB0FFYWN1dGULQ2NpcmN1bWZsZXgKQ2RvdGFjY2VudAZEY2Fyb24GRGNyb2F0BkVicmV2ZQZFY2Fyb24KRWRvdGFjY2VudAdFbWFjcm9uB0VvZ29uZWsLR2NpcmN1bWZsZXgMR2NvbW1hYWNjZW50Ckdkb3RhY2NlbnQESGJhcgtIY2lyY3VtZmxleAJJSgZJYnJldmUHSW1hY3JvbgdJb2dvbmVrBkl0aWxkZQtKY2lyY3VtZmxleAxLY29tbWFhY2NlbnQGTGFjdXRlBkxjYXJvbgxMY29tbWFhY2NlbnQETGRvdAZOYWN1dGUGTmNhcm9uDE5jb21tYWFjY2VudANFbmcGT2JyZXZlDU9odW5nYXJ1bWxhdXQHT21hY3JvbgtPc2xhc2hhY3V0ZQZSYWN1dGUGUmNhcm9uDFJjb21tYWFjY2VudAZTYWN1dGULU2NpcmN1bWZsZXgMU2NvbW1hYWNjZW50BFRiYXIGVGNhcm9uB3VuaTAxNjIHdW5pMDIxQQZVYnJldmUNVWh1bmdhcnVtbGF1dAdVbWFjcm9uB1VvZ29uZWsFVXJpbmcGVXRpbGRlBldhY3V0ZQtXY2lyY3VtZmxleAlXZGllcmVzaXMGV2dyYXZlC1ljaXJjdW1mbGV4BllncmF2ZQZaYWN1dGUKWmRvdGFjY2VudAZhYnJldmUHYW1hY3Jvbgdhb2dvbmVrCmFyaW5nYWN1dGUHYWVhY3V0ZQtjY2lyY3VtZmxleApjZG90YWNjZW50BmRjYXJvbgZlYnJldmUGZWNhcm9uCmVkb3RhY2NlbnQHZW1hY3Jvbgdlb2dvbmVrC2djaXJjdW1mbGV4DGdjb21tYWFjY2VudApnZG90YWNjZW50BGhiYXILaGNpcmN1bWZsZXgGaWJyZXZlAmlqB2ltYWNyb24HaW9nb25lawZpdGlsZGULamNpcmN1bWZsZXgMa2NvbW1hYWNjZW50DGtncmVlbmxhbmRpYwZsYWN1dGUGbGNhcm9uDGxjb21tYWFjY2VudARsZG90Bm5hY3V0ZQtuYXBvc3Ryb3BoZQZuY2Fyb24MbmNvbW1hYWNjZW50A2VuZwZvYnJldmUNb2h1bmdhcnVtbGF1dAdvbWFjcm9uC29zbGFzaGFjdXRlBnJhY3V0ZQZyY2Fyb24McmNvbW1hYWNjZW50BnNhY3V0ZQtzY2lyY3VtZmxleAxzY29tbWFhY2NlbnQFbG9uZ3MEdGJhcgZ0Y2Fyb24HdW5pMDE2Mwd1bmkwMjFCBnVicmV2ZQ11aHVuZ2FydW1sYXV0B3VtYWNyb24HdW9nb25lawV1cmluZwZ1dGlsZGUGd2FjdXRlC3djaXJjdW1mbGV4CXdkaWVyZXNpcwZ3Z3JhdmULeWNpcmN1bWZsZXgGeWdyYXZlBnphY3V0ZQp6ZG90YWNjZW50B3VuaTIwN0YHdW5pMDM5NAd1bmkwM0E5B3VuaTAzQkMHdW5pMDBCOQd1bmkwMEIyB3VuaTAwQjMJb25lZWlnaHRoDHRocmVlZWlnaHRocwtmaXZlZWlnaHRocwxzZXZlbmVpZ2h0aHMJZXhjbGFtZGJsDXVuZGVyc2NvcmVkYmwHdW5pMjAzRQd1bmkyMDE1B3VuaTAwQUQNcXVvdGVyZXZlcnNlZAd1bmkwMEEwBEV1cm8EbGlyYQZwZXNldGEHdW5pMjIxOQd1bmkyMjE1C2VxdWl2YWxlbmNlCmludGVncmFsYnQKaW50ZWdyYWx0cAxpbnRlcnNlY3Rpb24HdW5pMjEyNgd1bmkyMjA2B3VuaTAwQjUKb3J0aG9nb25hbA1yZXZsb2dpY2Fsbm90B2Fycm93dXAKYXJyb3dyaWdodAlhcnJvd2Rvd24JYXJyb3dsZWZ0CWFycm93Ym90aAlhcnJvd3VwZG4MYXJyb3d1cGRuYnNl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"


# ---------------------------------------------------------------- pages

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>grind overlay</title>
<style>
__FONTS_CSS__
  html, body {
    margin: 0;
    background: transparent;
    font-family: "Asap", Verdana, Geneva, sans-serif;
    color: #c4bfb4;
  }
  #card {
    margin: 8px;
    padding: 0 14px 12px;
    background: rgba(10, 8, 5, 0.85);
    border: 1px solid #a85e00;
    border-radius: 0;
    box-shadow: 0 0 0 1px #000, 0 6px 18px rgba(0, 0, 0, 0.7), 0 0 22px rgba(204, 110, 0, 0.12);
    overflow: hidden;
    height: calc(100% - 16px);
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
  }
  #card::before {
    content: "GRIND";
    display: block;
    margin: 0 -14px 10px;
    padding: 5px 14px 4px;
    background: linear-gradient(180deg, #e0820f 0%, #a85a00 100%);
    color: #1c0e00;
    font-family: "Archivo Black", "Arial Black", sans-serif;
    font-size: 15px;
    letter-spacing: 5px;
    text-shadow: 0 1px 0 rgba(255, 200, 120, 0.45);
    box-shadow: inset 0 1px 0 rgba(255, 210, 150, 0.4), 0 1px 3px rgba(0, 0, 0, 0.6);
  }

  .stats {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #b98a4a;
    text-shadow: 0 1px 1px #000;
    border-bottom: 1px solid #2c2820;
    padding: 4px 0 8px;
    margin-bottom: 2px;
  }

  .pomo {
    text-align: center;
    padding: 12px 0 10px;
    margin-bottom: 2px;
  }
  .ptime {
    font-family: "IBM Plex Mono", "Courier New", monospace;
    font-weight: 700;
    font-size: 46px;
    letter-spacing: 1px;
    line-height: 1.1;
    color: #8a8478;
    text-shadow: 0 2px 2px #000;
  }
  .pomo.running .ptime {
    color: #f5f0e4;
    text-shadow: 0 0 18px rgba(204, 110, 0, 0.45), 0 0 5px rgba(204, 110, 0, 0.3), 0 2px 2px #000;
  }
  .pomo.paused .ptime { color: #8a8478; }
  .pomo.ended .ptime { color: #e8a340; animation: blink 1.1s ease-in-out infinite; }
  @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  .pphase {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 3px;
    color: #8a8478;
    text-shadow: 0 1px 1px #000;
    margin-top: 3px;
  }
  .pomo.running .pphase { color: #d98d2a; }
  .pomo.ended .pphase { color: #e8a340; }

  .listwrap { flex: 1 1 auto; overflow: hidden; }
  ul { list-style: none; margin: 0; padding: 0; }
  li {
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 5px 0;
    font-size: 19px;
    line-height: 1.4;
  }
  .box {
    flex: none;
    width: 20px;
    text-align: center;
    font-size: 17px;
    font-family: "IBM Plex Mono", "Courier New", monospace;
    font-weight: 700;
  }
  .doing .box { color: #e0820f; text-shadow: 0 0 7px rgba(204, 110, 0, 0.55); animation: pulse 1.6s ease-in-out infinite; }
  .doing .label { color: #f0ead9; font-weight: 700; text-shadow: 0 1px 1px #000; }
  .todo .box { color: #5a564c; }
  .todo .label { color: #8a8478; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

  .pfoot {
    flex: none;
    text-align: center;
    padding-top: 9px;
    border-top: 1px solid #2c2820;
  }
  .pfocus { font-size: 13px; font-weight: 700; color: #d98d2a; letter-spacing: 0.5px; text-shadow: 0 1px 1px #000; }
  .pdone { display: flex; align-items: center; justify-content: center; gap: 10px; margin-top: 7px; }
  .pdone .seg { flex: 1; height: 3px; background: #26221a; position: relative; box-shadow: inset 0 1px 1px #000; }
  .pdone .seg i { position: absolute; left: 0; top: 0; bottom: 0; background: linear-gradient(90deg, #a85a00, #e0820f); }
  .pdone b { font-size: 11px; font-weight: 700; color: #8a8478; font-variant-numeric: tabular-nums; }
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
__FONTS_CSS__
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0 auto;
    background: #0b0a08 radial-gradient(1200px 500px at 50% -80px, #171310 0%, #0b0a08 60%);
    color: #b8b5ae;
    font-family: "Asap", Verdana, Geneva, sans-serif;
    padding: 0 22px 30px;
    max-width: 900px;
    font-size: 12px;
  }

  /* ---------- navbar ---------- */
  .nav {
    display: flex; align-items: center;
    margin: 0 -22px 4px; padding: 0 22px;
    background: linear-gradient(180deg, #1c1913 0%, #12100b 100%);
    border-top: 1px solid #322d22;
    border-bottom: 3px solid #a85e00;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.6);
  }
  .brand {
    font-family: "Archivo Black", "Arial Black", sans-serif;
    font-size: 21px; font-weight: 400;
    text-transform: uppercase; letter-spacing: 1px;
    color: #d97a10;
    text-shadow: 0 1px 0 #8a4d00, 0 2px 0 #553000, 0 3px 2px rgba(0, 0, 0, 0.9);
    margin-right: 22px;
  }
  .nlinks button {
    background: none; border: none; border-right: 1px solid #26221a; cursor: pointer;
    color: #8a8478; font-size: 11px; font-weight: 700; letter-spacing: 2px;
    text-transform: uppercase; font-family: inherit;
    margin-left: 0; padding: 10px 16px;
    text-shadow: 0 1px 1px #000;
  }
  .nlinks button:hover { color: #e8dcc8; background: rgba(204, 110, 0, 0.08); }
  .nlinks button.cur {
    background: linear-gradient(180deg, #e0820f 0%, #a85a00 100%);
    color: #1c0e00;
    text-shadow: 0 1px 0 rgba(255, 190, 110, 0.45);
    box-shadow: inset 0 1px 0 rgba(255, 210, 150, 0.5), inset 0 -2px 4px rgba(60, 30, 0, 0.6);
  }

  /* ---------- bronze title bars ---------- */
  .btitle {
    display: flex; align-items: center; justify-content: space-between;
    margin: -10px -14px 10px;
    padding: 5px 10px;
    background: linear-gradient(180deg, #43301a 0%, #2b1e0e 55%, #241808 100%);
    color: #eda44f;
    font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase;
    text-shadow: 0 1px 1px #000;
    border-top: 1px solid #5f4626;
    border-bottom: 1px solid #120c05;
    box-shadow: inset 0 1px 0 rgba(255, 190, 110, 0.12);
  }
  .btitle .rt { font-weight: 400; letter-spacing: 1px; color: #b98a4a; }

  /* ---------- boxes ---------- */
  .card {
    background: linear-gradient(180deg, #171512 0%, #141210 100%);
    border: 1px solid #363127;
    border-top-color: #453e30;
    border-radius: 0;
    padding: 10px 14px;
    margin-bottom: 14px;
    box-shadow: 0 3px 10px rgba(0, 0, 0, 0.55);
  }
  .hint { color: #7d786e; font-size: 12px; margin-bottom: 14px; }

  /* ---------- beveled buttons ---------- */
  .tool, button.std {
    padding: 6px 13px; border-radius: 0;
    border: 1px solid; border-color: #4c463a #171410 #171410 #4c463a;
    background: linear-gradient(180deg, #35322a 0%, #26231d 100%);
    color: #c4bfb4; cursor: pointer; font-size: 10px; font-weight: 700;
    letter-spacing: 1px; text-transform: uppercase;
    font-family: inherit;
    text-shadow: 0 1px 1px #000;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.6), inset 0 1px 0 rgba(255, 255, 255, 0.06);
  }
  .tool:hover, button.std:hover { color: #f0ead9; }
  .tool:active, button.std:active {
    border-color: #171410 #4c463a #4c463a #171410;
    background: linear-gradient(180deg, #211f19 0%, #2a271f 100%);
    box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.7);
  }
  .tool.pri {
    background: linear-gradient(180deg, #e0820f 0%, #a85a00 100%);
    color: #1c0e00;
    border-color: #f2ae5c #5c3100 #5c3100 #f2ae5c;
    text-shadow: 0 1px 0 rgba(255, 200, 120, 0.45);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.6), inset 0 1px 0 rgba(255, 210, 150, 0.5);
  }
  .tool.pri:hover { background: linear-gradient(180deg, #ef8f1a 0%, #b86300 100%); color: #140a00; }
  .tool.danger { color: #8a8478; }
  .tool.danger:hover { color: #e06a3a; }

  /* ---------- sunken inputs ---------- */
  .editor input[type=text], .editor textarea, .tinput, textarea.ta,
  .addsub input[type=text], .tedit input[type=text], .tedit textarea,
  .addbar input, .catrow input[type=text] {
    width: 100%; box-sizing: border-box;
    background: #0f0d0a;
    border: 1px solid; border-color: #050403 #3a352a #3a352a #050403;
    border-radius: 0;
    color: #cfc9be;
    padding: 8px 10px; font-size: 12.5px; font-family: inherit; margin-bottom: 8px;
    box-shadow: inset 0 2px 3px rgba(0, 0, 0, 0.5);
    outline: none;
  }
  .editor input[type=text]:focus, .editor textarea:focus, .tinput:focus, textarea.ta:focus,
  .addsub input[type=text]:focus, .tedit input[type=text]:focus, .tedit textarea:focus,
  .addbar input:focus, .catrow input[type=text]:focus {
    border-color: #050403 #a85e00 #a85e00 #050403;
    box-shadow: inset 0 2px 3px rgba(0, 0, 0, 0.5), 0 0 5px rgba(204, 110, 0, 0.3);
    color: #e8e2d4;
  }
  .editor textarea, textarea.ta { min-height: 70px; resize: vertical; }
  textarea.ta { min-height: 130px; font-family: "IBM Plex Mono", "Courier New", monospace; font-size: 12px; line-height: 1.6; }
  .editor label, .lbl {
    font-size: 10px; color: #8a8478; text-transform: uppercase; letter-spacing: 1px;
    font-weight: 700; display: block; margin-bottom: 4px;
    text-shadow: 0 1px 1px #000;
  }

  /* ---------- task rows ---------- */
  .task { border-bottom: 1px solid #1e1b15; }
  .task[draggable="true"] { cursor: grab; }
  .task[draggable="true"]:active { cursor: grabbing; }
  .task:last-child { border-bottom: none; }
  .row { display: flex; align-items: center; gap: 10px; padding: 8px 0; }
  .statebtn {
    flex: none; width: 28px; height: 28px; border-radius: 0;
    border: 1px solid; border-color: #050403 #3a352a #3a352a #050403;
    background: #0f0d0a; cursor: pointer; font-size: 14px;
    font-family: "IBM Plex Mono", "Courier New", monospace; font-weight: 700;
    box-shadow: inset 0 2px 3px rgba(0, 0, 0, 0.5);
  }
  .todo .statebtn { color: #5a564c; }
  .doing .statebtn { color: #e0820f; text-shadow: 0 0 7px rgba(204, 110, 0, 0.55); }
  .done .statebtn { color: #6e675a; }
  .ttext { flex: 1; font-size: 13.5px; cursor: pointer; text-shadow: 0 1px 1px #000; }
  .todo .ttext { color: #9a948a; }
  .doing .ttext { color: #f0ead9; font-weight: 700; }
  .done .ttext { color: #5a564c; text-decoration: line-through; }
  .hasnotes { font-size: 10px; color: #d98d2a; margin-left: 6px; }
  .delbtn { background: none; border: none; color: #5a564c; cursor: pointer; font-size: 14px; }
  .delbtn:hover { color: #e06a3a; }
  .editor { padding: 4px 0 14px 38px; display: none; }
  .editor.open { display: block; }
  .editor textarea { min-height: 70px; resize: vertical; }
  .tedit { display: grid; grid-template-columns: 1fr; gap: 6px 14px; }
  .tedit .full { grid-column: 1 / -1; }
  .tedit textarea { min-height: 54px; resize: vertical; }
  .saved { color: #d98d2a; font-size: 11.5px; margin-left: 8px; opacity: 0; transition: opacity .4s; }
  .saved.show { opacity: 1; }

  .addbar { display: flex; gap: 8px; margin-top: 12px; }
  .addbar input { flex: 1; margin-bottom: 0; }
  .addreveal { margin-top: 10px; padding: 4px 12px; font-size: 14px; line-height: 1.3; color: #5a564c; }
  .addreveal:hover { color: #d97a10; }
  .empty { color: #5a564c; padding: 16px 0; text-align: center; font-size: 13px; }
  .donehead {
    display: flex; align-items: center; gap: 7px;
    padding: 10px 2px 6px; cursor: pointer; user-select: none;
    color: #5a564c; font-size: 10px; text-transform: uppercase;
    letter-spacing: 2px; font-weight: 700;
  }
  .donehead:hover { color: #d97a10; }
  .donehead .arrow { font-size: 9px; }
  .catrow { display: flex; gap: 6px; margin-top: 8px; align-items: center; }
  .catrow input[type=text] { flex: 1; min-width: 0; margin-bottom: 0; }
  .catrow .tool { flex: none; padding: 6px 9px; }
  .tviewtabs { display: flex; gap: 3px; margin: 14px 0 0; }
  .tviewtab {
    background: linear-gradient(180deg, #26221a 0%, #1a1712 100%);
    border: 1px solid #363127; border-bottom: none;
    color: #8a8478; cursor: pointer; font-size: 12px;
    padding: 5px 14px 6px; border-radius: 0; font-family: inherit;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
  }
  .tviewtab:hover { color: #e8dcc8; }
  .tviewtab.cur {
    background: linear-gradient(180deg, #e0820f 0%, #a85a00 100%);
    border-color: #f2ae5c #5c3100 #a85a00 #f2ae5c;
    color: #1c0e00; font-weight: 700;
    text-shadow: 0 1px 0 rgba(255, 200, 120, 0.45);
  }
  #notebox {
    width: 100%; box-sizing: border-box; min-height: 55vh;
    background: none; border: none; outline: none; resize: vertical;
    box-shadow: none;
    color: #cfc9be; font-size: 13px; line-height: 1.7;
    font-family: "IBM Plex Mono", "Courier New", monospace; padding: 2px;
  }
  #notebox::placeholder { color: #45413a; }
  .noterow { display: flex; justify-content: flex-end; margin-top: 4px; }

  /* ---------- timer ---------- */
  .tbox {
    border: 1px solid #363127;
    border-top-color: #453e30;
    background: linear-gradient(180deg, #171512 0%, #141210 100%);
    border-radius: 0;
    min-height: 58vh;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 4px;
    padding: 30px 20px;
    box-shadow: 0 3px 10px rgba(0, 0, 0, 0.55);
  }
  .tphase {
    font-size: 12px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 7px; color: #c98d3f;
    text-shadow: 0 1px 1px #000;
  }
  .tctrl { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; justify-content: center; }
  .tctrl .tool { padding: 8px 20px; font-size: 11px; }
  .tsum { margin-top: 16px; font-size: 12px; color: #8a8478; text-shadow: 0 1px 1px #000; }
  .tsum b { color: #d98d2a; font-weight: 700; }
  .ttime {
    font-family: "IBM Plex Mono", "Courier New", monospace;
    font-size: 52px; font-weight: 700; font-variant-numeric: tabular-nums;
    letter-spacing: 2px; line-height: 1.1; color: #8a8478;
    text-shadow: 0 2px 2px #000;
  }
  .tbox.run .ttime {
    color: #f5f0e4;
    text-shadow: 0 0 22px rgba(204, 110, 0, 0.4), 0 0 6px rgba(204, 110, 0, 0.25), 0 3px 3px #000;
  }
  .tbox.end .ttime { color: #e8a340; animation: blink 1.1s ease-in-out infinite; }
  @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
  details.setdrop { margin-top: 14px; }
  details.setdrop summary {
    cursor: pointer; text-align: center; color: #8a8478; font-size: 11px;
    text-transform: uppercase; letter-spacing: 2px; font-weight: 700;
    padding: 8px;
    border: 1px solid #363127; border-top-color: #453e30;
    border-radius: 0;
    background: linear-gradient(180deg, #1a1712 0%, #141210 100%);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.5);
    list-style: none; font-family: inherit;
    text-shadow: 0 1px 1px #000;
  }
  details.setdrop summary::-webkit-details-marker { display: none; }
  details.setdrop summary:hover { color: #d98d2a; }
  details.setdrop[open] summary { color: #eda44f; border-color: #a85e00; }
  .setbody { padding: 16px 4px 4px; display: grid; grid-template-columns: 1fr 1fr; gap: 0 18px; }
  .setcol { min-width: 0; }
  .setcol .ctitle { margin-top: 0; }
  .ctitle {
    font-size: 11px; color: #c98d3f; text-transform: uppercase; letter-spacing: 2px;
    font-weight: 700; margin: 14px 0 8px;
    text-shadow: 0 1px 1px #000;
  }
  .prow { display: flex; gap: 7px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; }
  .tinput { width: auto; margin-bottom: 0; }
  select.tinput { appearance: auto; }
  .meta { color: #9a948a; font-size: 12.5px; line-height: 1.7; }
  .meta b { color: #e8e2d4; font-weight: 700; }

  /* ---------- tables (log) ---------- */
  table.t { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.t th {
    text-align: left; color: #7d786e; font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 1px; padding: 5px 6px;
    border-bottom: 1px solid #302b21;
    text-shadow: 0 1px 1px #000;
  }
  table.t td { padding: 6px; border-bottom: 1px solid #1e1b15; color: #b8b5ae; }
  table.t td.num, table.t th.num { text-align: right; font-variant-numeric: tabular-nums; font-family: "IBM Plex Mono", "Courier New", monospace; font-weight: 700; font-size: 11.5px; color: #9a948a; }
  table.t td .delbtn { opacity: 0.4; }
  table.t tr:hover td .delbtn { opacity: 1; }
  .imp { color: #9a948a; font-size: 12px; margin-top: 8px; white-space: pre-line; }

  /* ---------- summary ---------- */
  .stat {
    background: linear-gradient(180deg, #1a1712 0%, #131110 100%);
    border: 1px solid #363127; border-top-color: #453e30;
    border-radius: 0; padding: 14px 16px; text-align: center;
    box-shadow: 0 3px 8px rgba(0, 0, 0, 0.5);
  }
  .stat b {
    display: block; font-size: 25px; font-weight: 400;
    font-family: "Archivo Black", "Arial Black", sans-serif;
    color: #e0912a;
    text-shadow: 0 2px 2px #000, 0 0 14px rgba(204, 110, 0, 0.25);
    font-variant-numeric: tabular-nums;
  }
  .stat span {
    font-size: 10px; color: #7d786e; text-transform: uppercase;
    letter-spacing: 1.5px; font-weight: 700; margin-top: 5px; display: block;
    text-shadow: 0 1px 1px #000;
  }
  .statsgrid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 16px; }
  .chart { margin-bottom: 18px; }
  .bars { display: flex; align-items: flex-end; gap: 3px; height: 110px; margin-bottom: 16px; border-bottom: 1px solid #363127; }
  .bcol { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; position: relative; min-width: 0; }
  .bcol i {
    display: block; width: 100%;
    background: linear-gradient(180deg, #e0820f 0%, #a85a00 70%, #7a4200 100%);
    border-top: 1px solid #f2ae5c;
    border-radius: 0; min-height: 1px;
    box-shadow: 0 0 8px rgba(204, 110, 0, 0.18), inset 0 -4px 8px rgba(40, 20, 0, 0.35);
  }
  .bcol:hover i { background: linear-gradient(180deg, #f5972c 0%, #c26a08 70%, #8a4d00 100%); }
  .bcol b { font-size: 10px; color: #9a948a; font-weight: 700; margin-bottom: 3px; font-variant-numeric: tabular-nums; font-family: "IBM Plex Mono", "Courier New", monospace; text-shadow: 0 1px 1px #000; }
  .bcol s { text-decoration: none; font-size: 9.5px; color: #5a564c; margin-top: 5px; position: absolute; bottom: -15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; text-transform: uppercase; letter-spacing: 0.5px; }
  .legend { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }
  .legend span { font-size: 11.5px; color: #9a948a; display: inline-flex; align-items: center; gap: 5px; }
  .legend i { width: 10px; height: 10px; border-radius: 0; display: inline-block; border: 1px solid #000; box-shadow: 0 1px 1px rgba(0, 0, 0, 0.6); }
  .twisty { background: none; border: none; color: #5a564c; cursor: pointer; font-size: 10px; padding: 0 2px; font-family: inherit; line-height: 1; }
  .twisty:hover { color: #d98d2a; }
  .cardkebab { position: absolute; top: 8px; right: 10px; font-size: 16px; z-index: 5; }
  .cardmenu { top: 30px; right: 12px; min-width: 170px; }
  .focusline { font-size: 14.5px; color: #e8e2d4; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-shadow: 0 1px 1px #000; }
  .focusline.dim { color: #5a564c; }
  .focusinput { flex: 1; min-width: 0; background: none; border: none; outline: none; border-bottom: 1px solid #3a352a; color: #e8e2d4; font-size: 14.5px; font-family: inherit; padding: 2px 0; }

  /* ---------- daily stacked chart ---------- */
  .histplot { position: relative; height: 220px; margin: 20px 0 20px 30px; }
  .histgrid { position: absolute; left: 0; right: 0; top: 0; bottom: 20px; }
  .gline { position: absolute; left: 0; right: 0; border-top: 1px solid #201d16; }
  .gline s { position: absolute; left: -30px; top: -6px; width: 24px; text-align: right; text-decoration: none; font-size: 9.5px; color: #5a564c; font-variant-numeric: tabular-nums; font-family: "IBM Plex Mono", "Courier New", monospace; }
  .hist { position: absolute; left: 0; right: 0; top: 0; bottom: 20px; display: flex; align-items: flex-end; gap: 4px; }
  .hcol { position: relative; flex: 1; min-width: 0; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; }
  .hcol b { font-size: 10px; color: #9a948a; font-weight: 700; margin-bottom: 4px; height: 13px; line-height: 13px; white-space: nowrap; font-variant-numeric: tabular-nums; font-family: "IBM Plex Mono", "Courier New", monospace; text-shadow: 0 1px 1px #000; }
  .hstack { width: 100%; display: flex; flex-direction: column-reverse; border-radius: 0; overflow: hidden; background: #191712; box-shadow: inset 0 0 0 1px #201d16; }
  .hstack i { display: block; width: 100%; min-height: 2px; }
  .hcol s { position: absolute; bottom: -20px; left: 0; right: 0; text-align: center; text-decoration: none; font-size: 9.5px; color: #5a564c; text-transform: uppercase; }
  .mrow { display: flex; align-items: center; gap: 10px; padding: 3px 0; }
  .mrow s { text-decoration: none; flex: 0 0 34px; font-size: 11px; color: #7d786e; text-transform: uppercase; }
  .mbar { flex: 1; height: 10px; border-radius: 0; background: #100e0b; border: 1px solid #302b21; overflow: hidden; box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.5); }
  .mbar i { display: block; height: 100%; background: linear-gradient(180deg, #e0820f 0%, #a85a00 100%); border-right: 1px solid #f2ae5c; box-shadow: inset 0 1px 0 rgba(255, 210, 150, 0.35); }
  .mrow b { flex: 0 0 56px; text-align: right; font-size: 11px; color: #9a948a; font-variant-numeric: tabular-nums; font-weight: 700; font-family: "IBM Plex Mono", "Courier New", monospace; }

  /* ---------- tracker ---------- */
  .group {
    border: 1px solid #363127; border-top-color: #453e30;
    border-radius: 0; margin-bottom: 10px;
    background: linear-gradient(180deg, #171512 0%, #141210 100%);
    box-shadow: 0 3px 8px rgba(0, 0, 0, 0.5);
  }
  .ghead { display: flex; align-items: center; gap: 8px; padding: 8px 12px; cursor: pointer; user-select: none; border-radius: 0; }
  .ghead[draggable="true"] { cursor: grab; }
  .ghead[draggable="true"]:active { cursor: grabbing; }
  .ghead:hover { background: rgba(204, 110, 0, 0.06); }
  .ghead .delbtn { opacity: 0; }
  .ghead:hover .delbtn { opacity: 1; }
  .garrow { font-size: 8px; color: #5a564c; flex: none; }
  .gname { font-weight: 700; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 220px; color: #eda44f; text-transform: uppercase; letter-spacing: 1px; text-shadow: 0 1px 1px #000; }
  .gname:hover { color: #ffc46b; }
  .gbar { flex: 1; height: 6px; background: #100e0b; border: 1px solid #302b21; border-radius: 0; overflow: hidden; min-width: 40px; box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.5); }
  .gbar i { display: block; height: 100%; background: linear-gradient(180deg, #e0820f 0%, #a85a00 100%); border-right: 1px solid #f2ae5c; }
  .gpct { font-size: 11.5px; color: #d98d2a; font-variant-numeric: tabular-nums; width: 34px; text-align: right; font-weight: 700; flex: none; font-family: "IBM Plex Mono", "Courier New", monospace; }
  .gmeta { font-size: 10.5px; color: #5a564c; white-space: nowrap; flex: none; text-transform: uppercase; letter-spacing: 0.5px; }
  .gbody { padding: 0 12px 8px; border-top: 1px solid #1e1b15; border-radius: 0; }
  .subj { border-bottom: 1px solid #1e1b15; position: relative; }
  .subj:last-of-type { border-bottom: none; }
  .srow { display: flex; align-items: center; gap: 7px; padding: 5px 0; }
  .srow[draggable="true"] { cursor: grab; }
  .srow[draggable="true"]:active { cursor: grabbing; }
  .star { background: none; border: none; cursor: pointer; color: #45413a; font-size: 13px; padding: 0; width: 16px; flex: none; line-height: 1; opacity: 0.35; }
  .star.on { color: #e8a340; opacity: 1; text-shadow: 0 0 6px rgba(204, 110, 0, 0.4); }
  .srow:hover .star { opacity: 1; }
  .star:hover { color: #e8a340; }
  .kebab { background: none; border: none; cursor: pointer; color: #9a948a; font-size: 14px; padding: 0 2px; flex: none; line-height: 1; opacity: 0.35; }
  .srow:hover .kebab { opacity: 1; }
  .kebab:hover { color: #f0ead9; }
  .sname { font-size: 14.5px; cursor: pointer; flex: 1 1 140px; min-width: 120px; font-weight: 400; text-shadow: 0 1px 1px #000; }
  .todo .sname { color: #9a948a; }
  .doing .sname { color: #f0ead9; font-weight: 700; }
  .paused .sname { color: #6e675a; }
  .done .sname { color: #5a564c; text-decoration: line-through; }
  .dropped .sname { color: #45413a; text-decoration: line-through; }
  .slink { color: #5a564c; text-decoration: none; font-size: 11px; margin-left: 5px; }
  .slink:hover { color: #d98d2a; }
  .subj .statebtn { width: 22px; height: 22px; font-size: 12px; }
  .badge {
    font-size: 9.5px; padding: 1px 7px; border-radius: 0; font-weight: 700;
    cursor: pointer; white-space: nowrap; flex: none; font-family: inherit;
    text-transform: uppercase; letter-spacing: 1px;
    border: 1px solid transparent;
  }
  .badge.stale { background: rgba(224, 106, 58, 0.12); color: #e06a3a; border-color: rgba(224, 106, 58, 0.4); }
  .badge.zzz { background: rgba(255, 255, 255, 0.04); color: #8a8478; border-color: #3a352a; }
  .badge.hold { background: rgba(217, 180, 74, 0.10); color: #d9b44a; border-color: rgba(217, 180, 74, 0.35); }
  .qadd {
    background: linear-gradient(180deg, #35322a 0%, #26231d 100%);
    border: 1px solid; border-color: #4c463a #171410 #171410 #4c463a;
    color: #9a948a; border-radius: 0; font-size: 11px; padding: 2px 8px;
    cursor: pointer; font-family: inherit; flex: none;
  }
  .qadd:hover { color: #f0ead9; }
  .snoozebar { display: flex; gap: 6px; padding: 0 0 7px 25px; align-items: center; font-size: 11px; color: #7d786e; flex-wrap: wrap; }

  /* ---------- menus ---------- */
  .menu {
    position: absolute; right: 8px; top: 24px; z-index: 60; min-width: 132px;
    background: #1d1a15;
    border: 1px solid; border-color: #453e30 #171410 #171410 #453e30;
    border-radius: 0;
    box-shadow: 0 8px 22px rgba(0, 0, 0, 0.7);
    padding: 4px;
  }
  .menuhead {
    font-size: 10px; color: #c98d3f; padding: 5px 10px 6px;
    border-bottom: 1px solid #302b21; margin-bottom: 3px;
    font-variant-numeric: tabular-nums; font-weight: 700;
    text-transform: uppercase; letter-spacing: 1.5px;
    text-shadow: 0 1px 1px #000;
  }
  .menuitem {
    display: block; width: 100%; text-align: left; padding: 6px 10px;
    font-size: 12.5px; color: #c4bfb4; background: none; border: none;
    border-radius: 0; cursor: pointer; font-family: inherit;
  }
  .menuitem:hover { background: rgba(204, 110, 0, 0.12); color: #ffc46b; }
  .menuitem.danger { color: #e06a3a; }
  .menuitem.danger:hover { background: rgba(224, 106, 58, 0.12); color: #e06a3a; }
  .addsub { display: flex; gap: 6px; margin-top: 8px; align-items: center; }
  .addsub input[type=text] { flex: 1; min-width: 0; margin-bottom: 0; }
  .addsub .tool { padding: 5px 11px; }

  .hidden { display: none !important; }
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
    <div class="btitle"><span>&raquo; tasks</span><span class="rt">grind &middot; est. 1998</span></div>
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
    <div class="btitle"><span>&raquo; summary</span><span class="rt">export via &#8943;</span></div>
    <button class="kebab cardkebab" title="export" onclick="toggleCardMenu(event)">&#8943;</button>
    <div class="menu cardmenu" id="cardmenu" hidden>
      <div class="menuhead">classic</div>
      <button class="menuitem" onclick="exportCard('day')">export today</button>
      <button class="menuitem" onclick="exportCard('week')">export this week</button>
      <button class="menuitem" onclick="exportCard('month')">export this month</button>
      <div class="menuhead" style="margin-top:6px">retro 1998</div>
      <button class="menuitem" onclick="exportCard('day','retro')">export today</button>
      <button class="menuitem" onclick="exportCard('week','retro')">export this week</button>
      <button class="menuitem" onclick="exportCard('month','retro')">export this month</button>
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
  function exportCard(kind, style) {
    closeCardMenu();
    const base = kind === "day" ? "/card.png" : kind === "week" ? "/card-week.png" : "/card-month.png";
    window.open(style === "retro" ? base + "?style=retro" : base, "_blank");
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
    const shades = ["#e0820f", "#a85a00", "#8a8478", "#5a564c", "#c98d3f", "#7a4200", "#f2ae5c", "#363127"];
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

PAGE = PAGE.replace("__FONTS_CSS__", FONTS_CSS)
MANAGE = MANAGE.replace("__FONTS_CSS__", FONTS_CSS)


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
