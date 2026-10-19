"""
AI-Relay Database Layer
SQLite storage for sessions, context snapshots, provider quotas, and handoff logs.
"""

import sqlite3
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict


@dataclass
class Session:
    id: str
    project_path: str
    created_at: str
    last_provider: str
    total_tokens_used: int
    status: str = "active"  # active, completed, abandoned


@dataclass
class ContextSnapshot:
    id: str
    session_id: str
    provider: str
    captured_at: str
    full_transcript: str  # JSON
    compressed_summary: str
    file_changes: str  # JSON
    task_state: str  # JSON
    compression_level: str
    token_count: int


@dataclass
class ProviderQuota:
    provider: str
    daily_limit: Optional[int]
    used_today: int
    reset_at: str
    priority: int
    is_available: bool = True


@dataclass
class HandoffLog:
    id: str
    session_id: str
    from_provider: str
    to_provider: str
    handoff_at: str
    context_tokens: int
    reason: str  # quota, manual, error, scheduled


class RelayDatabase:
    """SQLite database for AI-Relay session management."""

    SCHEMA_VERSION = 1

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            relay_dir = Path.home() / ".ai-relay"
            relay_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(relay_dir / "relay.db")

        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initialize database schema."""
        conn = self._get_conn()
        try:
            conn.executescript("""
                -- Schema version tracking
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY
                );

                -- Sessions table
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    project_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_provider TEXT,
                    total_tokens_used INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'active'
                );

                -- Context snapshots
                CREATE TABLE IF NOT EXISTS context_snapshots (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    full_transcript TEXT,
                    compressed_summary TEXT,
                    file_changes TEXT,
                    task_state TEXT,
                    compression_level TEXT,
                    token_count INTEGER DEFAULT 0,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );

                -- Provider quotas
                CREATE TABLE IF NOT EXISTS provider_quotas (
                    provider TEXT PRIMARY KEY,
                    daily_limit INTEGER,
                    used_today INTEGER DEFAULT 0,
                    reset_at TEXT,
                    priority INTEGER DEFAULT 10,
                    is_available INTEGER DEFAULT 1
                );

                -- Handoff log
                CREATE TABLE IF NOT EXISTS handoff_log (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    from_provider TEXT NOT NULL,
                    to_provider TEXT NOT NULL,
                    handoff_at TEXT NOT NULL,
                    context_tokens INTEGER DEFAULT 0,
                    reason TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );

                -- Indexes
                CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
                CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at);
                CREATE INDEX IF NOT EXISTS idx_snapshots_session ON context_snapshots(session_id);
                CREATE INDEX IF NOT EXISTS idx_handoff_session ON handoff_log(session_id);
            """)

            # Check/update schema version
            cur = conn.execute("SELECT version FROM schema_version LIMIT 1")
            row = cur.fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)",
                           (self.SCHEMA_VERSION,))

            conn.commit()
        finally:
            conn.close()

    # =========================================================================
    # Session CRUD
    # =========================================================================

    def create_session(self, project_path: str, provider: str) -> Session:
        """Create a new relay session."""
        session = Session(
            id=f"rel_{uuid.uuid4().hex[:12]}",
            project_path=project_path,
            created_at=datetime.now(timezone.utc).isoformat(),
            last_provider=provider,
            total_tokens_used=0,
            status="active"
        )

        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO sessions (id, project_path, created_at, last_provider,
                                      total_tokens_used, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (session.id, session.project_path, session.created_at,
                  session.last_provider, session.total_tokens_used, session.status))
            conn.commit()
        finally:
            conn.close()

        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        """Get a session by ID."""
        conn = self._get_conn()
        try:
            cur = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = cur.fetchone()
            if row:
                return Session(**dict(row))
            return None
        finally:
            conn.close()

    def get_active_session(self, project_path: Optional[str] = None) -> Optional[Session]:
        """Get the most recent active session, optionally filtered by project."""
        conn = self._get_conn()
        try:
            if project_path:
                cur = conn.execute("""
                    SELECT * FROM sessions
                    WHERE status = 'active' AND project_path = ?
                    ORDER BY created_at DESC LIMIT 1
                """, (project_path,))
            else:
                cur = conn.execute("""
                    SELECT * FROM sessions
                    WHERE status = 'active'
                    ORDER BY created_at DESC LIMIT 1
                """)
            row = cur.fetchone()
            if row:
                return Session(**dict(row))
            return None
        finally:
            conn.close()

    def list_sessions(self, limit: int = 10, include_completed: bool = False) -> List[Session]:
        """List recent sessions."""
        conn = self._get_conn()
        try:
            if include_completed:
                cur = conn.execute("""
                    SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?
                """, (limit,))
            else:
                cur = conn.execute("""
                    SELECT * FROM sessions WHERE status = 'active'
                    ORDER BY created_at DESC LIMIT ?
                """, (limit,))
            return [Session(**dict(row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def update_session(self, session_id: str, **kwargs) -> bool:
        """Update session fields."""
        allowed = {'last_provider', 'total_tokens_used', 'status'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [session_id]

        conn = self._get_conn()
        try:
            conn.execute(f"UPDATE sessions SET {set_clause} WHERE id = ?", values)
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    # =========================================================================
    # Context Snapshot CRUD
    # =========================================================================

    def save_snapshot(self, snapshot: ContextSnapshot) -> str:
        """Save a context snapshot."""
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO context_snapshots
                (id, session_id, provider, captured_at, full_transcript,
                 compressed_summary, file_changes, task_state, compression_level, token_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (snapshot.id, snapshot.session_id, snapshot.provider,
                  snapshot.captured_at, snapshot.full_transcript,
                  snapshot.compressed_summary, snapshot.file_changes,
                  snapshot.task_state, snapshot.compression_level, snapshot.token_count))
            conn.commit()
        finally:
            conn.close()
        return snapshot.id

    def get_latest_snapshot(self, session_id: str) -> Optional[ContextSnapshot]:
        """Get the most recent snapshot for a session."""
        conn = self._get_conn()
        try:
            cur = conn.execute("""
                SELECT * FROM context_snapshots
                WHERE session_id = ? ORDER BY captured_at DESC LIMIT 1
            """, (session_id,))
            row = cur.fetchone()
            if row:
                return ContextSnapshot(**dict(row))
            return None
        finally:
            conn.close()

    def list_snapshots(self, session_id: str) -> List[ContextSnapshot]:
        """List all snapshots for a session."""
        conn = self._get_conn()
        try:
            cur = conn.execute("""
                SELECT * FROM context_snapshots
                WHERE session_id = ? ORDER BY captured_at DESC
            """, (session_id,))
            return [ContextSnapshot(**dict(row)) for row in cur.fetchall()]
        finally:
            conn.close()

    # =========================================================================
    # Provider Quota CRUD
    # =========================================================================

    def get_quota(self, provider: str) -> Optional[ProviderQuota]:
        """Get quota info for a provider."""
        conn = self._get_conn()
        try:
            cur = conn.execute("SELECT * FROM provider_quotas WHERE provider = ?",
                             (provider,))
            row = cur.fetchone()
            if row:
                data = dict(row)
                data['is_available'] = bool(data['is_available'])
                return ProviderQuota(**data)
            return None
        finally:
            conn.close()

    def set_quota(self, quota: ProviderQuota):
        """Set or update quota for a provider."""
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO provider_quotas
                (provider, daily_limit, used_today, reset_at, priority, is_available)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (quota.provider, quota.daily_limit, quota.used_today,
                  quota.reset_at, quota.priority, int(quota.is_available)))
            conn.commit()
        finally:
            conn.close()

    def increment_usage(self, provider: str, tokens: int = 1) -> int:
        """Increment usage counter for a provider. Returns new total."""
        conn = self._get_conn()
        try:
            # Check if reset needed
            cur = conn.execute("SELECT reset_at, used_today FROM provider_quotas WHERE provider = ?",
                             (provider,))
            row = cur.fetchone()
            if row:
                reset_at = datetime.fromisoformat(row['reset_at'])
                if datetime.now(timezone.utc) >= reset_at:
                    # Reset counter
                    new_reset = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ).isoformat()
                    conn.execute("""
                        UPDATE provider_quotas
                        SET used_today = ?, reset_at = ? WHERE provider = ?
                    """, (tokens, new_reset, provider))
                    conn.commit()
                    return tokens
                else:
                    # Increment
                    new_total = row['used_today'] + tokens
                    conn.execute("""
                        UPDATE provider_quotas SET used_today = ? WHERE provider = ?
                    """, (new_total, provider))
                    conn.commit()
                    return new_total
            return 0
        finally:
            conn.close()

    def list_quotas(self) -> List[ProviderQuota]:
        """List all provider quotas ordered by priority."""
        conn = self._get_conn()
        try:
            cur = conn.execute("""
                SELECT * FROM provider_quotas ORDER BY priority ASC
            """)
            quotas = []
            for row in cur.fetchall():
                data = dict(row)
                data['is_available'] = bool(data['is_available'])
                quotas.append(ProviderQuota(**data))
            return quotas
        finally:
            conn.close()

    def get_best_available_provider(self) -> Optional[str]:
        """Get the best available provider based on priority and quota."""
        quotas = self.list_quotas()
        for quota in quotas:
            if not quota.is_available:
                continue
            if quota.daily_limit is None:
                return quota.provider
            if quota.used_today < quota.daily_limit:
                return quota.provider
        return None

    # =========================================================================
    # Handoff Log CRUD
    # =========================================================================

    def log_handoff(self, session_id: str, from_provider: str, to_provider: str,
                    context_tokens: int, reason: str) -> str:
        """Log a handoff event."""
        log_id = f"hoff_{uuid.uuid4().hex[:12]}"
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO handoff_log
                (id, session_id, from_provider, to_provider, handoff_at, context_tokens, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (log_id, session_id, from_provider, to_provider,
                  datetime.now(timezone.utc).isoformat(), context_tokens, reason))
            conn.commit()
        finally:
            conn.close()
        return log_id

    def get_handoff_history(self, session_id: str) -> List[HandoffLog]:
        """Get handoff history for a session."""
        conn = self._get_conn()
        try:
            cur = conn.execute("""
                SELECT * FROM handoff_log WHERE session_id = ? ORDER BY handoff_at ASC
            """, (session_id,))
            return [HandoffLog(**dict(row)) for row in cur.fetchall()]
        finally:
            conn.close()

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        conn = self._get_conn()
        try:
            stats = {}
            stats['total_sessions'] = conn.execute(
                "SELECT COUNT(*) FROM sessions").fetchone()[0]
            stats['active_sessions'] = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE status = 'active'").fetchone()[0]
            stats['total_snapshots'] = conn.execute(
                "SELECT COUNT(*) FROM context_snapshots").fetchone()[0]
            stats['total_handoffs'] = conn.execute(
                "SELECT COUNT(*) FROM handoff_log").fetchone()[0]
            stats['total_tokens'] = conn.execute(
                "SELECT COALESCE(SUM(total_tokens_used), 0) FROM sessions").fetchone()[0]
            return stats
        finally:
            conn.close()


# Convenience function for quick access
def get_db(db_path: Optional[str] = None) -> RelayDatabase:
    """Get database instance."""
    return RelayDatabase(db_path)
