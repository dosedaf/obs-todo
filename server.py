#!/usr/bin/env python3
"""OBS todo overlay + manage panel, backed by SQLite.

Pages:
  /        -> stream overlay (read-only, follows the server's current day)
  /manage  -> control panel (days, tasks, details, carry-over)

Usage: python3 server.py [--file ../todolist.txt] [--db todo.db] [--port 8787]
"""

import argparse
import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ITEM_RE = re.compile(r"^\[\s*([XO]?)\s*\]\s*(.*)$")
STATE_MAP = {"X": "done", "O": "doing", "": "todo"}

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
    color: #e6e9f2;
  }
  .card {
    margin: 8px;
    padding: 14px 18px;
    background: rgba(13, 16, 28, 0.96);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 16px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45);
    overflow: hidden;
    height: calc(100% - 16px);
    box-sizing: border-box;
  }
  .daynav {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 6px;
  }
  .daynav button {
    background: none;
    border: 1px solid rgba(255,255,255,0.15);
    color: #8b93a7;
    border-radius: 8px;
    font-size: 17px;
    width: 34px;
    height: 30px;
    cursor: pointer;
  }
  .daynav button:hover { color: #e6e9f2; border-color: rgba(255,255,255,0.35); }
  .daylabel { font-size: 15px; font-weight: 700; color: #aab1c5; letter-spacing: 0.4px; }
  h1 {
    margin: 0 0 2px;
    font-size: 27px;
    font-weight: 700;
    letter-spacing: 0.2px;
  }
  .stats {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 15px;
    color: #8b93a7;
    margin-bottom: 6px;
  }
  .bar {
    height: 8px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.08);
    overflow: hidden;
    margin-bottom: 10px;
  }
  .bar-fill {
    height: 100%;
    width: 0;
    border-radius: 4px;
    background: linear-gradient(90deg, #34d399, #22d3ee);
    transition: width 0.6s ease;
  }
  ul { list-style: none; margin: 0; padding: 0; }
  li {
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 5px 0;
    font-size: 21px;
    line-height: 1.4;
  }
  .box {
    flex: none;
    width: 22px;
    text-align: center;
    font-size: 19px;
  }
  .done .box { color: #34d399; }
  .done .label { color: #6ee7b7; text-decoration: line-through; text-decoration-color: rgba(52, 211, 153, 0.6); }
  .doing .box { color: #fbbf24; animation: pulse 1.6s ease-in-out infinite; }
  .doing .label { color: #fcd34d; font-weight: 600; }
  .todo .box { color: #4b5265; }
  .todo .label { color: #aab1c5; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
</style>
</head>
<body>
<div class="card" id="card"></div>
<script>
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  let data = null;

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

  async function tick() {
    try {
      data = await (await fetch("/api/todo")).json();
      const pct = data.stats.total ? Math.round(100 * data.stats.done / data.stats.total) : 0;
      const idx = data.days.findIndex(x => x.id === data.day.id);
      let html = `
        <div class="daynav">
          <button onclick="shift(-1)">&#9664;</button>
          <span class="daylabel">${esc(data.day.label)} &middot; ${idx + 1}/${data.days.length}</span>
          <button onclick="shift(1)">&#9654;</button>
        </div>
        <h1>${esc(data.day.label)}</h1>
        <div class="stats"><span>${data.stats.done} / ${data.stats.total} done</span><span>${pct}%</span></div>
        <div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div>
        <ul>` + data.items.map(i =>
          `<li class="${i.state}"><span class="box">${i.state === "done" ? "&#10003;" : i.state === "doing" ? "&#9679;" : "&#9675;"}</span><span class="label">${esc(i.text)}</span></li>`
        ).join("") + "</ul>";
      document.getElementById("card").innerHTML = html;
    } catch (e) { /* keep last render */ }
  }
  tick();
  setInterval(tick, 1500);
</script>
</body>
</html>"""

MANAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>todo manage</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0;
    background: #0d101c;
    color: #e6e9f2;
    font-family: "Inter", "Segoe UI", system-ui, sans-serif;
    padding: 24px;
    max-width: 720px;
    margin: 0 auto;
  }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .hint { color: #8b93a7; font-size: 13px; margin-bottom: 18px; }
  .tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
  .tab {
    padding: 7px 14px;
    border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.12);
    background: rgba(255,255,255,0.04);
    color: #aab1c5;
    cursor: pointer;
    font-size: 14px;
  }
  .tab.current { background: rgba(52, 211, 153, 0.15); border-color: #34d399; color: #6ee7b7; font-weight: 600; }
  .tab .edit { opacity: 0.45; margin-left: 7px; font-size: 12px; cursor: text; }
  .tab .edit:hover { opacity: 1; }
  .tab input.rename {
    background: transparent; border: none; outline: none;
    border-bottom: 1px solid #34d399; color: #6ee7b7;
    font: inherit; font-weight: 600; width: 90px; padding: 0;
  }
  .toolbar { display: flex; gap: 8px; margin-bottom: 18px; flex-wrap: wrap; }
  .tool {
    padding: 7px 12px;
    border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.12);
    background: none;
    color: #8b93a7;
    cursor: pointer;
    font-size: 13px;
  }
  .tool:hover { color: #e6e9f2; border-color: rgba(255,255,255,0.3); }
  .tool.danger:hover { color: #f87171; border-color: #f87171; }
  .card {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 14px;
    padding: 10px 14px;
  }
  .task { border-bottom: 1px solid rgba(255,255,255,0.06); }
  .task:last-child { border-bottom: none; }
  .row { display: flex; align-items: center; gap: 10px; padding: 9px 0; }
  .statebtn {
    flex: none;
    width: 30px; height: 30px;
    border-radius: 8px;
    border: 1px solid rgba(255,255,255,0.15);
    background: none;
    cursor: pointer;
    font-size: 15px;
  }
  .todo .statebtn { color: #4b5265; }
  .doing .statebtn { color: #fbbf24; }
  .done .statebtn { color: #34d399; }
  .ttext { flex: 1; font-size: 15.5px; cursor: pointer; }
  .todo .ttext { color: #aab1c5; }
  .doing .ttext { color: #fcd34d; font-weight: 600; }
  .done .ttext { color: #6ee7b7; text-decoration: line-through; }
  .hasnotes { font-size: 11px; color: #6b7386; margin-left: 6px; }
  .delbtn { background: none; border: none; color: #4b5265; cursor: pointer; font-size: 15px; }
  .delbtn:hover { color: #f87171; }
  .editor { padding: 4px 0 14px 40px; display: none; }
  .editor.open { display: block; }
  .editor input[type=text], .editor textarea {
    width: 100%;
    box-sizing: border-box;
    background: rgba(0,0,0,0.35);
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 8px;
    color: #e6e9f2;
    padding: 8px 10px;
    font-size: 14px;
    font-family: inherit;
    margin-bottom: 8px;
  }
  .editor textarea { min-height: 70px; resize: vertical; }
  .editor label { font-size: 11px; color: #6b7386; text-transform: uppercase; letter-spacing: 1px; display: block; margin-bottom: 4px; }
  .addbar { display: flex; gap: 8px; margin-top: 14px; }
  .addbar input {
    flex: 1;
    background: rgba(0,0,0,0.35);
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 10px;
    color: #e6e9f2;
    padding: 10px 12px;
    font-size: 15px;
    font-family: inherit;
  }
  .addbar button {
    padding: 10px 18px;
    border-radius: 10px;
    border: 1px solid #34d399;
    background: rgba(52,211,153,0.12);
    color: #6ee7b7;
    cursor: pointer;
    font-size: 15px;
    font-weight: 600;
  }
  .empty { color: #4b5265; padding: 18px 0; text-align: center; font-size: 14px; }
  .saved { color: #34d399; font-size: 12px; margin-left: 8px; opacity: 0; transition: opacity .4s; }
  .saved.show { opacity: 1; }
</style>
</head>
<body>
<h1>Todo manage</h1>
<div class="hint">Click a day tab to select it AND push it to the stream overlay. Click a task's text to edit it and add details. &#9998; on a tab renames the day. Ongoing tasks float to the top, done tasks sink to the bottom.</div>
<div class="toolbar">
  <button class="tool" onclick="addDay()">+ day</button>
  <button class="tool" onclick="carry()">carry unfinished &rarr; next day</button>
  <button class="tool danger" onclick="delDay()">delete day</button>
</div>
<div class="tabs" id="tabs"></div>
<div class="card"><div id="list"></div>
  <div class="addbar">
    <input type="text" id="newtext" placeholder="new task... (Enter)" onkeydown="if(event.key==='Enter')addTask()">
    <button onclick="addTask()">add</button>
  </div>
</div>
<script>
  const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const GLYPH = {todo: "&#9675;", doing: "&#9679;", done: "&#10003;"};
  let days = [], currentDayId = null, selectedId = null, openId = null;

  async function api(path, method="GET", body) {
    const opt = {method, headers: {"Content-Type": "application/json"}};
    if (body !== undefined) opt.body = JSON.stringify(body);
    const r = await fetch(path, opt);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
    return r.status === 204 ? null : r.json();
  }

  async function load(keepOpen=false) {
    days = await api("/api/days");
    const cur = await (await fetch("/api/todo")).json();
    currentDayId = cur.day.id;
    if (!selectedId || !days.find(d => d.id === selectedId)) selectedId = currentDayId;
    if (!keepOpen) openId = null;
    render();
  }

  function render() {
    document.getElementById("tabs").innerHTML = days.map(d =>
      `<button class="tab ${d.id === currentDayId ? "current" : ""}" onclick="selectDay(${d.id})" title="click: select + push to stream">${esc(d.label)}${d.id === currentDayId ? " &#9679;" : ""}<span class="edit" title="rename day" onclick="renameDay(${d.id}, event)">&#9998;</span></button>`
    ).join("");
    const day = days.find(d => d.id === selectedId);
    const list = document.getElementById("list");
    if (!day || !day.tasks.length) {
      list.innerHTML = `<div class="empty">no tasks yet</div>`;
      return;
    }
    list.innerHTML = day.tasks.map(t => `
      <div class="task ${t.state}">
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
      </div>`).join("");
  }

  function toggleEditor(id) {
    openId = openId === id ? null : id;
    render();
    if (openId === id) document.getElementById(`txt-${id}`).focus();
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

  async function saveTask(id) {
    const text = document.getElementById(`txt-${id}`).value.trim();
    const details = document.getElementById(`det-${id}`).value;
    if (!text) return;
    await api(`/api/tasks/${id}`, "PATCH", {text, details});
    const ok = document.getElementById(`ok-${id}`);
    ok.classList.add("show");
    setTimeout(() => ok.classList.remove("show"), 1200);
    await load(true);
  }

  async function delTask(id) {
    await api(`/api/tasks/${id}`, "DELETE");
    await load(true);
  }

  async function addTask() {
    const el = document.getElementById("newtext");
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

  load();
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
            try:
                if self.path == "/":
                    self._send(200, PAGE.encode())
                elif self.path == "/manage":
                    self._send(200, MANAGE.encode())
                elif self.path == "/api/todo":
                    payload = day_payload(conn, current_day_id(conn))
                    self._json(200, payload)
                elif self.path == "/api/days":
                    self._json(200, manage_payload(conn))
                else:
                    self._json(404, {"error": "not found"})
            finally:
                conn.close()

        def do_POST(self):
            conn = get_db(db_path)
            try:
                body = self._body()
                if self.path == "/api/days":
                    day = create_day(conn, (body.get("label") or "").strip() or None)
                    self._json(201, {"id": day["id"], "label": day["label"]})
                elif self.path == "/api/current-day":
                    day = conn.execute("SELECT id FROM days WHERE id=?", (body.get("day_id"),)).fetchone()
                    if not day:
                        return self._json(404, {"error": "day not found"})
                    conn.execute(
                        "INSERT INTO settings (key,value) VALUES ('current_day_id',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(day["id"]),),
                    )
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
                    unfinished = conn.execute(
                        "SELECT id FROM tasks WHERE day_id=? AND state!='done' ORDER BY position",
                        (day_id,),
                    ).fetchall()
                    base = next_position(conn, nxt["id"])
                    for n, t in enumerate(unfinished):
                        conn.execute(
                            "UPDATE tasks SET day_id=?, position=? WHERE id=?",
                            (nxt["id"], base + n, t["id"]),
                        )
                    conn.commit()
                    self._json(200, {"moved": len(unfinished), "moved_to": nxt["id"]})
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
                if not updates:
                    return self._json(400, {"error": "nothing to update"})
                params.append(task_id)
                conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id=?", params)
                conn.commit()
                self._json(200, {"id": task_id})
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
                    if current_day_id(conn) == day_id:
                        pass  # current_day_id() falls back to first day if missing
                    conn.execute(
                        "INSERT INTO settings (key,value) VALUES ('current_day_id',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(current_day_id(conn)),),
                    )
                    conn.commit()
                    return self._send(204, b"")
                self._json(404, {"error": "not found"})
            finally:
                conn.close()

        def log_message(self, *args):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(Path(__file__).resolve().parent.parent / "todolist.txt"),
                    help="seed file used only when the database is empty")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent / "todo.db"))
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    init_db(args.db, args.file)
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
