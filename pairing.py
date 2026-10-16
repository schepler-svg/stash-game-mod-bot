"""One-time pairing codes. No game or platform credentials are stored."""
import hashlib
import re
import secrets
import sqlite3
import time

CODE = re.compile(r"^[0-9]{8}$")
TTL = 600


class Pairing:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.execute(
            "CREATE TABLE IF NOT EXISTS pairing "
            "(code TEXT PRIMARY KEY, secret_hash TEXT UNIQUE NOT NULL, "
            "expires INTEGER NOT NULL, discord_id TEXT, delivered INTEGER NOT NULL DEFAULT 0)"
        )
        db.commit()

    def _cleanup(self):
        self.db.execute("DELETE FROM pairing WHERE expires <= ? OR delivered = 1", (int(time.time()),))
        self.db.commit()

    def start(self) -> tuple[str, str]:
        self._cleanup()
        ticket = secrets.token_urlsafe(32)
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        for _ in range(20):
            code = f"{secrets.randbelow(100_000_000):08d}"
            try:
                self.db.execute(
                    "INSERT INTO pairing (code, secret_hash, expires) VALUES (?, ?, ?)",
                    (code, digest, int(time.time()) + TTL),
                )
                self.db.commit()
                return code, ticket
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("Could not allocate a pairing code")

    def link(self, code: str, discord_id: int) -> bool:
        if not CODE.fullmatch(code):
            return False
        cursor = self.db.execute(
            "UPDATE pairing SET discord_id = ? "
            "WHERE code = ? AND expires > ? AND discord_id IS NULL AND delivered = 0",
            (str(discord_id), code, int(time.time())),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def poll(self, ticket: str) -> dict:
        if len(ticket) > 256:
            return {"status": "invalid"}
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        row = self.db.execute(
            "SELECT code, expires, discord_id, delivered FROM pairing WHERE secret_hash = ?",
            (digest,),
        ).fetchone()
        if not row or row[1] <= int(time.time()) or row[3]:
            return {"status": "invalid"}
        if row[2] is None:
            return {"status": "pending"}
        self.db.execute("UPDATE pairing SET delivered = 1 WHERE code = ?", (row[0],))
        self.db.commit()
        return {"status": "linked", "discord_id": row[2]}
