"""Небольшое постоянное хранилище доступа и журнала оферов."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class OfferLog:
    id: int
    buyer_id: int
    buyer_label: str
    seller_id: int
    seller_label: str
    nft_link: str
    price: str
    locale: str
    status: str
    chat_id: int | None
    message_id: int | None
    admin_message_id: int | None
    business_connection_id: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "OfferLog":
        return cls(**dict(row))


class Store:
    """SQLite-хранилище без платёжных реквизитов и пользовательских секретов."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS members (
                    user_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'revoked')),
                    requested_at TEXT NOT NULL,
                    activated_at TEXT,
                    expires_at TEXT
                );

                CREATE TABLE IF NOT EXISTS offer_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    buyer_id INTEGER NOT NULL,
                    buyer_label TEXT NOT NULL,
                    seller_id INTEGER NOT NULL,
                    seller_label TEXT NOT NULL,
                    nft_link TEXT NOT NULL,
                    price TEXT NOT NULL,
                    locale TEXT NOT NULL,
                    status TEXT NOT NULL,
                    chat_id INTEGER,
                    message_id INTEGER,
                    admin_message_id INTEGER,
                    business_connection_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_offer_log_message
                    ON offer_log(chat_id, message_id);

                CREATE TABLE IF NOT EXISTS offer_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id INTEGER NOT NULL REFERENCES offer_log(id),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS access_payments (
                    invoice_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    payment_url TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'expired')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_access_payments_status
                    ON access_payments(status, created_at);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            member_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(members)").fetchall()
            }
            if "expires_at" not in member_columns:
                connection.execute("ALTER TABLE members ADD COLUMN expires_at TEXT")
            offer_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(offer_log)").fetchall()
            }
            if "business_connection_id" not in offer_columns:
                connection.execute("ALTER TABLE offer_log ADD COLUMN business_connection_id TEXT")

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_setting(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def is_active(self, user_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, expires_at FROM members WHERE user_id = ?", (user_id,)
            ).fetchone()
        return (
            row is not None
            and row["status"] == "active"
            and (row["expires_at"] is None or str(row["expires_at"]) > _now())
        )

    def access_expiry(self, user_id: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM members WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ).fetchone()
        return str(row["expires_at"]) if row and row["expires_at"] else None

    def request_access(self, user_id: int) -> bool:
        """Создаёт заявку на доступ. True, если она новая."""
        now = _now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, expires_at FROM members WHERE user_id = ?", (user_id,)
            ).fetchone()
            if (
                row is not None
                and row["status"] == "active"
                and (row["expires_at"] is None or str(row["expires_at"]) > now)
            ):
                return False
            is_new = row is None or row["status"] != "pending"
            connection.execute(
                """
                INSERT INTO members (user_id, status, requested_at, activated_at)
                VALUES (?, 'pending', ?, NULL)
                ON CONFLICT(user_id) DO UPDATE SET
                    status = 'pending', requested_at = excluded.requested_at,
                    activated_at = NULL, expires_at = NULL
                """,
                (user_id, now),
            )
        return is_new

    def grant_access(self, user_id: int, duration_days: int | None = None) -> str | None:
        if duration_days is not None and duration_days <= 0:
            raise ValueError("duration_days must be positive")
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat(timespec="seconds")
        with self._connect() as connection:
            expires_at = None
            if duration_days is not None:
                existing = connection.execute(
                    "SELECT status, expires_at FROM members WHERE user_id = ?", (user_id,)
                ).fetchone()
                base_dt = now_dt
                if existing is not None and existing["status"] == "active" and existing["expires_at"]:
                    current_expiry = datetime.fromisoformat(str(existing["expires_at"]))
                    if current_expiry > base_dt:
                        base_dt = current_expiry
                expires_at = (base_dt + timedelta(days=duration_days)).isoformat(timespec="seconds")
            connection.execute(
                """
                INSERT INTO members (user_id, status, requested_at, activated_at, expires_at)
                VALUES (?, 'active', ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    status = 'active', activated_at = excluded.activated_at,
                    expires_at = excluded.expires_at
                """,
                (user_id, now, now, expires_at),
            )
        return expires_at

    def revoke_access(self, user_id: int) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO members (user_id, status, requested_at, activated_at)
                VALUES (?, 'revoked', ?, NULL)
                ON CONFLICT(user_id) DO UPDATE SET
                    status = 'revoked', activated_at = NULL, expires_at = NULL
                """,
                (user_id, now),
            )

    def pending_payment_for_user(self, user_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """SELECT invoice_id, user_id, payment_url FROM access_payments
                   WHERE user_id = ? AND status = 'pending'
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id,),
            ).fetchone()

    def add_access_payment(self, invoice_id: str, user_id: int, payment_url: str) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO access_payments
                   (invoice_id, user_id, payment_url, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'pending', ?, ?)""",
                (invoice_id, user_id, payment_url, now, now),
            )

    def pending_access_payments(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """SELECT invoice_id, user_id FROM access_payments
                   WHERE status = 'pending' ORDER BY created_at"""
            ).fetchall()

    def finish_access_payment(self, invoice_id: str, *, paid: bool) -> int | None:
        """Однократно меняет статус счёта и, при оплате, открывает доступ."""
        now = _now()
        status = "paid" if paid else "expired"
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id FROM access_payments WHERE invoice_id = ? AND status = 'pending'",
                (invoice_id,),
            ).fetchone()
            if row is None:
                return None
            user_id = int(row["user_id"])
            connection.execute(
                "UPDATE access_payments SET status = ?, updated_at = ? WHERE invoice_id = ?",
                (status, now, invoice_id),
            )
            if paid:
                connection.execute(
                    """INSERT INTO members (user_id, status, requested_at, activated_at, expires_at)
                       VALUES (?, 'active', ?, ?, NULL)
                       ON CONFLICT(user_id) DO UPDATE SET
                           status = 'active', activated_at = excluded.activated_at,
                           expires_at = NULL""",
                    (user_id, now, now),
                )
        return user_id

    def create_offer(
        self,
        *,
        buyer_id: int,
        buyer_label: str,
        seller_id: int,
        seller_label: str,
        nft_link: str,
        price: str,
        locale: str,
    ) -> OfferLog:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO offer_log
                (buyer_id, buyer_label, seller_id, seller_label, nft_link, price,
                 locale, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'sent', ?, ?)
                """,
                (
                    buyer_id,
                    buyer_label,
                    seller_id,
                    seller_label,
                    nft_link,
                    price,
                    locale,
                    now,
                    now,
                ),
            )
            offer_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO offer_events (offer_id, status, created_at) VALUES (?, 'sent', ?)",
                (offer_id, now),
            )
            row = connection.execute("SELECT * FROM offer_log WHERE id = ?", (offer_id,)).fetchone()
        assert row is not None
        return OfferLog.from_row(row)

    def bind_offer_message(
        self,
        offer_id: int,
        chat_id: int,
        message_id: int,
        business_connection_id: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE offer_log SET chat_id = ?, message_id = ?, business_connection_id = ? WHERE id = ?",
                (chat_id, message_id, business_connection_id, offer_id),
            )

    def set_admin_message(self, offer_id: int, message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE offer_log SET admin_message_id = ? WHERE id = ?",
                (message_id, offer_id),
            )

    def get_offer(self, offer_id: int) -> OfferLog | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM offer_log WHERE id = ?", (offer_id,)).fetchone()
        return OfferLog.from_row(row) if row else None

    def get_offer_by_message(self, chat_id: int, message_id: int) -> OfferLog | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM offer_log WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
        return OfferLog.from_row(row) if row else None

    def set_offer_status(self, offer_id: int, status: str) -> OfferLog | None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE offer_log SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, offer_id),
            )
            connection.execute(
                "INSERT INTO offer_events (offer_id, status, created_at) VALUES (?, ?, ?)",
                (offer_id, status, now),
            )
            row = connection.execute("SELECT * FROM offer_log WHERE id = ?", (offer_id,)).fetchone()
        return OfferLog.from_row(row) if row else None

    def statistics(self) -> tuple[int, dict[str, int]]:
        with self._connect() as connection:
            active = int(
                connection.execute(
                    "SELECT COUNT(*) FROM members WHERE status = 'active' "
                    "AND (expires_at IS NULL OR expires_at > ?)",
                    (_now(),),
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM offer_log GROUP BY status"
            ).fetchall()
        return active, {row["status"]: int(row["count"]) for row in rows}
