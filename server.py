#!/usr/bin/env python3
"""OBS todo overlay + manage panel, backed by SQLite.

Pages:
  /        -> stream overlay (read-only, follows the server's current day)
  /manage  -> control panel (tasks, timer, log, summary)

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


def day_payload(conn, day_id: int):
    day = conn.execute("SELECT id, label, position FROM days WHERE id=?", (day_id,)).fetchone()
    if not day:
        return None
    tasks = conn.execute(
        "SELECT id, text, state FROM tasks WHERE day_id=? "
        "ORDER BY CASE state WHEN 'doing' THEN 0 WHEN 'todo' THEN 1 ELSE 2 END, position",
        (day_id,),
    ).fetchall()
    return {
        "day": {"id": day["id"], "label": day["label"]},
        "days": [
            {"id": r["id"], "label": r["label"]}
            for r in conn.execute("SELECT id, label FROM days ORDER BY position")
        ],
        "items": [dict(t) for t in tasks],
        "stats": {
            "done": sum(t["state"] == "done" for t in tasks),
            "total": len(tasks),
        },
    }


def manage_payload(conn) -> list:
    out = []
    for d in conn.execute("SELECT id, label, position FROM days ORDER BY position"):
        tasks = conn.execute(
            "SELECT id, text, details, state FROM tasks WHERE day_id=? "
            "ORDER BY CASE state WHEN 'doing' THEN 0 WHEN 'todo' THEN 1 ELSE 2 END, position",
            (d["id"],),
        ).fetchall()
        out.append({"id": d["id"], "label": d["label"], "tasks": [dict(t) for t in tasks]})
    return out


def next_position(conn, day_id: int) -> int:
    row = conn.execute("SELECT COALESCE(MAX(position),0)+1 AS p FROM tasks WHERE day_id=?", (day_id,)).fetchone()
    return row["p"]


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
        "session_id": None,
        "category_id": None,
        "task_id": None,
        "task_label": "",
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


def ensure_session(conn, st: dict, category_id=None):
    if st.get("session_id"):
        row = conn.execute("SELECT id FROM pomo_sessions WHERE id=?", (st["session_id"],)).fetchone()
        if row:
            return st["session_id"]
    cur = conn.execute(
        "INSERT INTO pomo_sessions (category_id, started_at) VALUES (?,?)",
        (category_id if category_id is not None else st.get("category_id"), now_iso()),
    )
    st["session_id"] = cur.lastrowid
    if category_id is not None:
        st["category_id"] = category_id
    return st["session_id"]


def log_period(conn, st: dict, minutes: int, started_at: str, ended_at: str):
    task_label = st.get("task_label") or ""
    if st.get("task_id"):
        row = conn.execute("SELECT text FROM tasks WHERE id=?", (st["task_id"],)).fetchone()
        if row:
            task_label = row["text"]
    conn.execute(
        "INSERT OR IGNORE INTO pomo_periods "
        "(session_id, category_id, task_id, task_label, minutes, started_at, ended_at, source) "
        "VALUES (?,?,?,?,?,?,?, 'timer')",
        (st.get("session_id"), st.get("category_id"), st.get("task_id"), task_label,
         minutes, started_at, ended_at),
    )


def log_partial(conn, st: dict):
    """Record unlogged focus time (called on pause/reset/session end)."""
    if st["phase"] != "focus" or not st.get("phase_started_at"):
        return
    elapsed = pomo_elapsed_ms(st)
    logged = int(st.get("logged_ms") or 0)
    unlogged = elapsed - logged
    if unlogged < 60000:
        return
    base = parse_iso(st["phase_started_at"])
    started = (base + timedelta(milliseconds=logged)).isoformat(timespec="seconds")
    ended = (base + timedelta(milliseconds=elapsed)).isoformat(timespec="seconds")
    log_period(conn, st, max(1, round(unlogged / 60000)), started, ended)
    st["logged_ms"] = elapsed


def notify_phase(phase: str):
    """System-level popup when a timer phase runs out."""
    if not shutil.which("notify-send"):
        return
    try:
        msg = "focus done" if phase == "focus" else "break over"
        subprocess.Popen(["notify-send", "-a", "obs-todo", "obs-todo", msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def close_session(conn, st: dict):
    """End the open session and freeze its tasks-done count."""
    if st.get("session_id"):
        row = conn.execute("SELECT started_at FROM pomo_sessions WHERE id=?", (st["session_id"],)).fetchone()
        if row:
            n = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE done_at IS NOT NULL AND done_at>=?",
                (row["started_at"],),
            ).fetchone()[0]
            conn.execute(
                "UPDATE pomo_sessions SET ended_at=COALESCE(ended_at,?), tasks_done=? WHERE id=?",
                (now_iso(), n, st["session_id"]),
            )


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
        base = parse_iso(st.get("phase_started_at") or st["started_at"])
        logged = int(st.get("logged_ms") or 0)
        rem_ms = max(60000, duration - logged)
        p_started = (base + timedelta(milliseconds=logged)).isoformat(timespec="seconds")
        p_ended = (base + timedelta(milliseconds=duration)).isoformat(timespec="seconds")
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

    task_label = st.get("task_label") or ""
    if st.get("task_id"):
        row = conn.execute("SELECT text FROM tasks WHERE id=?", (st["task_id"],)).fetchone()
        if row:
            task_label = row["text"]

    session = None
    if st.get("session_id"):
        row = conn.execute(
            "SELECT id, started_at, ended_at, tasks_done FROM pomo_sessions WHERE id=?",
            (st["session_id"],),
        ).fetchone()
        if row:
            agg = conn.execute(
                "SELECT COALESCE(SUM(minutes),0), COUNT(*) FROM pomo_periods WHERE session_id=?",
                (row["id"],),
            ).fetchone()
            done = row["tasks_done"]
            if row["ended_at"] is None:
                done = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE done_at IS NOT NULL AND done_at>=?",
                    (row["started_at"],),
                ).fetchone()[0]
            session = {
                "id": row["id"], "started_at": row["started_at"],
                "focused_min": agg[0], "periods": agg[1], "tasks_done": done,
            }

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
        "task": {"id": st.get("task_id"), "label": task_label} if task_label or st.get("task_id") else None,
        "session": session,
        "today": {"focused_min": today_min, "periods": today[1]},
    }


def pomo_action(conn, action: str, body: dict):
    st = pomo_tick(conn)
    if action == "start":
        elapsed = pomo_elapsed_ms(st)
        if elapsed >= int(st["duration_min"]) * 60000 or not st.get("phase_started_at"):
            fresh = dict(st)
            fresh["elapsed_ms"] = 0
            fresh["running"] = False
            st = new_phase(conn, fresh, st["phase"], running=True)
        else:
            st["running"] = True
            st["started_at"] = now_iso()
        ensure_session(conn, st)
        save_pomo_state(conn, st)
    elif action == "pause":
        if st["running"]:
            st["elapsed_ms"] = pomo_elapsed_ms(st)
            st["running"] = False
            st["started_at"] = None
            log_partial(conn, st)
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
        st = new_phase(conn, st, "break" if st["phase"] == "focus" else "focus", running=False)
        save_pomo_state(conn, st)
    elif action == "set":
        if "category_id" in body:
            cid = body["category_id"]
            if cid:
                if not conn.execute("SELECT 1 FROM categories WHERE id=?", (cid,)).fetchone():
                    raise ValueError("category not found")
            st["category_id"] = int(cid) if cid else None
            if st.get("session_id"):
                conn.execute("UPDATE pomo_sessions SET category_id=? WHERE id=?",
                             (st["category_id"], st["session_id"]))
        if "task_id" in body:
            tid = body["task_id"]
            if tid:
                row = conn.execute("SELECT id, text FROM tasks WHERE id=?", (tid,)).fetchone()
                if not row:
                    raise ValueError("task not found")
                st["task_id"] = row["id"]
                st["task_label"] = row["text"]
            else:
                st["task_id"] = None
                st["task_label"] = ""
        save_pomo_state(conn, st)
    elif action == "new_session":
        cid = body.get("category_id")
        if cid:
            if not conn.execute("SELECT 1 FROM categories WHERE id=?", (cid,)).fetchone():
                raise ValueError("category not found")
        log_partial(conn, st)
        close_session(conn, st)
        st["session_id"] = None
        if cid is not None:
            st["category_id"] = int(cid) if cid else None
        ensure_session(conn, st)
        st = new_phase(conn, st, "focus", running=False)
        save_pomo_state(conn, st)
    elif action == "end_session":
        log_partial(conn, st)
        close_session(conn, st)
        st["session_id"] = None
        st = new_phase(conn, st, "focus", running=False)
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

    daily = conn.execute(
        "SELECT substr(p.started_at,1,10) AS d, COALESCE(c.name,'Unallocated') AS name, SUM(p.minutes) AS m "
        "FROM pomo_periods p LEFT JOIN categories c ON c.id=p.category_id "
        "WHERE substr(p.started_at,1,10)>=? GROUP BY d, name",
        ((date.today() - timedelta(days=6)).isoformat(),),
    ).fetchall()
    by_day = {}
    for r in daily:
        e = by_day.setdefault(r["d"], {"cats": {}, "total": 0})
        e["cats"][r["name"]] = e["cats"].get(r["name"], 0) + r["m"]
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


# ---------------------------------------------------------------- pages

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>todo overlay</title>
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
  .daynav {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
  }
  .daynav button {
    background: none;
    border: 1px solid rgba(255,255,255,0.14);
    color: #9b9b9e;
    border-radius: 8px;
    font-size: 16px;
    width: 32px;
    height: 28px;
    cursor: pointer;
  }
  .daynav button:hover { color: #eaeaec; border-color: rgba(255,255,255,0.32); }
  .daylabel { font-size: 14px; font-weight: 600; color: #9b9b9e; letter-spacing: 0.4px; }

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

  function shift(dir) {
    if (!data) return;
    const idx = data.days.findIndex(x => x.id === data.day.id);
    const target = data.days[idx + dir];
    if (!target) return;
    fetch("/api/current-day", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({day_id: target.id})
    }).then(tick);
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
      const idx = data.days.findIndex(x => x.id === data.day.id);
      let html = `
        <div class="daynav">
          <button onclick="shift(-1)">&#9664;</button>
          <span class="daylabel">${esc(data.day.label)} &middot; ${idx + 1}/${data.days.length}</span>
          <button onclick="shift(1)">&#9654;</button>
        </div>
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
<title>obs-todo</title>
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
  .tabs { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 12px; align-items: center; }
  .tab {
    padding: 5px 10px; border: none; background: none;
    color: #57575a; cursor: pointer; font-size: 13px; border-radius: 8px;
    font-family: inherit;
  }
  .tab:hover { color: #9b9b9e; }
  .tab.current {
    background: #eaeaec; color: #161618; font-weight: 700;
    padding: 7px 15px; font-size: 13.5px;
  }
  .tab .edit { opacity: 0.45; margin-left: 7px; font-size: 11px; cursor: text; }
  .tab .edit:hover { opacity: 1; }
  .tab.current .edit { opacity: 0.6; }
  .tab input.rename {
    background: transparent; border: none; outline: none;
    border-bottom: 1px solid currentColor; color: inherit;
    font: inherit; font-weight: 700; width: 90px; padding: 0;
  }
  .card {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 12px;
    padding: 10px 14px;
  }
  .bottombar { display: flex; gap: 8px; margin-top: 12px; align-items: center; }
  .bottombar .spacer { flex: 1; }
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
  .focusbtn {
    background: none; border: 1px solid rgba(255,255,255,0.14); color: #57575a;
    border-radius: 7px; font-size: 11px; padding: 3px 8px; cursor: pointer; font-family: inherit;
  }
  .focusbtn:hover { color: #eaeaec; border-color: rgba(255,255,255,0.30); }
  .focusbtn.on { color: #161618; background: #eaeaec; border-color: #eaeaec; font-weight: 600; }
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
  .empty { color: #57575a; padding: 16px 0; text-align: center; font-size: 13px; }
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
  .ttime {
    font-size: 46px; font-weight: 700; font-variant-numeric: tabular-nums;
    letter-spacing: 1px; line-height: 1.1; color: #9b9b9e;
  }
  .tbox.run .ttime, .tbox.end .ttime { color: #ffffff; }
  .tbox.end .ttime { animation: blink 1.1s ease-in-out infinite; }
  @keyframes blink { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  .tctrl { display: flex; gap: 8px; margin-top: 16px; }
  .tsum { margin-top: 14px; font-size: 12.5px; color: #57575a; }
  .tsum b { color: #9b9b9e; font-weight: 600; }
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
  .setbody { padding: 16px 4px 4px; display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
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

  .statsgrid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 16px; }
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
  .drow { display: flex; align-items: center; gap: 10px; padding: 3px 0; }
  .drow s { text-decoration: none; flex: 0 0 58px; font-size: 11px; color: #57575a; }
  .drow .dbar { flex: 1; height: 10px; border-radius: 5px; background: rgba(255,255,255,0.06); display: flex; overflow: hidden; }
  .drow .dbar i { display: block; height: 100%; }
  .drow b { flex: 0 0 56px; text-align: right; font-size: 11px; color: #9b9b9e; font-variant-numeric: tabular-nums; font-weight: 600; }
  .srow { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; padding: 5px 0; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 12px; color: #9b9b9e; }
  .srow:last-child { border-bottom: none; }
  .srow .num { font-variant-numeric: tabular-nums; color: #cfcfd2; }
</style>
</head>
<body>
<nav class="nav">
  <span class="brand">obs-todo</span>
  <span class="nlinks">
    <button data-v="tasks" class="cur" onclick="switchView('tasks')">tasks</button>
    <button data-v="timer" onclick="switchView('timer')">timer</button>
    <button data-v="summary" onclick="switchView('summary')">summary</button>
    <button data-v="log" onclick="switchView('log')">log</button>
  </span>
</nav>

<div id="view-tasks">
  <div class="tabs" id="tabs"></div>
  <div class="card"><div id="list"></div>
    <div class="addbar">
      <input type="text" id="newtext" placeholder="new task... (Enter)" onkeydown="if(event.key==='Enter')addTask()">
      <button class="tool pri" onclick="addTask()">add</button>
    </div>
  </div>
  <div class="bottombar">
    <button class="tool pri" title="add day" onclick="addDay()">+</button>
    <button class="tool" title="carry unfinished to next day" onclick="carry()">&rarr;</button>
    <button class="tool hidden" id="undobtn" onclick="undoCarry()">undo carry</button>
    <span class="spacer"></span>
    <button class="tool danger" title="delete day" onclick="delDay()">&#128465;</button>
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
  <details class="setdrop">
    <summary>settings</summary>
    <div class="setbody">
      <div class="setcol">
        <div class="ctitle">session</div>
        <div class="meta" id="sessmeta" style="margin-bottom:10px">no session</div>
        <div class="prow">
          <button class="tool" onclick="pomoAct('new_session')">new session</button>
          <button class="tool" onclick="pomoAct('end_session')">end session</button>
        </div>
        <div class="ctitle" style="margin-top:14px">past sessions</div>
        <div class="prow">
          <select class="tinput" id="sesssort" onchange="renderSessions()" style="width:auto; margin-bottom:0">
            <option value="latest">latest added</option>
            <option value="oldest">oldest first</option>
            <option value="longest">longest</option>
          </select>
        </div>
        <div id="sesslist" style="max-height:220px; overflow-y:auto"></div>
      </div>
      <div class="setcol">
        <div class="ctitle">timer</div>
        <div class="prow">
          <span class="lbl" style="margin:0 6px 0 0">focus</span><input class="tinput" id="cfg-focus" type="number" min="1" max="600" style="width:70px; margin-bottom:0">
          <span class="lbl" style="margin:0 6px 0 10px">break</span><input class="tinput" id="cfg-break" type="number" min="1" max="600" style="width:70px; margin-bottom:0">
        </div>
        <div class="prow">
          <label class="lbl" style="margin:0; text-transform:none; letter-spacing:0; font-size:12.5px; color:#9b9b9e"><input type="checkbox" id="cfg-auto"> auto-start next phase</label>
        </div>
        <div class="prow">
          <button class="tool" onclick="saveCfg()">save</button>
        </div>
      </div>
    </div>
  </details>
</div>

<div id="view-summary" class="hidden">
  <div class="statsgrid" id="statgrid"></div>
  <div class="card">
    <div class="ctitle">last 7 days by category</div>
    <div id="ch-daily"></div>
    <div class="ctitle" style="margin-top:22px">weekly</div>
    <div class="bars" id="ch-weeks"></div>
    <div class="ctitle" style="margin-top:22px">monthly</div>
    <div class="bars" id="ch-months"></div>
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

<script>
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const GLYPH = {todo: "&#9675;", doing: "&#9679;", done: "&#10003;"};
  const $ = id => document.getElementById(id);
  let view = "tasks";
  let days = [], currentDayId = null, selectedId = null, openId = null;
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
    ["tasks","timer","summary","log"].forEach(x => $("view-" + x).classList.toggle("hidden", x !== v));
    if (v === "tasks") load(true);
    if (v === "timer") { if (!tickTimer) tickTimer = setInterval(renderTime, 250); loadSessions(); }
    else if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
    if (v === "summary") loadSummary();
    if (v === "log") loadLog();
  }

  // ---------- tasks view
  async function load(keepOpen=false) {
    days = await api("/api/days");
    const cur = await (await fetch("/api/todo")).json();
    currentDayId = cur.day.id;
    if (!selectedId || !days.find(d => d.id === selectedId)) selectedId = currentDayId;
    if (!keepOpen) openId = null;
    renderTasks();
    refreshUndo();
  }

  function renderTasks() {
    $("tabs").innerHTML = days.map(d =>
      `<button class="tab ${d.id === currentDayId ? "current" : ""}" onclick="selectDay(${d.id})" title="click: select + push to stream">${esc(d.label)}${d.id === currentDayId ? `<span class="edit" title="rename day" onclick="renameDay(${d.id}, event)">&#9998;</span>` : ""}</button>`
    ).join("");
    const day = days.find(d => d.id === selectedId);
    const list = $("list");
    if (!day || !day.tasks.length) {
      list.innerHTML = `<div class="empty">no tasks yet</div>`;
      return;
    }
    list.innerHTML = day.tasks.map(t => `
      <div class="task ${t.state}">
        <div class="row">
          <button class="statebtn" title="cycle state" onclick="cycleState(${t.id},'${t.state}')">${GLYPH[t.state]}</button>
          <span class="ttext" onclick="toggleEditor(${t.id})">${esc(t.text)}${t.details ? '<span class="hasnotes">&#9998;</span>' : ""}</span>
          <button class="focusbtn ${pomo && pomo.task && pomo.task.id === t.id ? "on" : ""}" title="bind/unbind to timer" onclick="bindTask(${t.id})">&rarr; timer</button>
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
      </div>`).join("");
  }

  function toggleEditor(id) {
    openId = openId === id ? null : id;
    renderTasks();
    if (openId === id) $(`txt-${id}`).focus();
  }

  function renameDay(id, ev) {
    ev.stopPropagation();
    const tab = ev.target.closest(".tab");
    const day = days.find(d => d.id === id);
    const input = document.createElement("input");
    input.className = "rename";
    input.value = day.label;
    tab.textContent = "";
    tab.appendChild(input);
    input.focus();
    input.select();
    let done = false;
    const finish = ok => {
      if (done) return;
      done = true;
      const label = ok ? input.value.trim() : "";
      const p = (label && label !== day.label)
        ? api(`/api/days/${id}`, "PATCH", {label})
        : Promise.resolve();
      p.then(() => load(true), () => load(true));
    };
    input.onkeydown = e => {
      e.stopPropagation();
      if (e.key === "Enter") finish(true);
      if (e.key === "Escape") finish(false);
    };
    input.onblur = () => finish(true);
    input.onclick = e => e.stopPropagation();
  }

  async function selectDay(id) {
    selectedId = id;
    if (id !== currentDayId) await api("/api/current-day", "POST", {day_id: id});
    await load();
  }

  async function cycleState(id, state) {
    const next = {todo: "doing", doing: "done", done: "todo"}[state];
    await api(`/api/tasks/${id}`, "PATCH", {state: next});
    await load(true);
  }

  async function bindTask(id) {
    const on = pomo && pomo.task && pomo.task.id === id;
    pomo = await api("/api/pomodoro", "POST", {action: "set", task_id: on ? null : id});
    renderTasks();
    renderPomo();
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

  async function addTask() {
    const el = $("newtext");
    const text = el.value.trim();
    if (!text) return;
    await api("/api/tasks", "POST", {day_id: selectedId, text});
    el.value = "";
    await load(true);
  }

  async function addDay() {
    const d = await api("/api/days", "POST", {});
    selectedId = d.id;
    await api("/api/current-day", "POST", {day_id: d.id});
    await load();
  }

  async function delDay() {
    if (!confirm(`delete "${days.find(d => d.id === selectedId)?.label}" and its tasks?`)) return;
    await api(`/api/days/${selectedId}`, "DELETE");
    await load();
  }

  async function carry() {
    const res = await api(`/api/days/${selectedId}/carry`, "POST", {});
    selectedId = res.moved_to;
    await api("/api/current-day", "POST", {day_id: res.moved_to});
    await load();
  }

  async function refreshUndo() {
    try {
      const u = await (await fetch("/api/days/undo-carry")).json();
      $("undobtn").classList.toggle("hidden", !u.available);
    } catch (e) {}
  }

  async function undoCarry() {
    await api("/api/days/undo-carry", "POST", {});
    selectedId = null;
    await load();
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
    document.title = fmtClock(ms) + " · " + fmtHM(pomo.today.focused_min) + " · obs-todo";
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
    renderTasks();
    if (action === "new_session" || action === "end_session") loadSessions();
  }
  async function saveCfg() {
    pomo = await api("/api/pomodoro", "POST", {action: "config",
      focus_min: parseInt($("cfg-focus").value, 10),
      break_min: parseInt($("cfg-break").value, 10),
      auto_next: $("cfg-auto").checked});
    renderPomo();
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
    const sm = $("sessmeta");
    if (pomo.session) {
      const s = pomo.session;
      sm.innerHTML = `focused <b>${fmtHM(s.focused_min)}</b> &middot; ${s.periods} period${s.periods === 1 ? "" : "s"} &middot; <b>${s.tasks_done}</b> task${s.tasks_done === 1 ? "" : "s"} closed`;
    } else sm.textContent = "no session";
    $("cfg-focus").value = pomo.focus_min;
    $("cfg-break").value = pomo.break_min;
    $("cfg-auto").checked = pomo.auto_next;
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
    drawBars($("ch-months"), s.months.map(m => ({l: m.label, v: m.min})));
  }
  function drawDaily(el, days) {
    const cats = [];
    days.forEach(d => d.cats.forEach(c => { if (!cats.includes(c.name)) cats.push(c.name); }));
    const shades = ["#eaeaec", "#8f8f93", "#57575a", "#c4c4c8", "#3f3f43", "#a8a8ac", "#6f6f73", "#2e2e31"];
    const shade = name => shades[cats.indexOf(name) % shades.length];
    const legend = cats.length
      ? `<div class="legend">` + cats.map(c => `<span><i style="background:${shade(c)}"></i>${esc(c)}</span>`).join("") + `</div>`
      : `<div class="empty" style="padding:4px 0">no focus in the last 7 days</div>`;
    const rows = days.map(d => {
      const segs = d.total ? d.cats.map(c =>
        `<i style="flex:${c.min}; background:${shade(c.name)}" title="${esc(c.name)}: ${fmtHM(c.min)}"></i>`).join("") : "";
      return `<div class="drow"><s>${esc(d.label)} ${d.date.slice(8)}</s><div class="dbar">${segs}</div><b>${fmtHM(d.total)}</b></div>`;
    }).join("");
    el.innerHTML = legend + rows;
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

  // ---------- sessions list
  let sessions = [];
  async function loadSessions() {
    try {
      sessions = await api("/api/pomodoro/sessions");
      renderSessions();
    } catch (e) {}
  }
  function renderSessions() {
    const el = $("sesslist");
    if (!el) return;
    const sort = $("sesssort") ? $("sesssort").value : "latest";
    const cur = pomo && pomo.session ? pomo.session.id : null;
    const rows = sessions.filter(s => s.id !== cur);
    rows.sort((a, b) =>
      sort === "oldest" ? a.started_at.localeCompare(b.started_at)
      : sort === "longest" ? b.focused_min - a.focused_min
      : b.started_at.localeCompare(a.started_at));
    el.innerHTML = rows.map(s => `<div class="srow">
        <span>${esc(s.started_at.slice(5, 10))} &middot; ${s.started_at.slice(11, 16)} ~ ${s.ended_at ? s.ended_at.slice(11, 16) : "now"}</span>
        <span class="num">${fmtHM(s.focused_min)} &middot; ${s.periods}p &middot; ${s.tasks_done}t</span>
      </div>`).join("") || `<div class="empty" style="padding:8px 0">no past sessions</div>`;
  }

  // ---------- log view
  async function loadLog() {
    logData = await api("/api/pomodoro/log?days=60");
    const rows = logData.periods.map(p => {
      const d = p.started_at.slice(0, 10), st = p.started_at.slice(11, 16), en = p.ended_at.slice(11, 16);
      return `<tr>
        <td>${esc(d)}</td><td>${st} ~ ${en}</td>
        <td>${esc(p.category_name || "Unallocated")}</td>
        <td>${esc(p.task_label || "")}</td>
        <td class="num">${p.minutes}</td>
        <td class="num"><button class="delbtn" onclick="delPeriod(${p.id})">&#10005;</button></td>
      </tr>`;
    }).join("");
    $("logtable").innerHTML = `<table class="t">
      <tr><th>date</th><th>range</th><th>category</th><th>task</th><th class="num">min</th><th></th></tr>${rows || `<tr><td colspan="6" class="empty">no periods</td></tr>`}</table>`;
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

  document.addEventListener("keydown", e => {
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(t.tagName))) return;
    if (e.code === "Space" && !e.altKey && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      if (pomo) pomoToggle();
      return;
    }
    if (e.altKey && !e.ctrlKey && !e.metaKey) {
      const map = {Digit1: "tasks", Digit2: "timer", Digit3: "summary", Digit4: "log",
                   Numpad1: "tasks", Numpad2: "timer", Numpad3: "summary", Numpad4: "log"};
      if (map[e.code]) { e.preventDefault(); switchView(map[e.code]); }
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
                elif path == "/manage":
                    self._send(200, MANAGE.encode())
                elif path == "/api/todo":
                    payload = day_payload(conn, current_day_id(conn))
                    self._json(200, payload)
                elif path == "/api/days":
                    self._json(200, manage_payload(conn))
                elif path == "/api/days/undo-carry":
                    self._json(200, {"available": bool(get_setting(conn, "last_carry"))})
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
                elif path == "/api/pomodoro/sessions":
                    rows = conn.execute(
                        "SELECT s.id, s.started_at, s.ended_at, s.tasks_done, "
                        "COALESCE((SELECT SUM(p.minutes) FROM pomo_periods p WHERE p.session_id=s.id),0) AS focused_min, "
                        "COALESCE((SELECT COUNT(*) FROM pomo_periods p WHERE p.session_id=s.id),0) AS periods "
                        "FROM pomo_sessions s ORDER BY s.started_at DESC LIMIT 100"
                    ).fetchall()
                    out = []
                    for r in rows:
                        done = r["tasks_done"]
                        if r["ended_at"] is None:
                            done = conn.execute(
                                "SELECT COUNT(*) FROM tasks WHERE done_at IS NOT NULL AND done_at>=?",
                                (r["started_at"],),
                            ).fetchone()[0]
                        out.append({
                            "id": r["id"], "started_at": r["started_at"], "ended_at": r["ended_at"],
                            "focused_min": r["focused_min"], "periods": r["periods"], "tasks_done": done,
                        })
                    self._json(200, out)
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
                if self.path == "/api/days":
                    day = create_day(conn, (body.get("label") or "").strip() or None)
                    self._json(201, {"id": day["id"], "label": day["label"]})
                elif self.path == "/api/days/undo-carry":
                    raw = get_setting(conn, "last_carry")
                    if not raw:
                        return self._json(400, {"error": "nothing to undo"})
                    try:
                        snap = json.loads(raw)
                    except json.JSONDecodeError:
                        return self._json(400, {"error": "corrupt snapshot"})
                    restored = 0
                    for t in snap.get("tasks", []):
                        if conn.execute("SELECT 1 FROM tasks WHERE id=?", (t["id"],)).fetchone():
                            conn.execute("UPDATE tasks SET day_id=?, position=? WHERE id=?",
                                         (t["day_id"], t["position"], t["id"]))
                            restored += 1
                    set_setting(conn, "last_carry", "")
                    conn.commit()
                    self._json(200, {"restored": restored})
                elif self.path == "/api/current-day":
                    day = conn.execute("SELECT id FROM days WHERE id=?", (body.get("day_id"),)).fetchone()
                    if not day:
                        return self._json(404, {"error": "day not found"})
                    set_setting(conn, "current_day_id", day["id"])
                    conn.commit()
                    self._json(200, {"current_day_id": day["id"]})
                elif self.path == "/api/tasks":
                    day = conn.execute("SELECT id FROM days WHERE id=?", (body.get("day_id"),)).fetchone()
                    text = (body.get("text") or "").strip()
                    if not day:
                        return self._json(404, {"error": "day not found"})
                    if not text:
                        return self._json(400, {"error": "text required"})
                    cur = conn.execute(
                        "INSERT INTO tasks (day_id, text, details, position) VALUES (?,?,?,?)",
                        (day["id"], text, (body.get("details") or "").strip(), next_position(conn, day["id"])),
                    )
                    conn.commit()
                    self._json(201, {"id": cur.lastrowid})
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
                elif self.path.endswith("/carry"):
                    day_id = self._path_id(r"^/api/days/(\d+)/carry$")
                    day = conn.execute("SELECT * FROM days WHERE id=?", (day_id,)).fetchone()
                    if not day:
                        return self._json(404, {"error": "day not found"})
                    nxt = conn.execute(
                        "SELECT * FROM days WHERE position>? ORDER BY position LIMIT 1", (day["position"],)
                    ).fetchone()
                    if not nxt:
                        nxt = create_day(conn)
                    src_rows = conn.execute(
                        "SELECT id, day_id, position FROM tasks WHERE day_id=? AND state!='done' ORDER BY position",
                        (day_id,),
                    ).fetchall()
                    set_setting(conn, "last_carry", json.dumps({
                        "to_day": nxt["id"],
                        "tasks": [{"id": t["id"], "day_id": t["day_id"], "position": t["position"]} for t in src_rows],
                    }))
                    base = next_position(conn, nxt["id"])
                    for n, t in enumerate(src_rows):
                        conn.execute(
                            "UPDATE tasks SET day_id=?, position=? WHERE id=?",
                            (nxt["id"], base + n, t["id"]),
                        )
                    conn.commit()
                    self._json(200, {"moved": len(src_rows), "moved_to": nxt["id"]})
                else:
                    self._json(404, {"error": "not found"})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            finally:
                conn.close()

        def do_PATCH(self):
            conn = get_db(db_path)
            try:
                day_id = self._path_id(r"^/api/days/(\d+)$")
                if day_id:
                    day = conn.execute("SELECT id FROM days WHERE id=?", (day_id,)).fetchone()
                    if not day:
                        return self._json(404, {"error": "day not found"})
                    label = (self._body().get("label") or "").strip()
                    if not label:
                        return self._json(400, {"error": "label cannot be empty"})
                    conn.execute("UPDATE days SET label=? WHERE id=?", (label, day_id))
                    conn.commit()
                    return self._json(200, {"id": day_id, "label": label})
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
                day_id = self._path_id(r"^/api/days/(\d+)$")
                if day_id:
                    total = conn.execute("SELECT COUNT(*) FROM days").fetchone()[0]
                    if total <= 1:
                        return self._json(400, {"error": "cannot delete the last day"})
                    if not conn.execute("SELECT 1 FROM days WHERE id=?", (day_id,)).fetchone():
                        return self._json(404, {"error": "day not found"})
                    conn.execute("DELETE FROM days WHERE id=?", (day_id,))
                    set_setting(conn, "current_day_id", current_day_id(conn))
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
