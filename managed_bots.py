"""Encrypted registry and creation permissions for Telegram managed bots."""
from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "master_bots.sqlite3"
KEY_PATH = DATA_DIR / "master_bot.key"
INTENT_TTL = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(UTC)


def _load_cipher() -> Fernet:
    configured_key = os.getenv("MASTER_BOT_ENCRYPTION_KEY", "").strip()
    if configured_key:
        key = configured_key.encode("ascii")
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not KEY_PATH.exists() and DB_PATH.exists():
            try:
                with sqlite3.connect(DB_PATH) as connection:
                    table = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'managed_bots'"
                    ).fetchone()
                    existing_bots = (
                        connection.execute("SELECT COUNT(*) FROM managed_bots").fetchone()[0]
                        if table else 0
                    )
            except sqlite3.DatabaseError:
                existing_bots = 0
            if existing_bots:
                raise RuntimeError(
                    "data/master_bot.key is missing; restore it to decrypt managed bot tokens"
                )
        try:
            descriptor = os.open(KEY_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            key = KEY_PATH.read_bytes().strip()
            try:
                os.chmod(KEY_PATH, 0o600)
            except OSError:
                pass
        else:
            key = Fernet.generate_key()
            with os.fdopen(descriptor, "wb") as key_file:
                key_file.write(key + b"\n")
            try:
                os.chmod(KEY_PATH, 0o600)
            except OSError:
                pass
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("MASTER_BOT_ENCRYPTION_KEY must be a Fernet key") from exc


class ManagedBotStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cipher = _load_cipher()
        self._init_schema()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS managed_bots (
                    bot_user_id INTEGER PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    owner_username TEXT,
                    bot_username TEXT,
                    bot_name TEXT NOT NULL,
                    profile TEXT NOT NULL CHECK (profile IN ('free', 'paid')),
                    token_ciphertext TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_managed_bots_owner
                    ON managed_bots(owner_id, created_at);

                CREATE TABLE IF NOT EXISTS creation_intents (
                    owner_id INTEGER PRIMARY KEY,
                    profile TEXT NOT NULL CHECK (profile IN ('free', 'paid')),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paid_creator_access (
                    user_id INTEGER PRIMARY KEY,
                    granted_by INTEGER NOT NULL,
                    granted_at TEXT NOT NULL
                );
                """
            )

    def grant_paid_creation(self, user_id: int, admin_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO paid_creator_access (user_id, granted_by, granted_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       granted_by = excluded.granted_by,
                       granted_at = excluded.granted_at""",
                (user_id, admin_id, _now().isoformat(timespec="seconds")),
            )

    def revoke_paid_creation(self, user_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM paid_creator_access WHERE user_id = ?", (user_id,))

    def can_create_paid(self, user_id: int) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM paid_creator_access WHERE user_id = ?", (user_id,)
            ).fetchone() is not None

    def set_intent(self, owner_id: int, profile: str) -> None:
        if profile not in {"free", "paid"}:
            raise ValueError("Unknown managed bot profile")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO creation_intents (owner_id, profile, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(owner_id) DO UPDATE SET
                       profile = excluded.profile, created_at = excluded.created_at""",
                (owner_id, profile, _now().isoformat(timespec="seconds")),
            )

    def _take_intent(self, connection: sqlite3.Connection, owner_id: int) -> str | None:
        row = connection.execute(
            "SELECT profile, created_at FROM creation_intents WHERE owner_id = ?", (owner_id,)
        ).fetchone()
        connection.execute("DELETE FROM creation_intents WHERE owner_id = ?", (owner_id,))
        if row is None:
            return None
        try:
            created_at = datetime.fromisoformat(str(row["created_at"]))
        except ValueError:
            return None
        if _now() - created_at > INTENT_TTL:
            return None
        return str(row["profile"])

    def register_bot(
        self,
        *,
        owner_id: int,
        owner_username: str | None,
        bot_user_id: int,
        bot_username: str | None,
        bot_name: str,
        token: str,
    ) -> tuple[str | None, bool]:
        """Register a created bot; return its profile and whether it was added/changed."""
        now = _now().isoformat(timespec="seconds")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT profile, token_ciphertext FROM managed_bots WHERE bot_user_id = ?",
                (bot_user_id,),
            ).fetchone()
            if row is not None:
                profile = str(row["profile"])
                try:
                    previous_token = self.cipher.decrypt(
                        str(row["token_ciphertext"]).encode("ascii")
                    ).decode("utf-8")
                except (InvalidToken, UnicodeError):
                    previous_token = ""
                changed = previous_token != token
                if changed:
                    ciphertext = self.cipher.encrypt(token.encode("utf-8")).decode("ascii")
                    connection.execute(
                        """UPDATE managed_bots SET owner_id = ?, owner_username = ?,
                           bot_username = ?, bot_name = ?, token_ciphertext = ?,
                           enabled = 1, updated_at = ? WHERE bot_user_id = ?""",
                        (owner_id, owner_username, bot_username, bot_name, ciphertext, now, bot_user_id),
                    )
                else:
                    connection.execute(
                        """UPDATE managed_bots SET owner_id = ?, owner_username = ?,
                           bot_username = ?, bot_name = ?, enabled = 1, updated_at = ?
                           WHERE bot_user_id = ?""",
                        (owner_id, owner_username, bot_username, bot_name, now, bot_user_id),
                    )
                return profile, changed

            profile = self._take_intent(connection, owner_id)
            if profile is None:
                return None, False
            ciphertext = self.cipher.encrypt(token.encode("utf-8")).decode("ascii")
            connection.execute(
                """INSERT INTO managed_bots
                   (bot_user_id, owner_id, owner_username, bot_username, bot_name,
                    profile, token_ciphertext, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (bot_user_id, owner_id, owner_username, bot_username, bot_name,
                 profile, ciphertext, now, now),
            )
            return profile, True

    def get_bot_config(self, bot_user_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT bot_user_id, owner_id, bot_username, bot_name, profile
                   FROM managed_bots WHERE bot_user_id = ? AND enabled = 1""",
                (bot_user_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_bot_token(self, bot_user_id: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT token_ciphertext FROM managed_bots WHERE bot_user_id = ? AND enabled = 1",
                (bot_user_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return self.cipher.decrypt(str(row["token_ciphertext"]).encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise RuntimeError("Stored managed bot token cannot be decrypted") from exc

    def replace_bot_token(self, bot_user_id: int, token: str) -> bool:
        ciphertext = self.cipher.encrypt(token.encode("utf-8")).decode("ascii")
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE managed_bots SET token_ciphertext = ?, updated_at = ?
                   WHERE bot_user_id = ? AND enabled = 1""",
                (ciphertext, _now().isoformat(timespec="seconds"), bot_user_id),
            )
        return cursor.rowcount > 0

    def active_bot_ids(self) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT bot_user_id FROM managed_bots WHERE enabled = 1 ORDER BY created_at"
            ).fetchall()
        return [int(row[0]) for row in rows]

    def bots_for_owner(self, owner_id: int) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """SELECT bot_user_id, bot_username, bot_name, profile, created_at
                   FROM managed_bots WHERE owner_id = ? AND enabled = 1
                   ORDER BY created_at DESC""",
                (owner_id,),
            ).fetchall()

    def all_active_bots(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """SELECT bot_user_id, owner_id, bot_username, bot_name, profile, created_at
                   FROM managed_bots WHERE enabled = 1 ORDER BY created_at DESC"""
            ).fetchall()

