"""One-time pairing codes and persistent game-device links."""
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
        db.execute(
            "CREATE TABLE IF NOT EXISTS game_devices "
            "(secret_hash TEXT PRIMARY KEY, discord_id TEXT NOT NULL, created INTEGER NOT NULL)"
        )
        db.commit()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _cleanup(self):
        self.db.execute("DELETE FROM pairing WHERE expires <= ? OR delivered = 1", (int(time.time()),))
        self.db.commit()

    def start(self) -> tuple[str, str]:
        self._cleanup()
        ticket = secrets.token_urlsafe(32)
        digest = self._hash(ticket)
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
        if not isinstance(ticket, str) or not ticket or len(ticket) > 256:
            return {"status": "invalid"}
        digest = self._hash(ticket)
        row = self.db.execute(
            "SELECT code, expires, discord_id, delivered FROM pairing WHERE secret_hash = ?",
            (digest,),
        ).fetchone()
        if not row or row[1] <= int(time.time()) or row[3]:
            return {"status": "invalid"}
        if row[2] is None:
            return {"status": "pending"}
        device_token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute(
                "INSERT INTO game_devices (secret_hash, discord_id, created) VALUES (?, ?, ?)",
                (self._hash(device_token), row[2], int(time.time())),
            )
            self.db.execute("UPDATE pairing SET delivered = 1 WHERE code = ?", (row[0],))
        return {"status": "linked", "discord_id": row[2], "device_token": device_token}

    def verify(self, device_token: str) -> dict:
        if not isinstance(device_token, str) or len(device_token) < 32 or len(device_token) > 256:
            return {"status": "invalid"}
        row = self.db.execute(
            "SELECT discord_id FROM game_devices WHERE secret_hash = ?",
            (self._hash(device_token),),
        ).fetchone()
        return {"status": "linked", "discord_id": row[0]} if row else {"status": "invalid"}

    def revoke(self, device_token: str) -> bool:
        if not isinstance(device_token, str) or len(device_token) < 32 or len(device_token) > 256:
            return False
        with self.db:
            cursor = self.db.execute(
                "DELETE FROM game_devices WHERE secret_hash = ?", (self._hash(device_token),)
            )
        return cursor.rowcount == 1

    def unlink_discord(self, discord_id: int) -> int:
        """Revoke this Discord user's devices and cancel outstanding pairings."""
        user_id = str(discord_id)
        with self.db:
            cursor = self.db.execute(
                "DELETE FROM game_devices WHERE discord_id = ?", (user_id,)
            )
            self.db.execute("DELETE FROM pairing WHERE discord_id = ?", (user_id,))
        return cursor.rowcount
