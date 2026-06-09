"""
RDP Session Tracker - FastAPI Backend
======================================
Receives RDP session events from PowerShell collectors running on
target machines, stores them in SQLite, and serves the dashboard API.

Run:
    pip install fastapi uvicorn aiofiles
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import os
import json
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "rdp_sessions.db")
DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "dashboard")

app = FastAPI(title="RDP Session Tracker", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_uid   TEXT UNIQUE,          -- combo of host+logon_id
            user          TEXT NOT NULL,
            target_ip     TEXT NOT NULL,        -- machine being connected TO
            target_host   TEXT,                 -- hostname of target machine
            source_ip     TEXT,                 -- machine connecting FROM
            logon_id      TEXT,
            event_id      INTEGER,              -- 4624/4778/4779/4634/21/23/24/25
            start_time    TEXT,                 -- ISO8601 UTC
            end_time      TEXT,
            status        TEXT DEFAULT 'active',-- active | disconnected | ended
            created_at    TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events_raw (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            target_ip   TEXT,
            event_id    INTEGER,
            user        TEXT,
            source_ip   TEXT,
            logon_id    TEXT,
            event_time  TEXT,
            raw_json    TEXT,
            received_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ── Pydantic Models ───────────────────────────────────────────────────────────
class RDPEvent(BaseModel):
    """Payload sent by the PowerShell collector on each target machine."""
    target_ip: str
    target_host: Optional[str] = None
    event_id: int               # Windows Event ID
    user: str
    source_ip: Optional[str] = None
    logon_id: Optional[str] = None
    event_time: str             # ISO8601 string from PowerShell
    raw_json: Optional[str] = None

class SessionOut(BaseModel):
    id: int
    session_uid: Optional[str]
    user: str
    target_ip: str
    target_host: Optional[str]
    source_ip: Optional[str]
    logon_id: Optional[str]
    event_id: Optional[int]
    start_time: Optional[str]
    end_time: Optional[str]
    status: str
    created_at: str

# ── Helpers ───────────────────────────────────────────────────────────────────
def uid(target_ip: str, logon_id: str) -> str:
    return f"{target_ip}::{logon_id}"

# Map Windows Event IDs to session lifecycle actions
LOGON_EVENTS      = {4624, 21}        # New session started
DISCONNECT_EVENTS = {4779, 24}        # Session disconnected (still alive)
RECONNECT_EVENTS  = {4778, 25}        # Session reconnected
LOGOFF_EVENTS     = {4634, 4647, 23}  # Session fully ended

def process_event(ev: RDPEvent, conn: sqlite3.Connection):
    """Update sessions table based on the incoming event type."""
    eid = ev.event_id
    lid = ev.logon_id or "unknown"
    session_uid = uid(ev.target_ip, lid)

    if eid in LOGON_EVENTS:
        # Insert new session (ignore if somehow duplicate uid)
        conn.execute("""
            INSERT OR IGNORE INTO sessions
                (session_uid, user, target_ip, target_host, source_ip,
                 logon_id, event_id, start_time, status)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (session_uid, ev.user, ev.target_ip, ev.target_host,
              ev.source_ip, lid, eid, ev.event_time, "active"))

    elif eid in DISCONNECT_EVENTS:
        conn.execute("""
            UPDATE sessions SET status='disconnected', event_id=?
            WHERE session_uid=?
        """, (eid, session_uid))

    elif eid in RECONNECT_EVENTS:
        # On reconnect Windows assigns a NEW logon_id, so we try to match
        # on user+target_ip with disconnected status
        conn.execute("""
            UPDATE sessions SET status='active', event_id=?, logon_id=?,
                session_uid=?
            WHERE user=? AND target_ip=? AND status='disconnected'
        """, (eid, lid, session_uid, ev.user, ev.target_ip))

    elif eid in LOGOFF_EVENTS:
        conn.execute("""
            UPDATE sessions SET status='ended', end_time=?, event_id=?
            WHERE session_uid=?
        """, (ev.event_time, eid, session_uid))
        # Fallback: match on user+target when logon_id unknown
        if lid == "unknown":
            conn.execute("""
                UPDATE sessions SET status='ended', end_time=?, event_id=?
                WHERE user=? AND target_ip=? AND status IN ('active','disconnected')
            """, (ev.event_time, eid, ev.user, ev.target_ip))

# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/api/event", summary="Receive RDP event from PowerShell collector")
def receive_event(ev: RDPEvent):
    conn = get_db()
    try:
        # Store raw event for audit trail
        conn.execute("""
            INSERT INTO events_raw (target_ip, event_id, user, source_ip,
                logon_id, event_time, raw_json)
            VALUES (?,?,?,?,?,?,?)
        """, (ev.target_ip, ev.event_id, ev.user, ev.source_ip,
              ev.logon_id, ev.event_time, ev.raw_json))

        process_event(ev, conn)
        conn.commit()
        return {"status": "ok", "message": f"Event {ev.event_id} for {ev.user} processed"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@app.get("/api/sessions", response_model=List[SessionOut],
         summary="Get all sessions with optional filters")
def get_sessions(
    status: Optional[str] = Query(None, description="active | disconnected | ended"),
    target_ip: Optional[str] = Query(None, description="Filter by target host IP"),
    user: Optional[str] = Query(None, description="Filter by username (partial match)"),
    limit: int = Query(200, le=1000)
):
    conn = get_db()
    query = "SELECT * FROM sessions WHERE 1=1"
    params = []

    if status:
        query += " AND status=?"
        params.append(status)
    if target_ip:
        query += " AND target_ip=?"
        params.append(target_ip)
    if user:
        query += " AND user LIKE ?"
        params.append(f"%{user}%")

    query += " ORDER BY start_time DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/stats", summary="Dashboard summary statistics")
def get_stats():
    conn = get_db()
    active      = conn.execute("SELECT COUNT(*) FROM sessions WHERE status='active'").fetchone()[0]
    disconnected= conn.execute("SELECT COUNT(*) FROM sessions WHERE status='disconnected'").fetchone()[0]
    ended_today = conn.execute("""
        SELECT COUNT(*) FROM sessions
        WHERE status='ended' AND date(end_time) = date('now')
    """).fetchone()[0]
    hosts       = conn.execute("SELECT COUNT(DISTINCT target_ip) FROM sessions").fetchone()[0]
    users_active= conn.execute("""
        SELECT COUNT(DISTINCT user) FROM sessions WHERE status='active'
    """).fetchone()[0]

    top_hosts = conn.execute("""
        SELECT target_ip, COUNT(*) as cnt
        FROM sessions GROUP BY target_ip ORDER BY cnt DESC LIMIT 5
    """).fetchall()

    top_users = conn.execute("""
        SELECT user, COUNT(*) as cnt
        FROM sessions GROUP BY user ORDER BY cnt DESC LIMIT 5
    """).fetchall()

    conn.close()
    return {
        "active": active,
        "disconnected": disconnected,
        "ended_today": ended_today,
        "total_hosts": hosts,
        "active_users": users_active,
        "top_hosts": [dict(r) for r in top_hosts],
        "top_users": [dict(r) for r in top_users],
    }


@app.get("/api/hosts", summary="List all unique target host IPs")
def get_hosts():
    conn = get_db()
    rows = conn.execute("""
        SELECT DISTINCT target_ip, target_host FROM sessions ORDER BY target_ip
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.delete("/api/sessions/{session_id}", summary="Delete a session record")
def delete_session(session_id: int):
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


@app.get("/api/events/raw", summary="Raw event audit log")
def get_raw_events(limit: int = Query(100, le=500)):
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM events_raw ORDER BY received_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Serve dashboard
if os.path.isdir(DASHBOARD_DIR):
    app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def serve_dashboard():
        return FileResponse(os.path.join(DASHBOARD_DIR, "index.html"))


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}
