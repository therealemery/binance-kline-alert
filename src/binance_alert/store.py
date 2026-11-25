from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .formatter import infer_display_name
from .models import Kline, PatternSignal, WatchedSymbol
from .rule_engine import CustomRule


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """SQLite persistence for watched symbols, alert deduplication, history, and bot metadata."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS watched_symbols (
                    symbol TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    added_by TEXT NOT NULL DEFAULT 'config',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS alert_state (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    last_open_time INTEGER NOT NULL,
                    last_pattern TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, interval)
                );

                CREATE TABLE IF NOT EXISTS alert_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    open_time INTEGER NOT NULL,
                    prev_open TEXT NOT NULL,
                    prev_close TEXT NOT NULL,
                    curr_open TEXT NOT NULL,
                    curr_close TEXT NOT NULL,
                    change_pct REAL NOT NULL,
                    volume_ratio REAL,
                    delivered_webhook INTEGER NOT NULL DEFAULT 0,
                    delivered_aibot INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS custom_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    expression TEXT NOT NULL,
                    lookback INTEGER NOT NULL DEFAULT 20,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    source_prompt TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT 'user',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rule_alert_state (
                    rule_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    last_open_time INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (rule_id, symbol, interval)
                );
                """
            )

    def seed_symbols_if_empty(self, symbols: list[str]) -> int:
        """Seed initial symbols from config only when the database has never been seeded."""
        if self.get_meta("seeded_initial_symbols") == "1":
            return 0
        added = 0
        now = _utc_now_iso()
        with self._connect() as conn:
            for raw in symbols:
                sym = raw.strip().upper()
                if not sym:
                    continue
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO watched_symbols (symbol, display_name, enabled, added_by, created_at)
                    VALUES (?, ?, 1, 'config', ?)
                    """,
                    (sym, infer_display_name(sym), now),
                )
                added += cur.rowcount
            conn.execute(
                """
                INSERT OR REPLACE INTO bot_meta (key, value, updated_at)
                VALUES ('seeded_initial_symbols', '1', ?)
                """,
                (now,),
            )
        return added

    def list_symbols(self, *, enabled_only: bool = True) -> list[WatchedSymbol]:
        query = "SELECT symbol, display_name, enabled, added_by, created_at FROM watched_symbols"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at ASC, symbol ASC"
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
        return [
            WatchedSymbol(
                symbol=row["symbol"],
                display_name=row["display_name"],
                enabled=bool(row["enabled"]),
                added_by=row["added_by"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def add_symbol(self, symbol: str, *, display_name: str | None = None, added_by: str = "user") -> bool:
        sym = symbol.strip().upper()
        name = display_name or infer_display_name(sym)
        now = _utc_now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT enabled FROM watched_symbols WHERE symbol = ?", (sym,)
            ).fetchone()
            if existing and existing["enabled"] == 1:
                return False
            conn.execute(
                """
                INSERT INTO watched_symbols (symbol, display_name, enabled, added_by, created_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    display_name = excluded.display_name,
                    enabled = 1,
                    added_by = excluded.added_by
                """,
                (sym, name, added_by, now),
            )
        return True

    def remove_symbol(self, symbol: str) -> str | None:
        """Remove a watched symbol; matches raw input or USDT/BUSDT variants."""
        sym = symbol.strip().upper()
        candidates = [sym, f"{sym}USDT", f"{sym}BUSDT"]
        with self._connect() as conn:
            for cand in candidates:
                row = conn.execute(
                    "SELECT symbol FROM watched_symbols WHERE symbol = ? AND enabled = 1",
                    (cand,),
                ).fetchone()
                if row:
                    conn.execute(
                        "DELETE FROM watched_symbols WHERE symbol = ?",
                        (row["symbol"],),
                    )
                    return str(row["symbol"])
        return None

    def get_last_alert_open_time(self, symbol: str, interval: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_open_time FROM alert_state WHERE symbol = ? AND interval = ?",
                (symbol.upper(), interval),
            ).fetchone()
        return int(row["last_open_time"]) if row else None

    def record_alert(
        self,
        signal: PatternSignal,
        *,
        delivered_webhook: bool,
        delivered_aibot: bool,
    ) -> None:
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_state (symbol, interval, last_open_time, last_pattern, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval) DO UPDATE SET
                    last_open_time = excluded.last_open_time,
                    last_pattern = excluded.last_pattern,
                    updated_at = excluded.updated_at
                """,
                (signal.symbol, signal.interval, signal.open_time, signal.pattern.value, now),
            )
            conn.execute(
                """
                INSERT INTO alert_history (
                    symbol, interval, pattern, open_time,
                    prev_open, prev_close, curr_open, curr_close,
                    change_pct, volume_ratio,
                    delivered_webhook, delivered_aibot, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.symbol,
                    signal.interval,
                    signal.pattern.value,
                    signal.open_time,
                    str(signal.prev_kline.open),
                    str(signal.prev_kline.close),
                    str(signal.curr_kline.open),
                    str(signal.curr_kline.close),
                    float(signal.curr_kline.change_pct),
                    float(signal.volume_ratio) if signal.volume_ratio is not None else None,
                    int(delivered_webhook),
                    int(delivered_aibot),
                    now,
                ),
            )

    def get_recent_history(self, limit: int = 5) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 20))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, interval, pattern, open_time, curr_open, curr_close,
                       change_pct, volume_ratio, created_at
                FROM alert_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_total_alert_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM alert_history").fetchone()
        return int(row["cnt"]) if row else 0

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM bot_meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO bot_meta (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, value, _utc_now_iso()),
            )

    def is_paused(self) -> bool:
        return self.get_meta("paused", "0") == "1"

    def set_paused(self, paused: bool) -> None:
        self.set_meta("paused", "1" if paused else "0")

    def register_chat_id(self, chat_id: str) -> None:
        if not chat_id:
            return
        chat_ids = self.get_chat_ids()
        if chat_id not in chat_ids:
            chat_ids.append(chat_id)
            self.set_meta("active_chat_ids", json.dumps(chat_ids))

    def get_chat_ids(self) -> list[str]:
        raw = self.get_meta("active_chat_ids")
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return [str(x) for x in data if x]
        except json.JSONDecodeError:
            return []

    # ---- Custom LLM-compiled Rules ----

    def add_rule(self, rule: CustomRule) -> int:
        now = _utc_now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO custom_rules (
                    name, description, symbols_json, interval, expression,
                    lookback, enabled, source_prompt, created_by, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.name,
                    rule.description,
                    json.dumps(rule.symbols, ensure_ascii=False),
                    rule.interval,
                    rule.expression,
                    rule.lookback,
                    int(rule.enabled),
                    rule.source_prompt,
                    rule.created_by,
                    now,
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def list_rules(self, *, enabled_only: bool = False) -> list[CustomRule]:
        query = (
            "SELECT id, name, description, symbols_json, interval, expression, "
            "lookback, enabled, source_prompt, created_by, created_at FROM custom_rules"
        )
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY id ASC"
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
        result: list[CustomRule] = []
        for row in rows:
            try:
                syms = json.loads(row["symbols_json"])
            except json.JSONDecodeError:
                syms = ["*"]
            result.append(
                CustomRule(
                    id=int(row["id"]),
                    name=row["name"],
                    description=row["description"],
                    symbols=[str(s) for s in syms],
                    interval=row["interval"],
                    expression=row["expression"],
                    lookback=int(row["lookback"]),
                    enabled=bool(row["enabled"]),
                    source_prompt=row["source_prompt"],
                    created_by=row["created_by"],
                    created_at=row["created_at"],
                )
            )
        return result

    def delete_rule(self, rule_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM custom_rules WHERE id = ?", (rule_id,))
            conn.execute("DELETE FROM rule_alert_state WHERE rule_id = ?", (rule_id,))
            return cur.rowcount > 0

    def toggle_rule(self, rule_id: int) -> bool | None:
        """Toggle a rule's enabled flag; returns new enabled bool or None if not found."""
        with self._connect() as conn:
            row = conn.execute("SELECT enabled FROM custom_rules WHERE id = ?", (rule_id,)).fetchone()
            if not row:
                return None
            new_state = not bool(row["enabled"])
            conn.execute(
                "UPDATE custom_rules SET enabled = ? WHERE id = ?",
                (int(new_state), rule_id),
            )
            return new_state

    def set_rule_enabled(self, rule_id: int, enabled: bool) -> bool | None:
        """Explicitly enable or disable a rule; returns new enabled bool or None if not found."""
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM custom_rules WHERE id = ?", (rule_id,)).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE custom_rules SET enabled = ? WHERE id = ?",
                (int(enabled), rule_id),
            )
            return enabled

    def get_last_rule_alert_open_time(self, rule_id: int, symbol: str, interval: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT last_open_time FROM rule_alert_state
                WHERE rule_id = ? AND symbol = ? AND interval = ?
                """,
                (rule_id, symbol.upper(), interval),
            ).fetchone()
        return int(row["last_open_time"]) if row else None

    def record_rule_alert(
        self,
        *,
        rule: CustomRule,
        symbol: str,
        curr: Kline,
        prev: Kline | None,
        delivered_webhook: bool,
        delivered_aibot: bool,
    ) -> None:
        assert rule.id is not None
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rule_alert_state (rule_id, symbol, interval, last_open_time, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(rule_id, symbol, interval) DO UPDATE SET
                    last_open_time = excluded.last_open_time,
                    updated_at = excluded.updated_at
                """,
                (rule.id, symbol.upper(), rule.interval, curr.open_time, now),
            )
            conn.execute(
                """
                INSERT INTO alert_history (
                    symbol, interval, pattern, open_time,
                    prev_open, prev_close, curr_open, curr_close,
                    change_pct, volume_ratio,
                    delivered_webhook, delivered_aibot, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol.upper(),
                    rule.interval,
                    f"rule:{rule.id}:{rule.name}",
                    curr.open_time,
                    str(prev.open) if prev else "",
                    str(prev.close) if prev else "",
                    str(curr.open),
                    str(curr.close),
                    float(curr.change_pct),
                    None,
                    int(delivered_webhook),
                    int(delivered_aibot),
                    now,
                ),
            )
