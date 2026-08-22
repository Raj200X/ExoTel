"""
PostgreSQL database layer for Saarthi.

Provides persistent storage for users, calls, and conversation messages
using asyncpg.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date
from typing import Any

import asyncpg

from saarthi.models.core import (
    Call,
    CallAnalysis,
    CallListItem,
    ConversationMessage,
    DashboardStats,
    User,
    UserProfile,
)
from saarthi.models.enums import CallStatus, CallTopic, MessageRole, RiskLevel

logger = logging.getLogger(__name__)


class Database:
    """Async PostgreSQL database for Saarthi."""

    def __init__(self, db_url: str):
        self._db_url = db_url
        self._pool: asyncpg.Pool | None = None

    async def init(self):
        """Initialize the database connection pool and create tables."""
        self._pool = await asyncpg.create_pool(self._db_url, min_size=1, max_size=10)
        await self._create_tables()
        logger.info("PostgreSQL database initialized")

    async def close(self):
        """Close the database connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def _create_tables(self):
        """Create all tables if they don't exist."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    phone_number TEXT UNIQUE NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    total_calls INTEGER DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone_number);

                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY,
                    exotel_call_sid TEXT UNIQUE NOT NULL,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    status TEXT NOT NULL DEFAULT 'incoming',
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    duration_seconds DOUBLE PRECISION DEFAULT 0.0,
                    transcript TEXT DEFAULT '',
                    recording_url TEXT,
                    summary TEXT DEFAULT '',
                    topic TEXT DEFAULT 'other',
                    risk_level TEXT DEFAULT 'low',
                    action_items TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_calls_exotel_sid ON calls(exotel_call_sid);
                CREATE INDEX IF NOT EXISTS idx_calls_user_id ON calls(user_id);
                CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
                CREATE INDEX IF NOT EXISTS idx_calls_start_time ON calls(start_time);

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    call_id TEXT NOT NULL REFERENCES calls(id),
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_messages_call_id ON messages(call_id);
            """)

    # -------------------------------------------------------------------
    # User operations
    # -------------------------------------------------------------------

    async def create_or_get_user(self, phone_number: str) -> User:
        """Find an existing user by phone number, or create a new one."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE phone_number = $1", phone_number
            )

            if row:
                # Update last_seen
                now = datetime.utcnow().isoformat()
                await conn.execute(
                    "UPDATE users SET last_seen = $1 WHERE id = $2", now, row["id"]
                )
                return User(
                    id=row["id"],
                    phone_number=row["phone_number"],
                    first_seen=datetime.fromisoformat(row["first_seen"]),
                    last_seen=datetime.utcnow(),
                    total_calls=row["total_calls"],
                )

            # Create new user
            user = User(phone_number=phone_number)
            await conn.execute(
                """INSERT INTO users (id, phone_number, first_seen, last_seen, total_calls)
                   VALUES ($1, $2, $3, $4, $5)""",
                user.id,
                user.phone_number,
                user.first_seen.isoformat(),
                user.last_seen.isoformat(),
                0,
            )
            logger.info("Created new user: %s (phone: %s)", user.id, user.masked_phone)
            return user

    async def increment_user_calls(self, user_id: str):
        """Increment the total_calls counter for a user."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET total_calls = total_calls + 1 WHERE id = $1", user_id
            )

    async def get_user_by_phone(self, phone_number: str) -> User | None:
        """Get a user by phone number."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE phone_number = $1", phone_number
            )
            if not row:
                return None
            return User(
                id=row["id"],
                phone_number=row["phone_number"],
                first_seen=datetime.fromisoformat(row["first_seen"]),
                last_seen=datetime.fromisoformat(row["last_seen"]),
                total_calls=row["total_calls"],
            )

    async def get_user_by_id(self, user_id: str) -> User | None:
        """Get a user by ID."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            if not row:
                return None
            return User(
                id=row["id"],
                phone_number=row["phone_number"],
                first_seen=datetime.fromisoformat(row["first_seen"]),
                last_seen=datetime.fromisoformat(row["last_seen"]),
                total_calls=row["total_calls"],
            )

    # -------------------------------------------------------------------
    # Call operations
    # -------------------------------------------------------------------

    async def create_call(self, call: Call) -> Call:
        """Create a new call record. Skips if exotel_call_sid already exists."""
        async with self._pool.acquire() as conn:
            # Check for duplicate
            existing = await conn.fetchrow(
                "SELECT id FROM calls WHERE exotel_call_sid = $1", call.exotel_call_sid
            )
            if existing:
                logger.debug("Call with exotel_call_sid %s already exists, skipping", call.exotel_call_sid)
                return await self.get_call(existing["id"])

            await conn.execute(
                """INSERT INTO calls
                   (id, exotel_call_sid, user_id, status, start_time, end_time,
                    duration_seconds, transcript, recording_url, summary, topic,
                    risk_level, action_items, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)""",
                call.id,
                call.exotel_call_sid,
                call.user_id,
                call.status.value,
                call.start_time.isoformat(),
                call.end_time.isoformat() if call.end_time else None,
                call.duration_seconds,
                call.transcript,
                call.recording_url,
                call.summary,
                call.topic.value,
                call.risk_level.value,
                json.dumps(call.action_items),
                call.created_at.isoformat(),
            )
            logger.info("Created call: %s ( exotel: %s)", call.id, call.exotel_call_sid)
            return call

    async def update_call(
        self,
        exotel_call_sid: str,
        **kwargs: Any,
    ) -> Call | None:
        """Update a call record by its Exotel CallSid."""
        set_parts = []
        values = []
        param_idx = 1
        for key, value in kwargs.items():
            if key == "action_items" and isinstance(value, list):
                value = json.dumps(value)
            elif key == "end_time" and isinstance(value, datetime):
                value = value.isoformat()
            elif key == "status" and isinstance(value, CallStatus):
                value = value.value
            elif key == "topic" and isinstance(value, CallTopic):
                value = value.value
            elif key == "risk_level" and isinstance(value, RiskLevel):
                value = value.value
            set_parts.append(f"{key} = ${param_idx}")
            values.append(value)
            param_idx += 1

        if not set_parts:
            return None

        values.append(exotel_call_sid)
        query = f"UPDATE calls SET {', '.join(set_parts)} WHERE exotel_call_sid = ${param_idx}"
        
        async with self._pool.acquire() as conn:
            await conn.execute(query, *values)
            return await self.get_call_by_exotel_sid(exotel_call_sid)

    async def get_call(self, call_id: str) -> Call | None:
        """Get a call by internal ID."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM calls WHERE id = $1", call_id)
            if not row:
                return None
            return self._row_to_call(row)

    async def get_call_by_exotel_sid(self, exotel_call_sid: str) -> Call | None:
        """Get a call by its Exotel CallSid."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM calls WHERE exotel_call_sid = $1", exotel_call_sid
            )
            if not row:
                return None
            return self._row_to_call(row)

    async def list_calls(
        self,
        search: str | None = None,
        topic: str | None = None,
        risk_level: str | None = None,
        status: str | None = None,
        date_filter: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CallListItem]:
        """List calls with optional filters."""
        query = """
            SELECT c.*, u.phone_number
            FROM calls c
            JOIN users u ON c.user_id = u.id
            WHERE 1=1
        """
        params: list[Any] = []
        param_idx = 1

        if search:
            query += f" AND u.phone_number LIKE ${param_idx}"
            params.append(f"%{search}%")
            param_idx += 1

        if topic and topic != "all":
            query += f" AND c.topic = ${param_idx}"
            params.append(topic)
            param_idx += 1

        if risk_level and risk_level != "all":
            query += f" AND c.risk_level = ${param_idx}"
            params.append(risk_level)
            param_idx += 1

        if status and status != "all":
            query += f" AND c.status = ${param_idx}"
            params.append(status)
            param_idx += 1

        if date_filter:
            today = date.today().isoformat()
            if date_filter == "today":
                query += f" AND c.start_time >= ${param_idx}"
                params.append(today)
                param_idx += 1
            elif date_filter == "week":
                from datetime import timedelta
                week_ago = (date.today() - timedelta(days=7)).isoformat()
                query += f" AND c.start_time >= ${param_idx}"
                params.append(week_ago)
                param_idx += 1
            elif date_filter == "month":
                from datetime import timedelta
                month_ago = (date.today() - timedelta(days=30)).isoformat()
                query += f" AND c.start_time >= ${param_idx}"
                params.append(month_ago)
                param_idx += 1

        query += f" ORDER BY c.start_time DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
        params.extend([limit, offset])

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            
            items = []
            for row in rows:
                call = self._row_to_call(row)
                user = User(phone_number=row["phone_number"])
                items.append(
                    CallListItem(
                        id=call.id,
                        exotel_call_sid=call.exotel_call_sid,
                        caller_phone=user.masked_phone,
                        topic=call.topic.value,
                        duration=call.duration_display,
                        risk_level=call.risk_level.value,
                        status=call.status.value,
                        start_time=call.start_time.isoformat(),
                        user_id=call.user_id,
                    )
                )
            return items

    async def get_active_calls(self) -> list[CallListItem]:
        """Get currently active calls."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT c.*, u.phone_number
                FROM calls c
                JOIN users u ON c.user_id = u.id
                WHERE c.status IN ('incoming', 'active')
                ORDER BY c.start_time DESC
            """)
            items = []
            for row in rows:
                call = self._row_to_call(row)
                user = User(phone_number=row["phone_number"])
                items.append(
                    CallListItem(
                        id=call.id,
                        exotel_call_sid=call.exotel_call_sid,
                        caller_phone=user.masked_phone,
                        topic=call.topic.value,
                        duration=call.duration_display,
                        risk_level=call.risk_level.value,
                        status=call.status.value,
                        start_time=call.start_time.isoformat(),
                        user_id=call.user_id,
                    )
                )
            return items

    async def get_user_calls(self, user_id: str) -> list[CallListItem]:
        """Get all calls for a specific user."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT c.*, u.phone_number
                FROM calls c
                JOIN users u ON c.user_id = u.id
                WHERE c.user_id = $1
                ORDER BY c.start_time DESC
            """, user_id)
            items = []
            for row in rows:
                call = self._row_to_call(row)
                user = User(phone_number=row["phone_number"])
                items.append(
                    CallListItem(
                        id=call.id,
                        exotel_call_sid=call.exotel_call_sid,
                        caller_phone=user.masked_phone,
                        topic=call.topic.value,
                        duration=call.duration_display,
                        risk_level=call.risk_level.value,
                        status=call.status.value,
                        start_time=call.start_time.isoformat(),
                        user_id=call.user_id,
                    )
                )
            return items

    # -------------------------------------------------------------------
    # Message operations
    # -------------------------------------------------------------------

    async def create_messages(self, messages: list[ConversationMessage]):
        """Bulk insert conversation messages."""
        if not messages:
            return
        
        # In asyncpg we use executemany with a list of tuples
        data = [
            (
                msg.id,
                msg.call_id,
                msg.role.value,
                msg.content,
                msg.timestamp.isoformat(),
            )
            for msg in messages
        ]
        
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """INSERT INTO messages (id, call_id, role, content, timestamp)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (id) DO NOTHING""",
                data
            )

    async def get_messages_for_call(self, call_id: str) -> list[ConversationMessage]:
        """Get all messages for a call, ordered by timestamp."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM messages WHERE call_id = $1 ORDER BY timestamp ASC",
                call_id,
            )
            return [
                ConversationMessage(
                    id=row["id"],
                    call_id=row["call_id"],
                    role=MessageRole(row["role"]),
                    content=row["content"],
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                )
                for row in rows
            ]

    # -------------------------------------------------------------------
    # Dashboard statistics
    # -------------------------------------------------------------------

    async def get_dashboard_stats(self) -> DashboardStats:
        """Get aggregate statistics for the dashboard."""
        today = date.today().isoformat()
        
        async with self._pool.acquire() as conn:
            # Total calls
            total = await conn.fetchval("SELECT COUNT(*) FROM calls")

            # Today's calls
            today_calls = await conn.fetchval(
                "SELECT COUNT(*) FROM calls WHERE start_time >= $1", today
            )

            # Unique callers
            unique_callers = await conn.fetchval("SELECT COUNT(*) FROM users")

            # Active calls
            active = await conn.fetchval(
                "SELECT COUNT(*) FROM calls WHERE status IN ('incoming', 'active')"
            )

            # High-risk calls
            high_risk = await conn.fetchval(
                "SELECT COUNT(*) FROM calls WHERE risk_level = 'high'"
            )

            return DashboardStats(
                total_calls=total,
                today_calls=today_calls,
                unique_callers=unique_callers,
                active_calls=active,
                high_risk_calls=high_risk,
            )

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _row_to_call(row) -> Call:
        """Convert a database row to a Call model."""
        action_items_raw = row["action_items"]
        if isinstance(action_items_raw, str):
            try:
                action_items = json.loads(action_items_raw)
            except (json.JSONDecodeError, TypeError):
                action_items = []
        else:
            action_items = action_items_raw or []

        return Call(
            id=row["id"],
            exotel_call_sid=row["exotel_call_sid"],
            user_id=row["user_id"],
            status=CallStatus(row["status"]),
            start_time=datetime.fromisoformat(row["start_time"]),
            end_time=datetime.fromisoformat(row["end_time"]) if row["end_time"] else None,
            duration_seconds=row["duration_seconds"] or 0.0,
            transcript=row["transcript"] or "",
            recording_url=row["recording_url"],
            summary=row["summary"] or "",
            topic=CallTopic(row["topic"]) if row["topic"] else CallTopic.OTHER,
            risk_level=RiskLevel(row["risk_level"]) if row["risk_level"] else RiskLevel.LOW,
            action_items=action_items,
            created_at=datetime.fromisoformat(row["created_at"]),
        )
