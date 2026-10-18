"""Encrypted, per-Discord-user game-session slots and device selection state."""
import json
import os
import sqlite3
import time

from cryptography.fernet import Fernet, InvalidToken


class AuthStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        key = os.getenv("AUTH_ENCRYPTION_KEY", "").strip()
        self.cipher = Fernet(key.encode("ascii")) if key else None
        db.execute(
            "CREATE TABLE IF NOT EXISTS auth_slots "
            "(discord_id TEXT NOT NULL, slot INTEGER NOT NULL, credentials BLOB NOT NULL, "
            "created INTEGER NOT NULL, PRIMARY KEY(discord_id, slot))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS auth_selection "
            "(discord_id TEXT PRIMARY KEY, slot INTEGER NOT NULL, revision INTEGER NOT NULL)"
        )
        db.commit()

    def require_key(self):
        if self.cipher is None:
            raise RuntimeError("AUTH_ENCRYPTION_KEY is not configured on the bot host")

    def add(self, discord_id: int, bearer: str, refresh: str) -> int:
        self.require_key()
        bearer, refresh = bearer.strip(), refresh.strip()
        if not (16 <= len(bearer) <= 4000 and 16 <= len(refresh) <= 4000):
            raise ValueError("Both token values must contain 16–4000 characters")
        user = str(discord_id)
        payload = self.cipher.encrypt(json.dumps({"bearer": bearer, "refresh": refresh}).encode())
        with self.db:
            slots = {row[0] for row in self.db.execute(
                "SELECT slot FROM auth_slots WHERE discord_id = ?", (user,)
            )}
            slot = next((n for n in range(1, 11) if n not in slots), None)
            if slot is None:
                raise ValueError("All ten token slots are full")
            self.db.execute(
                "INSERT INTO auth_slots(discord_id, slot, credentials, created) VALUES(?, ?, ?, ?)",
                (user, slot, payload, int(time.time())),
            )
        return slot

    def list_slots(self, discord_id: int) -> tuple[list[int], int | None]:
        user = str(discord_id)
        slots = [row[0] for row in self.db.execute(
            "SELECT slot FROM auth_slots WHERE discord_id = ? ORDER BY slot", (user,)
        )]
        row = self.db.execute(
            "SELECT slot FROM auth_selection WHERE discord_id = ?", (user,)
        ).fetchone()
        return slots, row[0] if row else None

    def select(self, discord_id: int, slot: int) -> int:
        self.require_key()
        user = str(discord_id)
        with self.db:
            row = self.db.execute(
                "SELECT 1 FROM auth_slots WHERE discord_id = ? AND slot = ?", (user, slot)
            ).fetchone()
            if not row:
                raise ValueError("That token slot does not exist")
            self.db.execute(
                "INSERT INTO auth_selection(discord_id, slot, revision) VALUES(?, ?, 1) "
                "ON CONFLICT(discord_id) DO UPDATE SET slot=excluded.slot, revision=revision+1",
                (user, slot),
            )
            revision = self.db.execute(
                "SELECT revision FROM auth_selection WHERE discord_id = ?", (user,)
            ).fetchone()[0]
        return revision

    def selection(self, discord_id: str) -> dict:
        row = self.db.execute(
            "SELECT slot, revision FROM auth_selection WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        if not row:
            return {"status": "none"}
        return {"status": "pending_game_auth", "slot": row[0], "revision": row[1]}

    def delete(self, discord_id: int, slot: int) -> bool:
        user = str(discord_id)
        with self.db:
            cursor = self.db.execute(
                "DELETE FROM auth_slots WHERE discord_id = ? AND slot = ?", (user, slot)
            )
            self.db.execute(
                "DELETE FROM auth_selection WHERE discord_id = ? AND slot = ?", (user, slot)
            )
        return cursor.rowcount == 1

    def clear(self, discord_id: int):
        user = str(discord_id)
        with self.db:
            self.db.execute("DELETE FROM auth_selection WHERE discord_id = ?", (user,))
            self.db.execute("DELETE FROM auth_slots WHERE discord_id = ?", (user,))

    def decrypt_for_future_adapter(self, discord_id: int, slot: int) -> dict:
        """Use only in a verified game-auth adapter; never put its result in Discord or HTTP responses."""
        self.require_key()
        row = self.db.execute(
            "SELECT credentials FROM auth_slots WHERE discord_id = ? AND slot = ?",
            (str(discord_id), slot),
        ).fetchone()
        if not row:
            raise ValueError("That token slot does not exist")
        try:
            return json.loads(self.cipher.decrypt(row[0]))
        except (InvalidToken, ValueError) as error:
            raise RuntimeError("Token cannot be decrypted with this host key") from error
