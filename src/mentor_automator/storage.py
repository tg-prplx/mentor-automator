import asyncio
import hashlib
import json
import secrets
import time
from pathlib import Path

import aiosqlite

from .models import Quiz, State


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS quizzes(id TEXT PRIMARY KEY, data TEXT NOT NULL, revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS notifications(
          id INTEGER PRIMARY KEY, quiz_id TEXT, revision INTEGER, kind TEXT, data TEXT,
          status TEXT NOT NULL DEFAULT 'pending', message_id INTEGER,
          UNIQUE(quiz_id, revision, kind));
        CREATE TABLE IF NOT EXISTS runs(
          id TEXT PRIMARY KEY, quiz_id TEXT NOT NULL, attempt_id TEXT,
          url TEXT, state TEXT NOT NULL, token_hash TEXT, expires REAL, message_id INTEGER,
          created REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt ON runs(attempt_id)
          WHERE state NOT IN ('COMPLETED','STOPPED','PLATFORM_CLOSED');
        CREATE TABLE IF NOT EXISTS progress(
          run_id TEXT, question_id TEXT, fingerprint TEXT, data TEXT, filled INTEGER,
          PRIMARY KEY(run_id, question_id));
        CREATE TABLE IF NOT EXISTS telegram_actions(id TEXT PRIMARY KEY, created REAL NOT NULL);
        """)
        columns = {row["name"] for row in await self.all("PRAGMA table_info(runs)")}
        for column in ("stage", "error_code", "error_context"):
            if column not in columns:
                await self.db.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
        await self.db.commit()

    async def close(self):
        await self.db.close()

    async def claim_telegram_action(self, action_id):
        async with self.lock:
            cursor = await self.db.execute(
                "INSERT OR IGNORE INTO telegram_actions(id,created) VALUES(?,?)", (action_id, time.time())
            )
            await self.db.commit()
            return cursor.rowcount == 1

    async def one(self, sql, args=()):
        async with self.db.execute(sql, args) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def all(self, sql, args=()):
        async with self.db.execute(sql, args) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def recover(self):
        # No final action is retried. Preserve waiting state, invalidate every old approval.
        async with self.lock:
            await self.db.execute("UPDATE runs SET token_hash=NULL, expires=NULL")
            await self.db.execute(
                "UPDATE runs SET state=? WHERE state IN (?,?,?,?,?)",
                (
                    State.RECOVERY_REQUIRED,
                    State.RUNNING,
                    State.STARTING,
                    State.PAUSED,
                    State.SUBMITTING,
                    State.ERROR,
                ),
            )
            # Delivery outcome may be ambiguous. Never resend automatically and create duplicates.
            await self.db.execute("UPDATE notifications SET status='uncertain' WHERE status='sending'")
            await self.db.commit()

    async def observe(self, quiz: Quiz):
        async with self.lock:
            row = await self.one("SELECT * FROM quizzes WHERE id=?", (quiz.id,))
            data = quiz.model_dump_json()
            if row and row["data"] == data:
                return
            revision = row["revision"] + 1 if row else 1
            await self.db.execute(
                "INSERT INTO quizzes VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "data=excluded.data,revision=excluded.revision",
                (quiz.id, data, revision),
            )
            await self.db.execute(
                "INSERT OR IGNORE INTO notifications(quiz_id,revision,kind,data) VALUES(?,?,?,?)",
                (quiz.id, revision, "new" if row is None else "changed", data),
            )
            await self.db.commit()

    async def quizzes(self):
        return [
            Quiz.model_validate_json(r["data"])
            for r in await self.all("SELECT data FROM quizzes ORDER BY id")
        ]

    async def quiz(self, qid):
        row = await self.one("SELECT data FROM quizzes WHERE id=?", (qid,))
        return Quiz.model_validate_json(row["data"]) if row else None

    async def notification_status(self, nid, status, message_id=None):
        async with self.lock:
            await self.db.execute(
                "UPDATE notifications SET status=?,message_id=? WHERE id=?", (status, message_id, nid)
            )
            await self.db.commit()

    async def create_run(self, quiz_id):
        async with self.lock:
            active = await self.one(
                "SELECT id FROM runs WHERE state NOT IN (?,?,?) LIMIT 1",
                (State.COMPLETED, State.STOPPED, State.PLATFORM_CLOSED),
            )
            if active:
                raise RuntimeError("ACTIVE_RUN_EXISTS")
            rid = secrets.token_hex(6)
            await self.db.execute(
                "INSERT INTO runs(id,quiz_id,state,created) VALUES(?,?,?,?)",
                (rid, quiz_id, State.STARTING, time.time()),
            )
            await self.db.commit()
            return rid

    async def run(self, rid):
        return await self.one("SELECT * FROM runs WHERE id=?", (rid,))

    async def latest_run(self):
        return await self.one("SELECT * FROM runs ORDER BY created DESC LIMIT 1")

    async def status_run(self):
        active = await self.one(
            "SELECT * FROM runs WHERE state NOT IN (?,?,?) ORDER BY created DESC LIMIT 1",
            (State.COMPLETED, State.STOPPED, State.PLATFORM_CLOSED),
        )
        return (
            active
            or await self.one("SELECT * FROM runs WHERE attempt_id IS NOT NULL ORDER BY created DESC LIMIT 1")
            or await self.latest_run()
        )

    async def attempt_run(self, attempt_id):
        return await self.one(
            "SELECT * FROM runs WHERE attempt_id=? ORDER BY created DESC LIMIT 1", (attempt_id,)
        )

    async def update_run(self, rid, **fields):
        if not fields or not set(fields) <= {
            "attempt_id",
            "url",
            "state",
            "message_id",
            "stage",
            "error_code",
            "error_context",
        }:
            raise ValueError("Invalid fields")
        async with self.lock:
            assignments = ",".join(f"{k}=?" for k in fields)
            await self.db.execute(f"UPDATE runs SET {assignments} WHERE id=?", (*fields.values(), rid))
            await self.db.commit()

    async def revoke(self, rid):
        async with self.lock:
            await self.db.execute("UPDATE runs SET token_hash=NULL,expires=NULL WHERE id=?", (rid,))
            await self.db.commit()

    async def issue_confirmation(self, rid, ttl):
        token = secrets.token_hex(8)
        async with self.lock:
            row = await self.run(rid)
            if not row or not row["attempt_id"] or row["state"] != State.AWAITING_FINAL_CONFIRMATION:
                raise RuntimeError("NOT_AWAITING")
            await self.db.execute(
                "UPDATE runs SET token_hash=?,expires=? WHERE id=?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time() + ttl, rid),
            )
            await self.db.commit()
        return token

    async def consume_confirmation(self, rid, token, attempt_id):
        async with self.lock:
            cursor = await self.db.execute(
                "UPDATE runs SET state=?,token_hash=NULL,expires=NULL "
                "WHERE id=? AND attempt_id=? AND state=? AND token_hash=? AND expires>?",
                (
                    State.SUBMITTING,
                    rid,
                    attempt_id,
                    State.AWAITING_FINAL_CONFIRMATION,
                    hashlib.sha256(token.encode()).hexdigest(),
                    time.time(),
                ),
            )
            await self.db.commit()
            return cursor.rowcount == 1

    async def progress(self, rid, qid, fingerprint, data, filled):
        async with self.lock:
            await self.db.execute(
                "INSERT INTO progress VALUES(?,?,?,?,?) ON CONFLICT(run_id,question_id) "
                "DO UPDATE SET fingerprint=excluded.fingerprint,data=excluded.data,filled=excluded.filled",
                (rid, qid, fingerprint, json.dumps(data, ensure_ascii=False), int(filled)),
            )
            await self.db.commit()

    async def counts(self, rid):
        row = await self.one(
            "SELECT COUNT(*) total, COALESCE(SUM(filled),0) filled FROM progress WHERE run_id=?", (rid,)
        )
        return row["filled"], row["total"] - row["filled"]
