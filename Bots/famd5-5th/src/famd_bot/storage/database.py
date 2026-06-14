from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time
from pathlib import Path
from typing import Any

from famd_bot.models import (
    AdminUser,
    ChangeRequest,
    CombinedUserStats,
    Employee,
    MessageRef,
    PayoutPreviewItem,
    PayoutSnapshot,
    PayoutSnapshotItem,
    RequestStatus,
    RequestType,
    ServiceLog,
    ServiceLogKind,
    ServiceLogProof,
    ServiceLogStatus,
    ServicePeriodTotals,
    Shift,
    ShiftBreak,
    ShiftPeriodTotals,
    ShiftStats,
    ShiftStatus,
    UserForumThread,
    UserLogThread,
)
from famd_bot.payout_rates import PayoutRates
from famd_bot.storage.migrations import run_migrations
from famd_bot.time_utils import format_db_dt, localize, parse_db_dt, rendered_minutes, timezone_for, utc_now, week_start_for


class Database:
    def __init__(self, connection: sqlite3.Connection, database_path: Path) -> None:
        self._connection = connection
        self._database_path = database_path

    @classmethod
    async def connect(cls, database_path: Path) -> Database:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        migrations_dir = Path(__file__).resolve().parents[3] / "migrations"
        run_migrations(connection, migrations_dir)
        return cls(connection, database_path)

    async def close(self) -> None:
        self._connection.close()

    async def get_setting(self, key: str) -> str | None:
        row = self._connection.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None

    async def set_setting(self, key: str, value: str) -> None:
        self._connection.execute(
            """
            INSERT INTO bot_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self._connection.commit()

    async def seed_admins(self, user_ids: list[int]) -> None:
        if await self.get_setting("initial_admins_seeded") == "true":
            return
        now = format_db_dt(utc_now())
        try:
            self._connection.execute("BEGIN")
            for user_id in dict.fromkeys(str(value) for value in user_ids if int(value) > 0):
                self._connection.execute(
                    """
                    INSERT INTO admin_users (
                        user_id, display_name, active, added_by_user_id, added_by_display_name, added_at_utc
                    ) VALUES (?, ?, 1, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET active = 1
                    """,
                    (user_id, user_id, "system", "System", now),
                )
            self._connection.execute(
                """
                INSERT INTO bot_settings (key, value)
                VALUES ('initial_admins_seeded', 'true')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    async def is_admin(self, user_id: int | str) -> bool:
        row = self._connection.execute(
            "SELECT active FROM admin_users WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
        return row is not None and int(row["active"]) == 1

    async def list_admins(self) -> list[AdminUser]:
        rows = self._connection.execute(
            "SELECT user_id, display_name, active FROM admin_users WHERE active = 1 ORDER BY user_id"
        ).fetchall()
        return [
            AdminUser(user_id=str(row["user_id"]), display_name=str(row["display_name"]), active=bool(row["active"]))
            for row in rows
        ]

    async def add_admin(
        self,
        user_id: int | str,
        display_name: str,
        *,
        actor_user_id: int | str,
        actor_display_name: str,
    ) -> None:
        now = utc_now()
        self._connection.execute(
            """
            INSERT INTO admin_users (
                user_id, display_name, active, added_by_user_id, added_by_display_name, added_at_utc,
                removed_by_user_id, removed_by_display_name, removed_at_utc
            ) VALUES (?, ?, 1, ?, ?, ?, NULL, NULL, NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name = excluded.display_name,
                active = 1,
                added_by_user_id = excluded.added_by_user_id,
                added_by_display_name = excluded.added_by_display_name,
                added_at_utc = excluded.added_at_utc,
                removed_by_user_id = NULL,
                removed_by_display_name = NULL,
                removed_at_utc = NULL
            """,
            (str(user_id), display_name, str(actor_user_id), actor_display_name, format_db_dt(now)),
        )
        self._insert_event(None, str(actor_user_id), actor_display_name, "admin_added", {}, {"user_id": str(user_id)})
        self._connection.commit()

    async def remove_admin(
        self,
        user_id: int | str,
        *,
        actor_user_id: int | str,
        actor_display_name: str,
    ) -> None:
        now = utc_now()
        self._connection.execute(
            """
            UPDATE admin_users
               SET active = 0,
                   removed_by_user_id = ?,
                   removed_by_display_name = ?,
                   removed_at_utc = ?
             WHERE user_id = ?
            """,
            (str(actor_user_id), actor_display_name, format_db_dt(now), str(user_id)),
        )
        self._insert_event(None, str(actor_user_id), actor_display_name, "admin_removed", {"user_id": str(user_id)}, {})
        self._connection.commit()

    async def list_employees(self) -> list[Employee]:
        rows = self._connection.execute(
            "SELECT user_id, display_name, rank, active FROM employees WHERE active = 1 ORDER BY rank, display_name, user_id"
        ).fetchall()
        return [self._row_to_employee(row) for row in rows]

    async def get_employee(self, user_id: int | str) -> Employee | None:
        row = self._connection.execute(
            "SELECT user_id, display_name, rank, active FROM employees WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
        return self._row_to_employee(row) if row is not None else None

    async def upsert_employee(
        self,
        *,
        user_id: int | str,
        display_name: str,
        rank: str,
        actor_user_id: int | str,
        actor_display_name: str,
    ) -> None:
        now = format_db_dt(utc_now())
        self._connection.execute(
            """
            INSERT INTO employees (
                user_id, display_name, rank, active, added_by_user_id, added_by_display_name,
                added_at_utc, updated_by_user_id, updated_by_display_name, updated_at_utc,
                removed_by_user_id, removed_by_display_name, removed_at_utc
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name = excluded.display_name,
                rank = excluded.rank,
                active = 1,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_by_display_name = excluded.updated_by_display_name,
                updated_at_utc = excluded.updated_at_utc,
                removed_by_user_id = NULL,
                removed_by_display_name = NULL,
                removed_at_utc = NULL
            """,
            (
                str(user_id),
                display_name,
                rank,
                str(actor_user_id),
                actor_display_name,
                now,
                str(actor_user_id),
                actor_display_name,
                now,
            ),
        )
        self._insert_event(None, str(actor_user_id), actor_display_name, "employee_upserted", {}, {"user_id": str(user_id), "rank": rank})
        self._connection.commit()

    async def remove_employee(
        self,
        user_id: int | str,
        *,
        actor_user_id: int | str,
        actor_display_name: str,
    ) -> None:
        now = format_db_dt(utc_now())
        self._connection.execute(
            """
            UPDATE employees
               SET active = 0,
                   removed_by_user_id = ?,
                   removed_by_display_name = ?,
                   removed_at_utc = ?,
                   updated_by_user_id = ?,
                   updated_by_display_name = ?,
                   updated_at_utc = ?
             WHERE user_id = ?
            """,
            (str(actor_user_id), actor_display_name, now, str(actor_user_id), actor_display_name, now, str(user_id)),
        )
        self._insert_event(None, str(actor_user_id), actor_display_name, "employee_removed", {"user_id": str(user_id)}, {})
        self._connection.commit()

    async def update_employee_rank(
        self,
        user_id: int | str,
        rank: str,
        *,
        actor_user_id: int | str,
        actor_display_name: str,
    ) -> None:
        now = format_db_dt(utc_now())
        self._connection.execute(
            """
            UPDATE employees
               SET rank = ?,
                   updated_by_user_id = ?,
                   updated_by_display_name = ?,
                   updated_at_utc = ?
             WHERE user_id = ? AND active = 1
            """,
            (rank, str(actor_user_id), actor_display_name, now, str(user_id)),
        )
        self._insert_event(None, str(actor_user_id), actor_display_name, "employee_rank_updated", {}, {"user_id": str(user_id), "rank": rank})
        self._connection.commit()

    async def best_display_name_for_user(self, user_id: int | str) -> str | None:
        user_key = str(user_id)
        queries = [
            (
                "SELECT display_name AS value FROM employees WHERE user_id = ? AND display_name != '' AND display_name != ?",
                (user_key, user_key),
            ),
            (
                """
                SELECT display_name AS value
                  FROM shifts
                 WHERE user_id = ?
                   AND display_name != ''
                   AND display_name != ?
                 ORDER BY updated_at_utc DESC
                 LIMIT 1
                """,
                (user_key, user_key),
            ),
            (
                """
                SELECT display_name AS value
                  FROM service_logs
                 WHERE user_id = ?
                   AND display_name != ''
                   AND display_name != ?
                 ORDER BY updated_at_utc DESC
                 LIMIT 1
                """,
                (user_key, user_key),
            ),
            (
                "SELECT thread_name AS value FROM user_log_threads WHERE user_id = ? AND thread_name != ''",
                (user_key,),
            ),
            (
                "SELECT thread_name AS value FROM user_forum_threads WHERE user_id = ? AND thread_name != '' ORDER BY updated_at_utc DESC LIMIT 1",
                (user_key,),
            ),
        ]
        for query, params in queries:
            row = self._connection.execute(query, params).fetchone()
            if row is not None and row["value"]:
                return str(row["value"])
        return None

    async def early_paid_user_ids_since_last_general(self) -> set[str]:
        cutoff = await self.last_general_snapshot_at()
        rows = self._connection.execute(
            """
            SELECT DISTINCT payout_snapshot_items.user_id
              FROM payout_snapshot_items
              JOIN payout_snapshots ON payout_snapshots.id = payout_snapshot_items.snapshot_id
             WHERE payout_snapshots.snapshot_type = 'early'
               AND payout_snapshots.status = 'active'
               AND payout_snapshots.created_at_utc > ?
            """,
            (format_db_dt(cutoff),),
        ).fetchall()
        return {str(row["user_id"]) for row in rows}

    async def last_general_snapshot_at(self) -> datetime:
        row = self._connection.execute(
            """
            SELECT MAX(created_at_utc) AS created_at
              FROM payout_snapshots
             WHERE snapshot_type = 'general' AND status = 'active'
            """
        ).fetchone()
        if row is None or not row["created_at"]:
            return datetime(1970, 1, 1, tzinfo=utc_now().tzinfo)
        return parse_db_dt(str(row["created_at"]))

    async def build_payout_preview(
        self,
        *,
        rates: PayoutRates,
        user_ids: list[int | str] | None = None,
        include_skipped: bool = False,
    ) -> list[PayoutPreviewItem]:
        employees = await self._payout_employees(user_ids)
        skipped_users = await self.early_paid_user_ids_since_last_general() if user_ids is None else set()
        cutoff_end = utc_now()
        items: list[PayoutPreviewItem] = []
        for employee in employees:
            skipped = employee.user_id in skipped_users and not include_skipped
            if skipped:
                items.append(PayoutPreviewItem(employee.user_id, employee.rank, 0, 0, 0, 0, skipped=True))
                continue
            duty_minutes = self._unaccounted_shift_minutes(employee.user_id, cutoff_end)
            response_count = self._unaccounted_response_count(employee.user_id, cutoff_end)
            vital_count = self._unaccounted_vital_count(employee.user_id, cutoff_end)
            payout_total = rates.payout_for(
                rank=employee.rank,
                duty_minutes=duty_minutes,
                response_count=response_count,
                vital_count=vital_count,
            )
            items.append(PayoutPreviewItem(employee.user_id, employee.rank, duty_minutes, response_count, vital_count, payout_total))
        return items

    async def unregistered_payout_user_ids(self, user_ids: list[int | str] | None = None) -> list[str]:
        cutoff = format_db_dt(utc_now())
        params: list[Any]
        user_filter = ""
        if user_ids is not None:
            ordered = list(dict.fromkeys(str(user_id) for user_id in user_ids))
            if not ordered:
                return []
            placeholders = ",".join("?" for _ in ordered)
            user_filter = f" AND user_id IN ({placeholders})"
            params = [cutoff, *ordered, cutoff, *ordered]
        else:
            params = [cutoff, cutoff]
        rows = self._connection.execute(
            f"""
            WITH unpaid_users AS (
                SELECT user_id
                  FROM shifts
                 WHERE status = 'valid'
                   AND clock_out_utc IS NOT NULL
                   AND payout_snapshot_id IS NULL
                   AND clock_out_utc <= ?
                   {user_filter}
                UNION
                SELECT user_id
                  FROM service_logs
                 WHERE status = 'valid'
                   AND payout_snapshot_id IS NULL
                   AND created_at_utc <= ?
                   {user_filter}
            )
            SELECT unpaid_users.user_id
              FROM unpaid_users
              LEFT JOIN employees
                ON employees.user_id = unpaid_users.user_id
               AND employees.active = 1
             WHERE employees.user_id IS NULL
             ORDER BY unpaid_users.user_id
            """,
            tuple(params),
        ).fetchall()
        return [str(row["user_id"]) for row in rows]

    async def create_payout_snapshot(
        self,
        *,
        snapshot_type: str,
        rates: PayoutRates,
        user_ids: list[int | str] | None,
        include_skipped: bool,
        actor_user_id: int | str,
        actor_display_name: str,
    ) -> tuple[PayoutSnapshot, list[PayoutSnapshotItem]]:
        if snapshot_type not in {"general", "early"}:
            raise ValueError("Snapshot type must be general or early.")
        cutoff_start = await self.last_general_snapshot_at()
        cutoff_end = utc_now()
        preview = await self.build_payout_preview(rates=rates, user_ids=user_ids, include_skipped=include_skipped)
        now = format_db_dt(cutoff_end)
        try:
            self._connection.execute("BEGIN")
            cursor = self._connection.execute(
                """
                INSERT INTO payout_snapshots (
                    snapshot_type, status, created_by_user_id, created_by_display_name,
                    created_at_utc, cutoff_start_utc, cutoff_end_utc, include_skipped
                ) VALUES (?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_type,
                    str(actor_user_id),
                    actor_display_name,
                    now,
                    format_db_dt(cutoff_start),
                    now,
                    1 if include_skipped else 0,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            for item in preview:
                self._connection.execute(
                    """
                    INSERT INTO payout_snapshot_items (
                        snapshot_id, user_id, rank, duty_minutes, response_count, vital_count,
                        payout_total, skipped, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        item.user_id,
                        item.rank,
                        item.duty_minutes,
                        item.response_count,
                        item.vital_count,
                        item.payout_total,
                        1 if item.skipped else 0,
                        now,
                    ),
                )
                if not item.skipped:
                    self._mark_user_logs_accounted(item.user_id, snapshot_id, cutoff_end)
            self._insert_event(
                None,
                str(actor_user_id),
                actor_display_name,
                "payout_snapshot_created",
                {},
                {"snapshot_id": snapshot_id, "snapshot_type": snapshot_type},
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        snapshot = await self.get_payout_snapshot(snapshot_id)
        if snapshot is None:
            raise RuntimeError("Snapshot disappeared after creation.")
        return snapshot, await self.payout_snapshot_items(snapshot_id)

    async def get_payout_snapshot(self, snapshot_id: int) -> PayoutSnapshot | None:
        row = self._connection.execute("SELECT * FROM payout_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        return self._row_to_payout_snapshot(row) if row is not None else None

    async def list_active_payout_snapshots(self, limit: int = 25) -> list[PayoutSnapshot]:
        rows = self._connection.execute(
            """
            SELECT * FROM payout_snapshots
             WHERE status = 'active'
             ORDER BY created_at_utc DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._row_to_payout_snapshot(row) for row in rows]

    async def payout_snapshot_items(self, snapshot_id: int) -> list[PayoutSnapshotItem]:
        rows = self._connection.execute(
            "SELECT * FROM payout_snapshot_items WHERE snapshot_id = ? ORDER BY user_id",
            (snapshot_id,),
        ).fetchall()
        return [self._row_to_payout_snapshot_item(row) for row in rows]

    async def revert_payout_snapshot(
        self,
        snapshot_id: int,
        *,
        actor_user_id: int | str,
        actor_display_name: str,
    ) -> None:
        snapshot = await self.get_payout_snapshot(snapshot_id)
        if snapshot is None or snapshot.status != "active":
            raise ValueError("Snapshot is not active.")
        now = format_db_dt(utc_now())
        try:
            self._connection.execute("BEGIN")
            self._connection.execute("UPDATE shifts SET payout_snapshot_id = NULL WHERE payout_snapshot_id = ?", (snapshot_id,))
            self._connection.execute("UPDATE service_logs SET payout_snapshot_id = NULL WHERE payout_snapshot_id = ?", (snapshot_id,))
            self._connection.execute(
                """
                UPDATE payout_snapshots
                   SET status = 'reverted',
                       reverted_by_user_id = ?,
                       reverted_by_display_name = ?,
                       reverted_at_utc = ?
                 WHERE id = ?
                """,
                (str(actor_user_id), actor_display_name, now, snapshot_id),
            )
            self._insert_event(None, str(actor_user_id), actor_display_name, "payout_snapshot_reverted", {"snapshot_id": snapshot_id}, {})
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    async def active_shift_for_user(self, user_id: int | str) -> Shift | None:
        row = self._connection.execute(
            """
            SELECT
                shifts.*,
                open_break.id AS current_break_id,
                open_break.break_start_utc AS current_break_start_utc
            FROM shifts
            LEFT JOIN shift_breaks open_break
              ON open_break.shift_id = shifts.id
             AND open_break.break_end_utc IS NULL
             WHERE user_id = ? AND status = 'active'
             ORDER BY clock_in_utc DESC
             LIMIT 1
            """,
            (str(user_id),),
        ).fetchone()
        return self._row_to_shift(row) if row is not None else None

    async def list_active_shifts(self) -> list[Shift]:
        rows = self._connection.execute(
            """
            SELECT
                shifts.*,
                open_break.id AS current_break_id,
                open_break.break_start_utc AS current_break_start_utc
            FROM shifts
            LEFT JOIN shift_breaks open_break
              ON open_break.shift_id = shifts.id
             AND open_break.break_end_utc IS NULL
            WHERE status = 'active'
            ORDER BY clock_in_utc ASC
            """
        ).fetchall()
        return [self._row_to_shift(row) for row in rows]

    async def get_shift(self, shift_id: int) -> Shift | None:
        row = self._connection.execute(
            """
            SELECT
                shifts.*,
                open_break.id AS current_break_id,
                open_break.break_start_utc AS current_break_start_utc
            FROM shifts
            LEFT JOIN shift_breaks open_break
              ON open_break.shift_id = shifts.id
             AND open_break.break_end_utc IS NULL
            WHERE shifts.id = ?
            """,
            (shift_id,),
        ).fetchone()
        return self._row_to_shift(row) if row is not None else None

    async def start_shift(
        self,
        *,
        user_id: int | str,
        username: str,
        display_name: str,
        guild_id: int | str | None,
        actor_display_name: str,
        started_at: datetime | None = None,
    ) -> Shift:
        now = utc_now()
        start = started_at or now
        try:
            self._connection.execute("BEGIN")
            cursor = self._connection.execute(
                """
                INSERT INTO shifts (
                    user_id, username, display_name, guild_id, clock_in_utc, clock_out_utc,
                    rendered_minutes, break_minutes, unpaid_break_minutes, status, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, NULL, 0, 0, 0, 'active', ?, ?)
                """,
                (
                    str(user_id),
                    username,
                    display_name,
                    str(guild_id) if guild_id is not None else None,
                    format_db_dt(start),
                    format_db_dt(now),
                    format_db_dt(now),
                ),
            )
            shift_id = int(cursor.lastrowid)
            self._insert_event(
                shift_id,
                str(user_id),
                actor_display_name,
                "clock_in",
                {},
                {"clock_in_utc": format_db_dt(start)},
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        shift = await self.get_shift(shift_id)
        if shift is None:
            raise RuntimeError("Shift was not saved.")
        return shift

    async def close_shift(
        self,
        *,
        shift_id: int,
        actor_user_id: int | str,
        actor_display_name: str,
        clock_out_at: datetime | None = None,
        break_cap_minutes: int = 15,
        auto_clocked_out: bool = False,
    ) -> Shift:
        shift = await self.get_shift(shift_id)
        if shift is None:
            raise ValueError("Shift not found.")
        if shift.status is not ShiftStatus.ACTIVE:
            raise ValueError("Shift is not active.")
        end = clock_out_at or utc_now()
        now = utc_now()
        before = self._shift_snapshot(shift)
        try:
            self._connection.execute("BEGIN")
            self._close_open_break_in_transaction(
                shift,
                end,
                break_cap_minutes=break_cap_minutes,
                auto_clocked_out=auto_clocked_out,
            )
            minutes = rendered_minutes(shift.clock_in_utc, end)
            self._connection.execute(
                """
                UPDATE shifts
                   SET clock_out_utc = ?,
                       rendered_minutes = ?,
                       status = 'valid',
                       updated_at_utc = ?
                 WHERE id = ?
                """,
                (
                    format_db_dt(end),
                    minutes,
                    format_db_dt(now),
                    shift_id,
                ),
            )
            after = dict(before)
            after.update(
                {
                    "clock_out_utc": format_db_dt(end),
                    "rendered_minutes": minutes,
                    "status": "valid",
                }
            )
            self._insert_event(
                shift_id,
                str(actor_user_id),
                actor_display_name,
                "auto_clock_out" if auto_clocked_out else "clock_out",
                before,
                after,
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        updated = await self.get_shift(shift_id)
        if updated is None:
            raise RuntimeError("Shift disappeared after close.")
        return updated

    async def adjust_shift(
        self,
        *,
        shift_id: int,
        clock_in_utc: datetime,
        clock_out_utc: datetime,
        actor_user_id: int | str,
        actor_display_name: str,
        edit_summary: str = "",
    ) -> Shift:
        if clock_out_utc < clock_in_utc:
            raise ValueError("Clock-out cannot be earlier than time-in.")
        shift = await self.get_shift(shift_id)
        if shift is None:
            raise ValueError("Shift not found.")
        minutes = rendered_minutes(clock_in_utc, clock_out_utc)
        now = utc_now()
        before = self._shift_snapshot(shift)
        try:
            self._connection.execute("BEGIN")
            self._connection.execute(
                """
                UPDATE shifts
                   SET clock_in_utc = ?,
                       clock_out_utc = ?,
                       rendered_minutes = ?,
                       break_minutes = 0,
                       unpaid_break_minutes = 0,
                       status = CASE WHEN status = 'active' THEN 'valid' ELSE status END,
                       edited_by_user_id = ?,
                       edited_by_display_name = ?,
                       edited_at_utc = ?,
                       edit_summary = ?,
                       updated_at_utc = ?
                 WHERE id = ?
                """,
                (
                    format_db_dt(clock_in_utc),
                    format_db_dt(clock_out_utc),
                    minutes,
                    str(actor_user_id),
                    actor_display_name,
                    format_db_dt(now),
                    edit_summary.strip(),
                    format_db_dt(now),
                    shift_id,
                ),
            )
            after = dict(before)
            after.update(
                {
                    "clock_in_utc": format_db_dt(clock_in_utc),
                    "clock_out_utc": format_db_dt(clock_out_utc),
                    "rendered_minutes": minutes,
                    "edited_by_user_id": str(actor_user_id),
                }
            )
            self._insert_event(shift_id, str(actor_user_id), actor_display_name, "shift_adjusted", before, after)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        updated = await self.get_shift(shift_id)
        if updated is None:
            raise RuntimeError("Shift disappeared after adjust.")
        return updated

    async def set_shift_status(
        self,
        *,
        shift_id: int,
        status: ShiftStatus,
        actor_user_id: int | str,
        actor_display_name: str,
    ) -> Shift:
        if status is ShiftStatus.ACTIVE:
            raise ValueError("Cannot manually set a shift back to active.")
        shift = await self.get_shift(shift_id)
        if shift is None:
            raise ValueError("Shift not found.")
        before = self._shift_snapshot(shift)
        now = utc_now()
        try:
            self._connection.execute("BEGIN")
            self._connection.execute(
                "UPDATE shifts SET status = ?, updated_at_utc = ? WHERE id = ?",
                (status.value, format_db_dt(now), shift_id),
            )
            after = dict(before)
            after["status"] = status.value
            action = "shift_invalidated" if status is ShiftStatus.INVALIDATED else "shift_validated"
            self._insert_event(shift_id, str(actor_user_id), actor_display_name, action, before, after)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        updated = await self.get_shift(shift_id)
        if updated is None:
            raise RuntimeError("Shift disappeared after status update.")
        return updated

    async def salary_eligible_minutes(self, user_id: int | str | None = None) -> int:
        if user_id is None:
            query = "SELECT COALESCE(SUM(rendered_minutes), 0) AS total FROM shifts WHERE status = 'valid'"
            params: tuple[Any, ...] = ()
        else:
            query = "SELECT COALESCE(SUM(rendered_minutes), 0) AS total FROM shifts WHERE status = 'valid' AND user_id = ?"
            params = (str(user_id),)
        row = self._connection.execute(query, params).fetchone()
        return int(row["total"] or 0)

    async def list_shift_stats(self) -> list[ShiftStats]:
        rows = self._connection.execute(
            """
            SELECT
                user_id,
                COALESCE(SUM(rendered_minutes), 0) AS total_minutes,
                COUNT(*) AS shift_count
              FROM shifts
             WHERE status = 'valid'
             GROUP BY user_id
             ORDER BY user_id
            """
        ).fetchall()
        return [
            ShiftStats(
                user_id=str(row["user_id"]),
                total_minutes=int(row["total_minutes"] or 0),
                shift_count=int(row["shift_count"] or 0),
            )
            for row in rows
        ]

    async def list_combined_user_stats(self) -> list[CombinedUserStats]:
        shift_rows = self._connection.execute(
            """
            SELECT user_id, COALESCE(SUM(rendered_minutes), 0) AS minutes, COUNT(*) AS shifts
              FROM shifts
             WHERE status = 'valid'
             GROUP BY user_id
            """
        ).fetchall()
        service_rows = self._connection.execute(
            """
            SELECT
                user_id,
                COALESCE(SUM(response_count), 0) AS responses,
                COALESCE(SUM(treatment_count), 0) AS treatments,
                COALESCE(SUM(revival_count), 0) AS revivals,
                COALESCE(SUM(bodybag_count), 0) AS bodybags
              FROM service_logs
             WHERE status = 'valid'
             GROUP BY user_id
            """
        ).fetchall()
        stats: dict[str, CombinedUserStats] = {}
        for row in shift_rows:
            user_id = str(row["user_id"])
            stats[user_id] = CombinedUserStats(user_id, int(row["minutes"] or 0), int(row["shifts"] or 0), 0, 0, 0, 0)
        for row in service_rows:
            user_id = str(row["user_id"])
            current = stats.get(user_id) or CombinedUserStats(user_id, 0, 0, 0, 0, 0, 0)
            current.response_count = int(row["responses"] or 0)
            current.treatment_count = int(row["treatments"] or 0)
            current.revival_count = int(row["revivals"] or 0)
            current.bodybag_count = int(row["bodybags"] or 0)
            stats[user_id] = current
        return [stats[user_id] for user_id in sorted(stats)]

    async def create_service_log(
        self,
        *,
        log_kind: ServiceLogKind,
        user_id: int | str,
        username: str,
        display_name: str,
        guild_id: int | str | None,
        postal: str,
        response_type: str | None,
        responders_text: str,
        response_count: int,
        treatment_count: int,
        revival_count: int,
        bodybag_count: int,
    ) -> ServiceLog:
        now = utc_now()
        cursor = self._connection.execute(
            """
            INSERT INTO service_logs (
                log_kind, user_id, username, display_name, guild_id, postal, response_type, responders_text,
                response_count, treatment_count, revival_count, bodybag_count, status, created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_proof', ?, ?)
            """,
            (
                log_kind.value,
                str(user_id),
                username,
                display_name,
                str(guild_id) if guild_id is not None else None,
                postal.strip(),
                response_type,
                responders_text.strip(),
                max(0, response_count),
                max(0, treatment_count),
                max(0, revival_count),
                max(0, bodybag_count),
                format_db_dt(now),
                format_db_dt(now),
            ),
        )
        self._connection.commit()
        service_log = await self.get_service_log(int(cursor.lastrowid))
        if service_log is None:
            raise RuntimeError("Service log was not saved.")
        return service_log

    async def get_service_log(self, service_log_id: int) -> ServiceLog | None:
        row = self._connection.execute("SELECT * FROM service_logs WHERE id = ?", (service_log_id,)).fetchone()
        return self._row_to_service_log(row) if row is not None else None

    async def pending_service_log_for_user_thread(self, user_id: int | str, thread_id: int | str) -> ServiceLog | None:
        row = self._connection.execute(
            """
            SELECT service_logs.*
              FROM service_logs
              JOIN user_forum_threads
                ON user_forum_threads.user_id = service_logs.user_id
               AND user_forum_threads.thread_id = ?
               AND user_forum_threads.thread_type = service_logs.log_kind
             WHERE service_logs.user_id = ?
               AND service_logs.status = 'pending_proof'
             ORDER BY service_logs.created_at_utc DESC
             LIMIT 1
            """,
            (str(thread_id), str(user_id)),
        ).fetchone()
        return self._row_to_service_log(row) if row is not None else None

    async def pending_service_logs_before(self, cutoff_utc: datetime) -> list[ServiceLog]:
        rows = self._connection.execute(
            """
            SELECT *
              FROM service_logs
             WHERE status = 'pending_proof'
               AND created_at_utc <= ?
             ORDER BY created_at_utc ASC
            """,
            (format_db_dt(cutoff_utc),),
        ).fetchall()
        return [self._row_to_service_log(row) for row in rows]

    async def mark_service_log_proof_received(self, service_log_id: int) -> ServiceLog:
        now = utc_now()
        self._connection.execute(
            """
            UPDATE service_logs
               SET status = 'valid',
                   proof_received_at_utc = ?,
                   updated_at_utc = ?
             WHERE id = ?
            """,
            (format_db_dt(now), format_db_dt(now), service_log_id),
        )
        self._connection.commit()
        service_log = await self.get_service_log(service_log_id)
        if service_log is None:
            raise RuntimeError("Service log disappeared after proof update.")
        return service_log

    async def set_service_log_proof_prompt(
        self,
        *,
        service_log_id: int,
        channel_id: int | str,
        message_id: int | str,
    ) -> None:
        self._connection.execute(
            """
            UPDATE service_logs
               SET proof_prompt_channel_id = ?,
                   proof_prompt_message_id = ?,
                   updated_at_utc = ?
             WHERE id = ?
            """,
            (str(channel_id), str(message_id), format_db_dt(utc_now()), service_log_id),
        )
        self._connection.commit()

    async def update_service_log_details(
        self,
        *,
        service_log_id: int,
        postal: str,
        response_type: str | None,
        responders_text: str,
        response_count: int,
        treatment_count: int,
        revival_count: int,
        bodybag_count: int,
        actor_user_id: int | str,
        actor_display_name: str,
        edit_summary: str,
    ) -> ServiceLog:
        now = utc_now()
        self._connection.execute(
            """
            UPDATE service_logs
               SET postal = ?,
                   response_type = ?,
                   responders_text = ?,
                   response_count = ?,
                   treatment_count = ?,
                   revival_count = ?,
                   bodybag_count = ?,
                   edited_by_user_id = ?,
                   edited_by_display_name = ?,
                   edited_at_utc = ?,
                   edit_summary = ?,
                   updated_at_utc = ?
             WHERE id = ?
            """,
            (
                postal.strip(),
                response_type,
                responders_text.strip(),
                max(0, response_count),
                max(0, treatment_count),
                max(0, revival_count),
                max(0, bodybag_count),
                str(actor_user_id),
                actor_display_name,
                format_db_dt(now),
                edit_summary.strip(),
                format_db_dt(now),
                service_log_id,
            ),
        )
        self._connection.commit()
        service_log = await self.get_service_log(service_log_id)
        if service_log is None:
            raise RuntimeError("Service log disappeared after edit.")
        return service_log

    async def set_service_log_status(
        self,
        *,
        service_log_id: int,
        status: ServiceLogStatus,
        actor_user_id: int | str,
        actor_display_name: str,
    ) -> ServiceLog:
        if status is ServiceLogStatus.PENDING_PROOF:
            raise ValueError("Cannot manually return a service log to pending proof.")
        now = utc_now()
        self._connection.execute(
            """
            UPDATE service_logs
               SET status = ?,
                   edited_by_user_id = ?,
                   edited_by_display_name = ?,
                   edited_at_utc = ?,
                   edit_summary = ?,
                   updated_at_utc = ?
             WHERE id = ?
            """,
            (
                status.value,
                str(actor_user_id),
                actor_display_name,
                format_db_dt(now),
                f"Marked {status.value}.",
                format_db_dt(now),
                service_log_id,
            ),
        )
        self._connection.commit()
        service_log = await self.get_service_log(service_log_id)
        if service_log is None:
            raise RuntimeError("Service log disappeared after status update.")
        return service_log

    async def add_service_log_proof(
        self,
        *,
        service_log_id: int,
        local_path: str,
        original_filename: str,
        content_type: str | None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO service_log_proofs (
                service_log_id, local_path, original_filename, content_type, created_at_utc
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (service_log_id, local_path, original_filename, content_type, format_db_dt(utc_now())),
        )
        self._connection.commit()

    async def service_log_proofs(self, service_log_id: int) -> list[ServiceLogProof]:
        rows = self._connection.execute(
            "SELECT * FROM service_log_proofs WHERE service_log_id = ? ORDER BY id",
            (service_log_id,),
        ).fetchall()
        return [self._row_to_service_log_proof(row) for row in rows]

    async def add_service_log_message_ref(
        self,
        *,
        service_log_id: int,
        ref_type: str,
        guild_id: int | str | None,
        channel_id: int | str,
        message_id: int | str,
        thread_id: int | str | None = None,
    ) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO service_log_message_refs (
                service_log_id, ref_type, guild_id, channel_id, message_id, thread_id, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                service_log_id,
                ref_type,
                str(guild_id) if guild_id is not None else None,
                str(channel_id),
                str(message_id),
                str(thread_id) if thread_id is not None else None,
                format_db_dt(utc_now()),
            ),
        )
        self._connection.commit()

    async def service_log_message_refs(self, service_log_id: int) -> list[MessageRef]:
        rows = self._connection.execute(
            """
            SELECT
                id,
                service_log_id AS shift_id,
                ref_type,
                guild_id,
                channel_id,
                message_id,
                thread_id
              FROM service_log_message_refs
             WHERE service_log_id = ?
             ORDER BY id
            """,
            (service_log_id,),
        ).fetchall()
        return [self._row_to_message_ref(row) for row in rows]

    async def shift_period_totals(
        self,
        *,
        user_id: int | str,
        timezone_name: str,
        week_start: str,
        now: datetime | None = None,
    ) -> ShiftPeriodTotals:
        current = now or utc_now()
        current_local = localize(current, timezone_name)
        zone = timezone_for(timezone_name)
        day_start = datetime.combine(current_local.date(), time.min, zone).astimezone(current.tzinfo)
        week_start_date = week_start_for(current_local.date(), week_start)
        weekly_start = datetime.combine(week_start_date, time.min, zone).astimezone(current.tzinfo)
        return ShiftPeriodTotals(
            user_id=str(user_id),
            weekly_minutes=self._valid_shift_minutes_between(str(user_id), weekly_start, current),
            daily_minutes=self._valid_shift_minutes_between(str(user_id), day_start, current),
        )

    async def service_period_totals(
        self,
        *,
        user_id: int | str,
        log_kind: ServiceLogKind,
        timezone_name: str,
        week_start: str,
        now: datetime | None = None,
    ) -> ServicePeriodTotals:
        current = now or utc_now()
        current_local = localize(current, timezone_name)
        zone = timezone_for(timezone_name)
        day_start = datetime.combine(current_local.date(), time.min, zone).astimezone(current.tzinfo)
        week_start_date = week_start_for(current_local.date(), week_start)
        weekly_start = datetime.combine(week_start_date, time.min, zone).astimezone(current.tzinfo)
        weekly = self._service_counts_since(str(user_id), log_kind, weekly_start, current)
        daily = self._service_counts_since(str(user_id), log_kind, day_start, current)
        return ServicePeriodTotals(
            user_id=str(user_id),
            log_kind=log_kind,
            weekly_response_count=weekly["responses"],
            daily_response_count=daily["responses"],
            weekly_distress_count=weekly["distress"],
            daily_distress_count=daily["distress"],
            weekly_robbery_count=weekly["robbery"],
            daily_robbery_count=daily["robbery"],
            weekly_treatment_count=weekly["treatments"],
            weekly_revival_count=weekly["revivals"],
            weekly_bodybag_count=weekly["bodybags"],
            daily_treatment_count=daily["treatments"],
            daily_revival_count=daily["revivals"],
            daily_bodybag_count=daily["bodybags"],
        )

    async def start_break(
        self,
        *,
        shift_id: int,
        actor_user_id: int | str,
        actor_display_name: str,
        started_at: datetime | None = None,
    ) -> ShiftBreak:
        shift = await self.get_shift(shift_id)
        if shift is None or shift.status is not ShiftStatus.ACTIVE:
            raise ValueError("You must be clocked in to go 10-7.")
        if shift.current_break_id is not None:
            raise ValueError("You are already 10-7.")
        now = utc_now()
        start = started_at or now
        try:
            self._connection.execute("BEGIN")
            cursor = self._connection.execute(
                """
                INSERT INTO shift_breaks (
                    shift_id, break_start_utc, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?)
                """,
                (shift_id, format_db_dt(start), format_db_dt(now), format_db_dt(now)),
            )
            break_id = int(cursor.lastrowid)
            self._insert_event(
                shift_id,
                str(actor_user_id),
                actor_display_name,
                "break_started",
                {},
                {"break_id": break_id, "break_start_utc": format_db_dt(start)},
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        shift_break = await self.get_break(break_id)
        if shift_break is None:
            raise RuntimeError("Break was not saved.")
        return shift_break

    async def end_break(
        self,
        *,
        shift_id: int,
        actor_user_id: int | str,
        actor_display_name: str,
        break_cap_minutes: int,
        ended_at: datetime | None = None,
    ) -> ShiftBreak:
        shift = await self.get_shift(shift_id)
        if shift is None or shift.current_break_id is None:
            raise ValueError("You are not currently 10-7.")
        end = ended_at or utc_now()
        try:
            self._connection.execute("BEGIN")
            shift_break = self._close_open_break_in_transaction(
                shift,
                end,
                break_cap_minutes=break_cap_minutes,
                auto_clocked_out=False,
            )
            self._connection.execute(
                """
                UPDATE shifts
                   SET updated_at_utc = ?
                 WHERE id = ?
                """,
                (format_db_dt(utc_now()), shift.id),
            )
            self._insert_event(
                shift.id,
                str(actor_user_id),
                actor_display_name,
                "break_ended",
                {"break_id": shift_break.id, "break_start_utc": format_db_dt(shift_break.break_start_utc)},
                {
                    "break_id": shift_break.id,
                    "break_end_utc": format_db_dt(end),
                    "credited_minutes": shift_break.credited_minutes,
                    "unpaid_minutes": shift_break.unpaid_minutes,
                },
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        updated = await self.get_break(shift_break.id)
        if updated is None:
            raise RuntimeError("Break disappeared after update.")
        return updated

    async def get_break(self, break_id: int) -> ShiftBreak | None:
        row = self._connection.execute("SELECT * FROM shift_breaks WHERE id = ?", (break_id,)).fetchone()
        return self._row_to_break(row) if row is not None else None

    async def list_open_breaks(self) -> list[ShiftBreak]:
        rows = self._connection.execute(
            "SELECT * FROM shift_breaks WHERE break_end_utc IS NULL ORDER BY break_start_utc ASC"
        ).fetchall()
        return [self._row_to_break(row) for row in rows]

    async def latest_break_end_for_shift(self, shift_id: int) -> datetime | None:
        row = self._connection.execute(
            """
            SELECT break_end_utc
              FROM shift_breaks
             WHERE shift_id = ?
               AND break_end_utc IS NOT NULL
             ORDER BY break_end_utc DESC
             LIMIT 1
            """,
            (shift_id,),
        ).fetchone()
        return parse_db_dt(str(row["break_end_utc"])) if row is not None and row["break_end_utc"] else None

    async def mark_break_warning_sent(self, break_id: int, sent_at: datetime | None = None) -> None:
        now = sent_at or utc_now()
        self._connection.execute(
            "UPDATE shift_breaks SET warning_sent_at_utc = ?, updated_at_utc = ? WHERE id = ?",
            (format_db_dt(now), format_db_dt(now), break_id),
        )
        self._connection.commit()

    async def mark_break_final_prompt_sent(
        self,
        break_id: int,
        *,
        channel_id: int | str | None,
        message_id: int | str | None,
        sent_at: datetime | None = None,
    ) -> None:
        now = sent_at or utc_now()
        self._connection.execute(
            """
            UPDATE shift_breaks
               SET final_prompt_sent_at_utc = ?,
                   final_prompt_message_channel_id = ?,
                   final_prompt_message_id = ?,
                   updated_at_utc = ?
             WHERE id = ?
            """,
            (
                format_db_dt(now),
                str(channel_id) if channel_id is not None else None,
                str(message_id) if message_id is not None else None,
                format_db_dt(now),
                break_id,
            ),
        )
        self._connection.commit()

    async def add_message_ref(
        self,
        *,
        shift_id: int,
        ref_type: str,
        guild_id: int | str | None,
        channel_id: int | str,
        message_id: int | str,
        thread_id: int | str | None = None,
    ) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO shift_message_refs (
                shift_id, ref_type, guild_id, channel_id, message_id, thread_id, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                shift_id,
                ref_type,
                str(guild_id) if guild_id is not None else None,
                str(channel_id),
                str(message_id),
                str(thread_id) if thread_id is not None else None,
                format_db_dt(utc_now()),
            ),
        )
        self._connection.commit()

    async def message_refs_for_shift(self, shift_id: int) -> list[MessageRef]:
        rows = self._connection.execute(
            "SELECT * FROM shift_message_refs WHERE shift_id = ? ORDER BY id",
            (shift_id,),
        ).fetchall()
        return [self._row_to_message_ref(row) for row in rows]

    async def get_user_log_thread(self, user_id: int | str) -> UserLogThread | None:
        row = self._connection.execute(
            "SELECT * FROM user_log_threads WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
        return self._row_to_user_log_thread(row) if row is not None else None

    async def get_user_forum_thread(self, thread_type: str, user_id: int | str) -> UserForumThread | None:
        row = self._connection.execute(
            "SELECT * FROM user_forum_threads WHERE thread_type = ? AND user_id = ?",
            (thread_type, str(user_id)),
        ).fetchone()
        return self._row_to_user_forum_thread(row) if row is not None else None

    async def upsert_user_forum_thread(
        self,
        *,
        thread_type: str,
        user_id: int | str,
        guild_id: int | str | None,
        forum_channel_id: int | str,
        thread_id: int | str,
        thread_name: str,
        summary_message_id: int | str | None = None,
    ) -> None:
        now = format_db_dt(utc_now())
        self._connection.execute(
            """
            INSERT INTO user_forum_threads (
                thread_type, user_id, guild_id, forum_channel_id, thread_id, thread_name,
                summary_message_id, created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_type, user_id) DO UPDATE SET
                guild_id = excluded.guild_id,
                forum_channel_id = excluded.forum_channel_id,
                thread_id = excluded.thread_id,
                thread_name = excluded.thread_name,
                summary_message_id = COALESCE(excluded.summary_message_id, user_forum_threads.summary_message_id),
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                thread_type,
                str(user_id),
                str(guild_id) if guild_id is not None else None,
                str(forum_channel_id),
                str(thread_id),
                thread_name,
                str(summary_message_id) if summary_message_id is not None else None,
                now,
                now,
            ),
        )
        self._connection.commit()

    async def upsert_user_log_thread(
        self,
        *,
        user_id: int | str,
        guild_id: int | str | None,
        forum_channel_id: int | str,
        thread_id: int | str,
        thread_name: str,
        summary_message_id: int | str | None = None,
    ) -> None:
        now = format_db_dt(utc_now())
        self._connection.execute(
            """
            INSERT INTO user_log_threads (
                user_id, guild_id, forum_channel_id, thread_id, thread_name, summary_message_id, created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                guild_id = excluded.guild_id,
                forum_channel_id = excluded.forum_channel_id,
                thread_id = excluded.thread_id,
                thread_name = excluded.thread_name,
                summary_message_id = COALESCE(excluded.summary_message_id, user_log_threads.summary_message_id),
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                str(user_id),
                str(guild_id) if guild_id is not None else None,
                str(forum_channel_id),
                str(thread_id),
                thread_name,
                str(summary_message_id) if summary_message_id is not None else None,
                now,
                now,
            ),
        )
        self._connection.commit()

    async def remove_message_ref_by_location(self, channel_id: int | str, message_id: int | str) -> None:
        self._connection.execute(
            "DELETE FROM shift_message_refs WHERE channel_id = ? AND message_id = ?",
            (str(channel_id), str(message_id)),
        )
        self._connection.commit()

    async def create_change_request(
        self,
        *,
        shift_id: int,
        requester_user_id: int | str,
        requester_display_name: str,
        request_type: RequestType,
        requested_clock_in_utc: datetime | None,
        requested_clock_out_utc: datetime | None,
    ) -> ChangeRequest:
        now = utc_now()
        try:
            self._connection.execute("BEGIN")
            cursor = self._connection.execute(
                """
                INSERT INTO shift_change_requests (
                    shift_id, requester_user_id, requester_display_name, request_type,
                    requested_clock_in_utc, requested_clock_out_utc, status, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    shift_id,
                    str(requester_user_id),
                    requester_display_name,
                    request_type.value,
                    format_db_dt(requested_clock_in_utc) if requested_clock_in_utc else None,
                    format_db_dt(requested_clock_out_utc) if requested_clock_out_utc else None,
                    format_db_dt(now),
                    format_db_dt(now),
                ),
            )
            request_id = int(cursor.lastrowid)
            self._insert_event(
                shift_id,
                str(requester_user_id),
                requester_display_name,
                "shift_change_requested",
                {},
                {"request_id": request_id, "request_type": request_type.value},
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        request = await self.get_change_request(request_id)
        if request is None:
            raise RuntimeError("Request was not saved.")
        return request

    async def set_request_admin_message(self, request_id: int, channel_id: int | str, message_id: int | str) -> None:
        self._connection.execute(
            """
            UPDATE shift_change_requests
               SET admin_message_channel_id = ?, admin_message_id = ?, updated_at_utc = ?
             WHERE id = ?
            """,
            (str(channel_id), str(message_id), format_db_dt(utc_now()), request_id),
        )
        self._connection.commit()

    async def get_change_request(self, request_id: int) -> ChangeRequest | None:
        row = self._connection.execute(
            "SELECT * FROM shift_change_requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        return self._row_to_request(row) if row is not None else None

    async def pending_request_for_shift(self, shift_id: int) -> ChangeRequest | None:
        row = self._connection.execute(
            "SELECT * FROM shift_change_requests WHERE shift_id = ? AND status = 'pending'",
            (shift_id,),
        ).fetchone()
        return self._row_to_request(row) if row is not None else None

    async def decide_request(
        self,
        *,
        request_id: int,
        status: RequestStatus,
        actor_user_id: int | str,
        actor_display_name: str,
    ) -> ChangeRequest:
        if status is RequestStatus.PENDING:
            raise ValueError("Request decision must be approved or denied.")
        request = await self.get_change_request(request_id)
        if request is None:
            raise ValueError("Request not found.")
        now = utc_now()
        try:
            self._connection.execute("BEGIN")
            self._connection.execute(
                """
                UPDATE shift_change_requests
                   SET status = ?,
                       decided_by_user_id = ?,
                       decided_by_display_name = ?,
                       decided_at_utc = ?,
                       updated_at_utc = ?
                 WHERE id = ?
                """,
                (
                    status.value,
                    str(actor_user_id),
                    actor_display_name,
                    format_db_dt(now),
                    format_db_dt(now),
                    request_id,
                ),
            )
            self._insert_event(
                request.shift_id,
                str(actor_user_id),
                actor_display_name,
                f"shift_change_request_{status.value}",
                {"request_id": request_id},
                {"request_id": request_id, "status": status.value},
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        updated = await self.get_change_request(request_id)
        if updated is None:
            raise RuntimeError("Request disappeared after decision.")
        return updated

    def _insert_event(
        self,
        shift_id: int | None,
        actor_user_id: str,
        actor_display_name: str,
        action: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO shift_events (
                shift_id, actor_user_id, actor_display_name, action, before_json, after_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                shift_id,
                actor_user_id,
                actor_display_name,
                action,
                json.dumps(before, ensure_ascii=False, sort_keys=True),
                json.dumps(after, ensure_ascii=False, sort_keys=True),
                format_db_dt(utc_now()),
            ),
        )

    def _close_open_break_in_transaction(
        self,
        shift: Shift,
        end: datetime,
        *,
        break_cap_minutes: int,
        auto_clocked_out: bool,
    ) -> ShiftBreak | None:
        if shift.current_break_id is None:
            return None
        row = self._connection.execute(
            "SELECT * FROM shift_breaks WHERE id = ? AND break_end_utc IS NULL",
            (shift.current_break_id,),
        ).fetchone()
        if row is None:
            return None
        shift_break = self._row_to_break(row)
        now = utc_now()
        self._connection.execute(
            """
            UPDATE shift_breaks
               SET break_end_utc = ?,
                   credited_minutes = 0,
                   unpaid_minutes = 0,
                   auto_clocked_out = ?,
                   updated_at_utc = ?
             WHERE id = ?
            """,
            (format_db_dt(end), 1 if auto_clocked_out else 0, format_db_dt(now), shift_break.id),
        )
        return ShiftBreak(
            id=shift_break.id,
            shift_id=shift_break.shift_id,
            break_start_utc=shift_break.break_start_utc,
            break_end_utc=end,
            credited_minutes=0,
            unpaid_minutes=0,
            warning_sent_at_utc=shift_break.warning_sent_at_utc,
            final_prompt_sent_at_utc=shift_break.final_prompt_sent_at_utc,
            final_prompt_message_channel_id=shift_break.final_prompt_message_channel_id,
            final_prompt_message_id=shift_break.final_prompt_message_id,
            auto_clocked_out=auto_clocked_out,
            created_at_utc=shift_break.created_at_utc,
            updated_at_utc=now,
        )

    def _valid_shift_minutes_between(self, user_id: str, start: datetime, end: datetime) -> int:
        rows = self._connection.execute(
            """
            SELECT clock_in_utc, clock_out_utc
              FROM shifts
             WHERE user_id = ?
               AND status = 'valid'
               AND clock_out_utc IS NOT NULL
            """,
            (user_id,),
        ).fetchall()
        total = 0
        for row in rows:
            shift_start = parse_db_dt(str(row["clock_in_utc"]))
            shift_end = parse_db_dt(str(row["clock_out_utc"]))
            overlap_start = max(shift_start, start)
            overlap_end = min(shift_end, end)
            total += rendered_minutes(overlap_start, overlap_end)
        return total

    def _service_counts_since(self, user_id: str, log_kind: ServiceLogKind, start: datetime, end: datetime) -> dict[str, int]:
        row = self._connection.execute(
            """
            SELECT
                COALESCE(SUM(response_count), 0) AS responses,
                COALESCE(SUM(CASE WHEN response_type = 'DISTRESS' THEN response_count ELSE 0 END), 0) AS distress,
                COALESCE(SUM(CASE WHEN response_type = 'ROBBERY' THEN response_count ELSE 0 END), 0) AS robbery,
                COALESCE(SUM(treatment_count), 0) AS treatments,
                COALESCE(SUM(revival_count), 0) AS revivals,
                COALESCE(SUM(bodybag_count), 0) AS bodybags
              FROM service_logs
             WHERE user_id = ?
               AND log_kind = ?
               AND status = 'valid'
               AND created_at_utc >= ?
               AND created_at_utc <= ?
            """,
            (user_id, log_kind.value, format_db_dt(start), format_db_dt(end)),
        ).fetchone()
        return {
            "responses": int(row["responses"] or 0),
            "distress": int(row["distress"] or 0),
            "robbery": int(row["robbery"] or 0),
            "treatments": int(row["treatments"] or 0),
            "revivals": int(row["revivals"] or 0),
            "bodybags": int(row["bodybags"] or 0),
        }

    async def _payout_employees(self, user_ids: list[int | str] | None) -> list[Employee]:
        if user_ids is None:
            return await self.list_employees()
        ordered = list(dict.fromkeys(str(user_id) for user_id in user_ids))
        if not ordered:
            return []
        placeholders = ",".join("?" for _ in ordered)
        rows = self._connection.execute(
            f"""
            SELECT user_id, display_name, rank, active
              FROM employees
             WHERE active = 1
               AND user_id IN ({placeholders})
            """,
            tuple(ordered),
        ).fetchall()
        by_id = {str(row["user_id"]): self._row_to_employee(row) for row in rows}
        return [by_id[user_id] for user_id in ordered if user_id in by_id]

    def _unaccounted_shift_minutes(self, user_id: str, cutoff_end: datetime) -> int:
        row = self._connection.execute(
            """
            SELECT COALESCE(SUM(rendered_minutes), 0) AS minutes
              FROM shifts
             WHERE user_id = ?
               AND status = 'valid'
               AND clock_out_utc IS NOT NULL
               AND payout_snapshot_id IS NULL
               AND clock_out_utc <= ?
            """,
            (user_id, format_db_dt(cutoff_end)),
        ).fetchone()
        return int(row["minutes"] or 0)

    def _unaccounted_response_count(self, user_id: str, cutoff_end: datetime) -> int:
        row = self._connection.execute(
            """
            SELECT COALESCE(SUM(response_count), 0) AS total
              FROM service_logs
             WHERE user_id = ?
               AND log_kind = 'response'
               AND status = 'valid'
               AND payout_snapshot_id IS NULL
               AND created_at_utc <= ?
            """,
            (user_id, format_db_dt(cutoff_end)),
        ).fetchone()
        return int(row["total"] or 0)

    def _unaccounted_vital_count(self, user_id: str, cutoff_end: datetime) -> int:
        row = self._connection.execute(
            """
            SELECT COALESCE(SUM(treatment_count + revival_count + bodybag_count), 0) AS total
              FROM service_logs
             WHERE user_id = ?
               AND log_kind = 'vital'
               AND status = 'valid'
               AND payout_snapshot_id IS NULL
               AND created_at_utc <= ?
            """,
            (user_id, format_db_dt(cutoff_end)),
        ).fetchone()
        return int(row["total"] or 0)

    def _mark_user_logs_accounted(self, user_id: str, snapshot_id: int, cutoff_end: datetime) -> None:
        cutoff = format_db_dt(cutoff_end)
        self._connection.execute(
            """
            UPDATE shifts
               SET payout_snapshot_id = ?
             WHERE user_id = ?
               AND status = 'valid'
               AND clock_out_utc IS NOT NULL
               AND payout_snapshot_id IS NULL
               AND clock_out_utc <= ?
            """,
            (snapshot_id, user_id, cutoff),
        )
        self._connection.execute(
            """
            UPDATE service_logs
               SET payout_snapshot_id = ?
             WHERE user_id = ?
               AND status = 'valid'
               AND payout_snapshot_id IS NULL
               AND created_at_utc <= ?
            """,
            (snapshot_id, user_id, cutoff),
        )

    def _credited_break_minutes_before(self, shift_id: int, break_id: int) -> int:
        row = self._connection.execute(
            """
            SELECT COALESCE(SUM(credited_minutes), 0) AS total
              FROM shift_breaks
             WHERE shift_id = ?
               AND id != ?
            """,
            (shift_id, break_id),
        ).fetchone()
        return int(row["total"] or 0)

    def _break_totals(self, shift_id: int) -> dict[str, int]:
        row = self._connection.execute(
            """
            SELECT
                COALESCE(SUM(credited_minutes), 0) AS credited,
                COALESCE(SUM(unpaid_minutes), 0) AS unpaid
              FROM shift_breaks
             WHERE shift_id = ?
            """,
            (shift_id,),
        ).fetchone()
        return {"credited": int(row["credited"] or 0), "unpaid": int(row["unpaid"] or 0)}

    @staticmethod
    def _effective_shift_minutes(start: datetime, end: datetime, unpaid_break_minutes: int) -> int:
        return max(0, rendered_minutes(start, end) - max(0, unpaid_break_minutes))

    @staticmethod
    def _shift_snapshot(shift: Shift) -> dict[str, Any]:
        return {
            "id": shift.id,
            "user_id": shift.user_id,
            "clock_in_utc": format_db_dt(shift.clock_in_utc),
            "clock_out_utc": format_db_dt(shift.clock_out_utc) if shift.clock_out_utc else None,
            "rendered_minutes": shift.rendered_minutes,
            "status": shift.status.value,
        }

    @staticmethod
    def _row_to_shift(row: sqlite3.Row) -> Shift:
        return Shift(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            guild_id=str(row["guild_id"]) if row["guild_id"] is not None else None,
            clock_in_utc=parse_db_dt(str(row["clock_in_utc"])),
            clock_out_utc=parse_db_dt(str(row["clock_out_utc"])) if row["clock_out_utc"] else None,
            rendered_minutes=int(row["rendered_minutes"]),
            break_minutes=int(row["break_minutes"]),
            unpaid_break_minutes=int(row["unpaid_break_minutes"]),
            status=ShiftStatus(str(row["status"])),
            current_break_id=int(row["current_break_id"]) if "current_break_id" in row.keys() and row["current_break_id"] is not None else None,
            current_break_start_utc=(
                parse_db_dt(str(row["current_break_start_utc"]))
                if "current_break_start_utc" in row.keys() and row["current_break_start_utc"] is not None
                else None
            ),
            edited_by_user_id=str(row["edited_by_user_id"]) if row["edited_by_user_id"] is not None else None,
            edited_by_display_name=str(row["edited_by_display_name"]) if row["edited_by_display_name"] is not None else None,
            edited_at_utc=parse_db_dt(str(row["edited_at_utc"])) if row["edited_at_utc"] else None,
            edit_summary=str(row["edit_summary"]),
            created_at_utc=parse_db_dt(str(row["created_at_utc"])),
            updated_at_utc=parse_db_dt(str(row["updated_at_utc"])),
        )

    @staticmethod
    def _row_to_break(row: sqlite3.Row) -> ShiftBreak:
        return ShiftBreak(
            id=int(row["id"]),
            shift_id=int(row["shift_id"]),
            break_start_utc=parse_db_dt(str(row["break_start_utc"])),
            break_end_utc=parse_db_dt(str(row["break_end_utc"])) if row["break_end_utc"] else None,
            credited_minutes=int(row["credited_minutes"]),
            unpaid_minutes=int(row["unpaid_minutes"]),
            warning_sent_at_utc=parse_db_dt(str(row["warning_sent_at_utc"])) if row["warning_sent_at_utc"] else None,
            final_prompt_sent_at_utc=(
                parse_db_dt(str(row["final_prompt_sent_at_utc"])) if row["final_prompt_sent_at_utc"] else None
            ),
            final_prompt_message_channel_id=(
                str(row["final_prompt_message_channel_id"]) if row["final_prompt_message_channel_id"] else None
            ),
            final_prompt_message_id=str(row["final_prompt_message_id"]) if row["final_prompt_message_id"] else None,
            auto_clocked_out=bool(row["auto_clocked_out"]),
            created_at_utc=parse_db_dt(str(row["created_at_utc"])),
            updated_at_utc=parse_db_dt(str(row["updated_at_utc"])),
        )

    @staticmethod
    def _row_to_message_ref(row: sqlite3.Row) -> MessageRef:
        return MessageRef(
            id=int(row["id"]),
            shift_id=int(row["shift_id"]),
            ref_type=str(row["ref_type"]),
            guild_id=str(row["guild_id"]) if row["guild_id"] is not None else None,
            channel_id=str(row["channel_id"]),
            message_id=str(row["message_id"]),
            thread_id=str(row["thread_id"]) if row["thread_id"] is not None else None,
        )

    @staticmethod
    def _row_to_user_log_thread(row: sqlite3.Row) -> UserLogThread:
        return UserLogThread(
            user_id=str(row["user_id"]),
            guild_id=str(row["guild_id"]) if row["guild_id"] is not None else None,
            forum_channel_id=str(row["forum_channel_id"]),
            thread_id=str(row["thread_id"]),
            thread_name=str(row["thread_name"]),
            summary_message_id=str(row["summary_message_id"]) if row["summary_message_id"] is not None else None,
        )

    @staticmethod
    def _row_to_user_forum_thread(row: sqlite3.Row) -> UserForumThread:
        return UserForumThread(
            thread_type=str(row["thread_type"]),
            user_id=str(row["user_id"]),
            guild_id=str(row["guild_id"]) if row["guild_id"] is not None else None,
            forum_channel_id=str(row["forum_channel_id"]),
            thread_id=str(row["thread_id"]),
            thread_name=str(row["thread_name"]),
            summary_message_id=str(row["summary_message_id"]) if row["summary_message_id"] is not None else None,
        )

    @staticmethod
    def _row_to_service_log(row: sqlite3.Row) -> ServiceLog:
        return ServiceLog(
            id=int(row["id"]),
            log_kind=ServiceLogKind(str(row["log_kind"])),
            user_id=str(row["user_id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            guild_id=str(row["guild_id"]) if row["guild_id"] is not None else None,
            postal=str(row["postal"]),
            response_type=str(row["response_type"]) if row["response_type"] is not None else None,
            responders_text=str(row["responders_text"]),
            response_count=int(row["response_count"]),
            treatment_count=int(row["treatment_count"]),
            revival_count=int(row["revival_count"]),
            bodybag_count=int(row["bodybag_count"]),
            status=ServiceLogStatus(str(row["status"])),
            edited_by_user_id=str(row["edited_by_user_id"]) if row["edited_by_user_id"] is not None else None,
            edited_by_display_name=str(row["edited_by_display_name"]) if row["edited_by_display_name"] is not None else None,
            edited_at_utc=parse_db_dt(str(row["edited_at_utc"])) if row["edited_at_utc"] else None,
            edit_summary=str(row["edit_summary"]),
            created_at_utc=parse_db_dt(str(row["created_at_utc"])),
            updated_at_utc=parse_db_dt(str(row["updated_at_utc"])),
            proof_received_at_utc=parse_db_dt(str(row["proof_received_at_utc"])) if row["proof_received_at_utc"] else None,
            proof_prompt_channel_id=str(row["proof_prompt_channel_id"]) if row["proof_prompt_channel_id"] is not None else None,
            proof_prompt_message_id=str(row["proof_prompt_message_id"]) if row["proof_prompt_message_id"] is not None else None,
        )

    @staticmethod
    def _row_to_service_log_proof(row: sqlite3.Row) -> ServiceLogProof:
        return ServiceLogProof(
            id=int(row["id"]),
            service_log_id=int(row["service_log_id"]),
            local_path=str(row["local_path"]),
            original_filename=str(row["original_filename"]),
            content_type=str(row["content_type"]) if row["content_type"] is not None else None,
        )

    @staticmethod
    def _row_to_employee(row: sqlite3.Row) -> Employee:
        return Employee(
            user_id=str(row["user_id"]),
            display_name=str(row["display_name"]),
            rank=str(row["rank"]),
            active=bool(row["active"]),
        )

    @staticmethod
    def _row_to_payout_snapshot(row: sqlite3.Row) -> PayoutSnapshot:
        return PayoutSnapshot(
            id=int(row["id"]),
            snapshot_type=str(row["snapshot_type"]),
            status=str(row["status"]),
            created_by_user_id=str(row["created_by_user_id"]),
            created_by_display_name=str(row["created_by_display_name"]),
            created_at_utc=parse_db_dt(str(row["created_at_utc"])),
            cutoff_start_utc=parse_db_dt(str(row["cutoff_start_utc"])),
            cutoff_end_utc=parse_db_dt(str(row["cutoff_end_utc"])),
            include_skipped=bool(row["include_skipped"]),
        )

    @staticmethod
    def _row_to_payout_snapshot_item(row: sqlite3.Row) -> PayoutSnapshotItem:
        return PayoutSnapshotItem(
            id=int(row["id"]),
            snapshot_id=int(row["snapshot_id"]),
            user_id=str(row["user_id"]),
            rank=str(row["rank"]),
            duty_minutes=int(row["duty_minutes"]),
            response_count=int(row["response_count"]),
            vital_count=int(row["vital_count"]),
            payout_total=int(row["payout_total"]),
            skipped=bool(row["skipped"]),
        )

    @staticmethod
    def _row_to_request(row: sqlite3.Row) -> ChangeRequest:
        return ChangeRequest(
            id=int(row["id"]),
            shift_id=int(row["shift_id"]),
            requester_user_id=str(row["requester_user_id"]),
            requester_display_name=str(row["requester_display_name"]),
            request_type=RequestType(str(row["request_type"])),
            requested_clock_in_utc=(
                parse_db_dt(str(row["requested_clock_in_utc"])) if row["requested_clock_in_utc"] else None
            ),
            requested_clock_out_utc=(
                parse_db_dt(str(row["requested_clock_out_utc"])) if row["requested_clock_out_utc"] else None
            ),
            status=RequestStatus(str(row["status"])),
            admin_message_channel_id=(
                str(row["admin_message_channel_id"]) if row["admin_message_channel_id"] is not None else None
            ),
            admin_message_id=str(row["admin_message_id"]) if row["admin_message_id"] is not None else None,
            decided_by_user_id=str(row["decided_by_user_id"]) if row["decided_by_user_id"] is not None else None,
            decided_by_display_name=(
                str(row["decided_by_display_name"]) if row["decided_by_display_name"] is not None else None
            ),
            decided_at_utc=parse_db_dt(str(row["decided_at_utc"])) if row["decided_at_utc"] else None,
            created_at_utc=parse_db_dt(str(row["created_at_utc"])),
            updated_at_utc=parse_db_dt(str(row["updated_at_utc"])),
        )
