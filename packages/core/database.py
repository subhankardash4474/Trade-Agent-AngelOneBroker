"""
Database Module
SQLite-backed local storage for tick data, candle history, trade logs,
and model artifacts. Provides fast lookups for backtesting and live use.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import pytz
from loguru import logger

# Trading session timezone. All event timestamps written to the DB are
# timezone-aware IST so they compare correctly with `entry_time` values
# written by Portfolio (which uses `datetime.now(IST)`). Naive timestamps
# silently break `_restore_positions` cash resolution on a UTC host
# (e.g. AWS Linux): on a Windows-IST laptop the naive ts is accidentally
# correct; on Linux-UTC it's 5h30m behind, the comparison fails, and
# the legacy `min(cash_after)` fallback ships a stale cash value.
IST = pytz.timezone("Asia/Kolkata")


# ── JSON serialization helpers (2026-05-06) ──────────────────────────
# Strategies (especially ML ones) emit numpy scalar types embedded in
# dicts that flow into the DB. json.dumps refuses every numpy type by
# default, including:
#   - numpy.float16/32/64
#   - numpy.int8/16/32/64
#   - numpy.bool_
#   - numpy.ndarray (we coerce to list of floats)
# Without these helpers, a single XGBoost-only signal would silently
# kill the open_position write — caught live with UNITDSPR + CGPOWER.

def _json_default(o: Any) -> Any:
    """json.dumps `default=` hook that handles numpy scalars + arrays."""
    # numpy.generic is the base class for ALL numpy scalar types
    if hasattr(o, "item") and callable(getattr(o, "item")):
        try:
            return o.item()  # converts np.float32(1.0) → 1.0 (Python float)
        except Exception:
            pass
    if hasattr(o, "tolist") and callable(getattr(o, "tolist")):
        try:
            return o.tolist()  # ndarrays → nested lists of plain floats
        except Exception:
            pass
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _coerce_json_safe(obj: Any) -> Any:
    """Recursively walk a dict/list and pre-coerce numpy types to plain
    Python equivalents. We do this proactively so the resulting JSON
    blob round-trips cleanly via standard `json.loads` (no custom hook
    needed on the read path)."""
    if isinstance(obj, dict):
        return {str(k): _coerce_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_coerce_json_safe(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes, dict, list, tuple)):
        # numpy scalar
        try:
            return obj.item()
        except Exception:
            return obj
    return obj


class Database:
    """
    Local SQLite database for persisting market data and trade records.
    Uses WAL mode for concurrent read/write access from multiple threads.
    """

    def __init__(self, db_path: str = "data/trading_agent.db"):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._db_path = db_path
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS candles (
                    symbol     TEXT NOT NULL,
                    timeframe  TEXT NOT NULL,
                    timestamp  TEXT NOT NULL,
                    open       REAL,
                    high       REAL,
                    low        REAL,
                    close      REAL,
                    volume     REAL,
                    PRIMARY KEY (symbol, timeframe, timestamp)
                );

                CREATE TABLE IF NOT EXISTS ticks (
                    symbol     TEXT NOT NULL,
                    timestamp  TEXT NOT NULL,
                    ltp        REAL,
                    volume     REAL,
                    bid        REAL,
                    ask        REAL
                );

                CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts
                    ON ticks(symbol, timestamp);

                CREATE TABLE IF NOT EXISTS trades (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol      TEXT NOT NULL,
                    side        TEXT NOT NULL,
                    entry_price REAL,
                    exit_price  REAL,
                    quantity    INTEGER,
                    entry_time  TEXT,
                    exit_time   TEXT,
                    pnl         REAL,
                    pnl_pct     REAL,
                    strategy    TEXT,
                    exit_reason TEXT,
                    commission  REAL,
                    slippage    REAL,
                    market_context TEXT
                );

                CREATE TABLE IF NOT EXISTS equity_curve (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp  TEXT NOT NULL,
                    equity     REAL,
                    cash       REAL,
                    positions  INTEGER
                );

                CREATE TABLE IF NOT EXISTS strategy_scores (
                    strategy    TEXT PRIMARY KEY,
                    total_trades INTEGER DEFAULT 0,
                    wins        INTEGER DEFAULT 0,
                    losses      INTEGER DEFAULT 0,
                    total_pnl   REAL DEFAULT 0,
                    avg_pnl     REAL DEFAULT 0,
                    win_rate    REAL DEFAULT 0,
                    profit_factor REAL DEFAULT 0,
                    sharpe      REAL DEFAULT 0,
                    learned_weight REAL DEFAULT 1.0,
                    updated_at  TEXT
                );

                CREATE TABLE IF NOT EXISTS trade_patterns (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy    TEXT NOT NULL,
                    symbol      TEXT NOT NULL,
                    entry_time  TEXT,
                    rsi         REAL,
                    atr_pct     REAL,
                    volume_ratio REAL,
                    hour_of_day INTEGER,
                    day_of_week INTEGER,
                    market_trend INTEGER,
                    pnl         REAL,
                    pnl_pct     REAL,
                    outcome     TEXT,
                    exit_reason TEXT,
                    holding_minutes REAL,
                    pnl_bucket  TEXT,
                    regime      TEXT,
                    created_at  TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_patterns_outcome
                    ON trade_patterns(outcome, strategy);

                CREATE TABLE IF NOT EXISTS open_positions (
                    symbol      TEXT PRIMARY KEY,
                    side        TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    quantity    INTEGER NOT NULL,
                    entry_time  TEXT NOT NULL,
                    stop_loss   REAL,
                    take_profit REAL,
                    strategy    TEXT,
                    order_id    TEXT,
                    cash_after  REAL,
                    regime      TEXT,
                    contributing_strategies TEXT
                );

                CREATE TABLE IF NOT EXISTS regime_weights (
                    strategy    TEXT NOT NULL,
                    regime      TEXT NOT NULL,
                    weight      REAL DEFAULT 1.0,
                    trades      INTEGER DEFAULT 0,
                    wins        INTEGER DEFAULT 0,
                    total_pnl   REAL DEFAULT 0,
                    sharpe      REAL DEFAULT 0,
                    updated_at  TEXT,
                    PRIMARY KEY (strategy, regime)
                );

                -- Order ledger (audit trail). Every placed order lands here,
                -- including partial-fill and failed attempts. Regulators /
                -- SEBI audits require a complete order history reconciliable
                -- with broker contracts.
                CREATE TABLE IF NOT EXISTS orders (
                    order_id         TEXT PRIMARY KEY,
                    timestamp        TEXT NOT NULL,
                    symbol           TEXT NOT NULL,
                    transaction_type TEXT,
                    order_type       TEXT,
                    quantity         INTEGER,
                    filled_quantity  INTEGER,
                    requested_price  REAL,
                    filled_price     REAL,
                    slippage         REAL,
                    status           TEXT,
                    mode             TEXT,
                    tag              TEXT,
                    exchange         TEXT,
                    stop_loss_price  REAL,
                    target_price     REAL
                );

                CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(timestamp);
                CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
            """)

            # Backward-compatible migrations: add columns if they don't exist yet.
            self._ensure_column(conn, "trade_patterns", "exit_reason", "TEXT")
            self._ensure_column(conn, "trade_patterns", "holding_minutes", "REAL")
            self._ensure_column(conn, "trade_patterns", "pnl_bucket", "TEXT")
            self._ensure_column(conn, "trade_patterns", "regime", "TEXT")
            self._ensure_column(conn, "open_positions", "regime", "TEXT")
            self._ensure_column(conn, "open_positions", "contributing_strategies", "TEXT")
            self._ensure_column(conn, "trades", "regime", "TEXT")
            self._ensure_column(conn, "trades", "holding_minutes", "REAL")

            # Create indexes that depend on the migrated columns
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_patterns_regime "
                    "ON trade_patterns(regime, strategy)"
                )
            except Exception as e:
                logger.debug(f"idx_patterns_regime skipped: {e}")

            logger.debug(f"Database initialized: {self._db_path}")

    @staticmethod
    def _ensure_column(conn, table: str, column: str, coltype: str):
        """Add a column to an existing table if it doesn't already exist."""
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                logger.info(f"[DB-MIGRATE] Added {table}.{column} ({coltype})")
        except Exception as e:
            logger.debug(f"_ensure_column({table}.{column}) skipped: {e}")

    # ── Candle Data ──────────────────────────────────────────

    def store_candles(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Upsert OHLCV candle data."""
        if df.empty:
            return
        with self._conn() as conn:
            for ts, row in df.iterrows():
                conn.execute(
                    """INSERT OR REPLACE INTO candles
                       (symbol, timeframe, timestamp, open, high, low, close, volume)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (symbol, timeframe, str(ts), row["open"], row["high"],
                     row["low"], row["close"], row.get("volume", 0)),
                )
        logger.debug(f"Stored {len(df)} candles for {symbol}/{timeframe}")

    def load_candles(
        self, symbol: str, timeframe: str,
        start: Optional[str] = None, end: Optional[str] = None,
    ) -> pd.DataFrame:
        """Load candle data from database."""
        query = "SELECT * FROM candles WHERE symbol=? AND timeframe=?"
        params: list = [symbol, timeframe]
        if start:
            query += " AND timestamp >= ?"
            params.append(start)
        if end:
            query += " AND timestamp <= ?"
            params.append(end)
        query += " ORDER BY timestamp"

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame([dict(r) for r in rows])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        df.drop(columns=["symbol", "timeframe"], inplace=True)
        return df

    # ── Tick Data ────────────────────────────────────────────

    def store_tick(self, symbol: str, ltp: float, volume: float = 0,
                   bid: float = 0, ask: float = 0, timestamp: Optional[str] = None):
        ts = timestamp or datetime.now().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO ticks (symbol, timestamp, ltp, volume, bid, ask) VALUES (?,?,?,?,?,?)",
                (symbol, ts, ltp, volume, bid, ask),
            )

    def store_ticks_batch(self, ticks: List[dict]):
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO ticks (symbol, timestamp, ltp, volume, bid, ask) VALUES (?,?,?,?,?,?)",
                [(t["symbol"], t.get("timestamp", datetime.now().isoformat()),
                  t["ltp"], t.get("volume", 0), t.get("bid", 0), t.get("ask", 0)) for t in ticks],
            )

    # ── Trade Records ────────────────────────────────────────

    def store_trade(self, trade: dict):
        """Insert a closed trade row, idempotently.

        The (symbol, exit_time) tuple uniquely identifies a trade in
        practice (microsecond-precision exit_time). Pre-fix: both
        `Portfolio.close_position()` and `TradingAgent._store_trade_to_db()`
        called this method on the same trade, and prior to 2026-05-07 the
        first path didn't exist, so the daemon worked. After we made
        Portfolio idempotent in close_position(), both call paths run, and
        a naive INSERT would duplicate the trade row. Existence check here
        keeps both paths safe.
        """
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT 1 FROM trades WHERE symbol=? AND exit_time=? LIMIT 1",
                (trade["symbol"], trade["exit_time"]),
            ).fetchone()
            if existing:
                return  # already persisted - no-op
            conn.execute(
                """INSERT INTO trades
                   (symbol, side, entry_price, exit_price, quantity,
                    entry_time, exit_time, pnl, pnl_pct, strategy,
                    exit_reason, commission, slippage, market_context)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (trade["symbol"], trade["side"], trade["entry_price"],
                 trade["exit_price"], trade["quantity"],
                 trade["entry_time"], trade["exit_time"],
                 trade["pnl"], trade["pnl_pct"], trade["strategy"],
                 trade["exit_reason"], trade.get("commission", 0),
                 trade.get("slippage", 0), trade.get("market_context", "")),
            )

    def load_trades(self, start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
        query = "SELECT * FROM trades WHERE 1=1"
        params: list = []
        if start:
            query += " AND entry_time >= ?"
            params.append(start)
        if end:
            query += " AND exit_time <= ?"
            params.append(end)
        query += " ORDER BY entry_time"

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()

        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])

    # ── Equity Curve ─────────────────────────────────────────

    def store_equity_point(self, equity: float, cash: float, positions: int):
        # Timezone-aware IST: must match the IST-aware `entry_time` values
        # Portfolio writes via `datetime.now(IST)`, otherwise `_restore_positions`
        # cash-source comparison breaks on UTC hosts. See module-level IST docstring.
        ts = datetime.now(IST).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO equity_curve (timestamp, equity, cash, positions) VALUES (?,?,?,?)",
                (ts, equity, cash, positions),
            )

    def load_equity_curve(self) -> pd.DataFrame:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM equity_curve ORDER BY timestamp").fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([dict(r) for r in rows])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)
        return df

    def get_last_equity_point(self) -> Optional[dict]:
        """Return the most recent equity snapshot or None if no history."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT timestamp, equity, cash, positions FROM equity_curve "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def get_peak_equity(self) -> Optional[float]:
        """Return the all-time peak equity across the curve (for drawdown calc)."""
        with self._conn() as conn:
            row = conn.execute("SELECT MAX(equity) AS peak FROM equity_curve").fetchone()
        if row and row["peak"] is not None:
            return float(row["peak"])
        return None

    # ── Open Positions (survive restarts) ─────────────────────

    def save_open_position(self, symbol: str, side: str, entry_price: float,
                           quantity: int, entry_time: str, stop_loss: float = None,
                           take_profit: float = None, strategy: str = "",
                           order_id: str = "", cash_after: float = 0,
                           regime: str = None,
                           contributing_strategies: Optional[dict] = None):
        """
        Persist an open position. Uses plain INSERT (not INSERT OR REPLACE)
        so concurrent duplicate opens are rejected by the PRIMARY KEY
        constraint on `symbol`. This is the authoritative uniqueness check.

        2026-05-06: numpy-aware JSON encoder. Solo-XGBoost ensemble votes
        produced `{'xgboost_classifier': np.float32(1.0)}`, which json.dumps
        rejects with "Object of type float32 is not JSON serializable".
        Live evidence: UNITDSPR + CGPOWER opens were silently rejected by
        the DB while the order had already simulated as filled — leaving
        the agent with a phantom in-memory position. Casting at the
        encoder level ensures every numpy type (float16/32/64, int*,
        bool_, ndarray) round-trips cleanly regardless of which strategy
        produced it.
        """
        import json as _json
        contrib_clean = _coerce_json_safe(contributing_strategies or {})
        contrib_json = _json.dumps(contrib_clean, default=_json_default)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO open_positions
                   (symbol, side, entry_price, quantity, entry_time,
                    stop_loss, take_profit, strategy, order_id, cash_after,
                    regime, contributing_strategies)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (symbol, side, entry_price, quantity, entry_time,
                 stop_loss, take_profit, strategy, order_id, cash_after,
                 regime, contrib_json),
            )
        logger.debug(f"Saved open position: {quantity}x {symbol} @ {entry_price}")

    def remove_open_position(self, symbol: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))
        logger.debug(f"Removed open position: {symbol}")

    def save_order(self, order: dict) -> None:
        """
        Persist a single order to the audit ledger. Uses INSERT OR REPLACE
        because a live order may go through PLACED -> FILLED updates.
        """
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO orders (
                    order_id, timestamp, symbol, transaction_type, order_type,
                    quantity, filled_quantity, requested_price, filled_price,
                    slippage, status, mode, tag, exchange,
                    stop_loss_price, target_price
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    order.get("order_id"),
                    order.get("timestamp"),
                    order.get("symbol"),
                    order.get("transaction_type"),
                    order.get("order_type"),
                    order.get("quantity"),
                    order.get("filled_quantity", order.get("quantity")),
                    order.get("requested_price"),
                    order.get("filled_price"),
                    order.get("slippage"),
                    order.get("status"),
                    order.get("mode"),
                    order.get("tag"),
                    order.get("exchange"),
                    order.get("stop_loss_price"),
                    order.get("target_price"),
                ),
            )

    def get_recent_orders(self, limit: int = 100) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def load_open_positions(self) -> List[dict]:
        import json as _json
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM open_positions").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            contrib = d.get("contributing_strategies")
            if contrib:
                try:
                    d["contributing_strategies"] = _json.loads(contrib)
                except Exception:
                    d["contributing_strategies"] = {}
            else:
                d["contributing_strategies"] = {}
            out.append(d)
        return out

    # ── Learning: Strategy Scores ─────────────────────────────

    def save_strategy_score(self, strategy: str, stats: dict):
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO strategy_scores
                   (strategy, total_trades, wins, losses, total_pnl, avg_pnl,
                    win_rate, profit_factor, sharpe, learned_weight, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (strategy, stats.get("total_trades", 0), stats.get("wins", 0),
                 stats.get("losses", 0), stats.get("total_pnl", 0),
                 stats.get("avg_pnl", 0), stats.get("win_rate", 0),
                 stats.get("profit_factor", 0), stats.get("sharpe", 0),
                 stats.get("learned_weight", 1.0), datetime.now().isoformat()),
            )

    def load_strategy_scores(self) -> Dict[str, dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM strategy_scores").fetchall()
        return {r["strategy"]: dict(r) for r in rows}

    def delete_strategy_score(self, strategy: str) -> int:
        """Remove a strategy row from strategy_scores.

        Used by ``TradeAnalyzer._rehydrate_internal_accumulators`` to evict
        phantom rows (strategy names that survived in the DB after a
        refactor but no longer appear in the trades table -- the canonical
        case is the ``ensemble`` row left behind when ensemble vote
        attribution moved to per-strategy contributors on 2026-05-06).
        Returns the number of rows deleted (0 or 1)."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM strategy_scores WHERE strategy = ?", (strategy,)
            )
            return cur.rowcount

    def load_learned_weights(self) -> Dict[str, float]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT strategy, learned_weight FROM strategy_scores WHERE learned_weight IS NOT NULL"
            ).fetchall()
        return {r["strategy"]: r["learned_weight"] for r in rows}

    # ── Learning: Trade Patterns ──────────────────────────────

    def save_trade_pattern(self, pattern: dict):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO trade_patterns
                   (strategy, symbol, entry_time, rsi, atr_pct, volume_ratio,
                    hour_of_day, day_of_week, market_trend, pnl, pnl_pct,
                    outcome, exit_reason, holding_minutes, pnl_bucket, regime,
                    created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pattern.get("strategy", ""), pattern.get("symbol", ""),
                 pattern.get("entry_time", ""), pattern.get("rsi"),
                 pattern.get("atr_pct"), pattern.get("volume_ratio"),
                 pattern.get("hour_of_day"), pattern.get("day_of_week"),
                 pattern.get("market_trend"), pattern.get("pnl", 0),
                 pattern.get("pnl_pct", 0), pattern.get("outcome", ""),
                 pattern.get("exit_reason", ""), pattern.get("holding_minutes"),
                 pattern.get("pnl_bucket", ""), pattern.get("regime", ""),
                 datetime.now().isoformat()),
            )

    def load_trade_patterns(self, limit: int = 200) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trade_patterns ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def load_patterns_by_outcome(self, outcome: str, limit: int = 100) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trade_patterns WHERE outcome=? ORDER BY created_at DESC LIMIT ?",
                (outcome, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Learning: Regime Weights ──────────────────────────────

    def save_regime_weight(self, strategy: str, regime: str, weight: float,
                           trades: int = 0, wins: int = 0, total_pnl: float = 0.0,
                           sharpe: float = 0.0):
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO regime_weights
                   (strategy, regime, weight, trades, wins, total_pnl, sharpe, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (strategy, regime, weight, trades, wins, total_pnl, sharpe,
                 datetime.now().isoformat()),
            )

    def load_regime_weights(self) -> Dict[tuple, dict]:
        """Return {(strategy, regime): stats_dict}."""
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM regime_weights").fetchall()
        return {(r["strategy"], r["regime"]): dict(r) for r in rows}

    def load_regime_weights_for(self, regime: str) -> Dict[str, float]:
        """Return {strategy: weight} for a specific regime."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT strategy, weight FROM regime_weights WHERE regime=?",
                (regime,),
            ).fetchall()
        return {r["strategy"]: r["weight"] for r in rows}

    def load_trades_for_day(self, day_iso: str) -> List[dict]:
        """Return all trades that exited on the given YYYY-MM-DD date."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE substr(exit_time,1,10)=? ORDER BY exit_time",
                (day_iso,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Cleanup ──────────────────────────────────────────────

    def purge_old_ticks(self, days: int = 7):
        """Remove tick data older than N days to keep DB size manageable."""
        cutoff = (datetime.now() - pd.Timedelta(days=days)).isoformat()
        with self._conn() as conn:
            result = conn.execute("DELETE FROM ticks WHERE timestamp < ?", (cutoff,))
            logger.info(f"Purged {result.rowcount} old ticks (older than {days} days)")
