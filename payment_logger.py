"""x402 Payment Logger — records settlements to a shared SQLite DB.

All x402 APIs on this machine write to the same DB for unified analytics.
DB: ~/.local/share/x402-payments/payments.db

Design: fail-open. If logging fails, the payment path is never blocked.
Hook-based: register on x402ResourceServer.on_after_settle / on_settle_failure.

Error severity classification (persisted as prefix in error column):
- [critical]: Facilitator 500, on-chain failure (lost revenue)
- [transient]: Timeout, network congestion (may succeed on retry)
- [silent]: Null/empty error context (logging infrastructure issue)
"""

import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

_DEFAULT_DB = str(
    Path.home() / ".local" / "share" / "x402-payments" / "payments.db"
)

_RETRY_COOLDOWN = 60  # seconds between re-init attempts


def _classify_error(msg: str) -> str:
    if not msg:
        return "silent"
    lower = msg.lower()
    if "facilitator settle failed" in lower or "settle_exact_failed_onchain" in lower:
        return "critical"
    if "context deadline" in lower or "did not confirm in time" in lower or "timeout" in lower:
        return "transient"
    return "critical"


class PaymentLogger:
    """Thread-safe SQLite logger for x402 payment settlements."""

    def __init__(self, api_name: str, db_path: str | None = None):
        self.api_name = api_name
        self._db_path = db_path or os.getenv("X402_PAYMENTS_DB", _DEFAULT_DB)
        self._enabled = True
        self._local = threading.local()
        self._drop_count = 0
        self._last_retry = 0.0
        # Failure rate tracking
        self._recent_failures: list[float] = []
        self._failure_window = 300  # 5 minutes
        self._failure_threshold = 10
        self._last_alert = 0.0
        try:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()
        except Exception as exc:
            print(
                f"[pay-log] WARNING: init failed, will retry on next write: {exc}",
                file=sys.stderr,
            )
            self._enabled = False

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=500")
        return self._local.conn

    def _init_schema(self):
        c = self._conn()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS payment_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                api       TEXT    NOT NULL,
                payer     TEXT    NOT NULL DEFAULT '',
                amount    TEXT    NOT NULL DEFAULT '0',
                network   TEXT    NOT NULL DEFAULT '',
                tx_hash   TEXT    NOT NULL DEFAULT '',
                success   INTEGER NOT NULL DEFAULT 1,
                error     TEXT    NOT NULL DEFAULT '',
                timestamp TEXT    DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_payment_ts ON payment_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_payment_api ON payment_log(api);
            CREATE INDEX IF NOT EXISTS idx_payment_payer ON payment_log(payer);
        """)
        c.commit()

    def _try_recover(self) -> bool:
        now = time.time()
        if now - self._last_retry < _RETRY_COOLDOWN:
            return False
        self._last_retry = now
        try:
            self._local.conn = None
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()
            self._enabled = True
            print("[pay-log] Recovered: DB re-initialized", file=sys.stderr)
            return True
        except Exception:
            return False

    def _log_drop(self, exc: Exception):
        self._drop_count += 1
        if self._drop_count <= 5 or self._drop_count % 100 == 0:
            print(
                f"[pay-log] WARNING: write failed (drops: {self._drop_count}): {exc}",
                file=sys.stderr,
            )

    def _check_failure_rate(self):
        now = time.time()
        self._recent_failures = [
            t for t in self._recent_failures if now - t < self._failure_window
        ]
        self._recent_failures.append(now)
        if (
            len(self._recent_failures) >= self._failure_threshold
            and now - self._last_alert > self._failure_window
        ):
            self._last_alert = now
            print(
                f"[pay-log] ALERT: {self.api_name} settlement failure rate exceeded "
                f"threshold: {len(self._recent_failures)} failures in 5 min. "
                f"Possible Facilitator outage.",
                file=sys.stderr,
            )

    def log_settlement(self, ctx) -> None:
        """on_after_settle hook — logs successful settlement."""
        if not self._enabled and not self._try_recover():
            return
        try:
            result = ctx.result
            payload = ctx.payment_payload
            accepted = getattr(payload, "accepted", None)
            amount = (
                accepted.amount
                if accepted is not None
                else str(getattr(payload, "maxAmountRequired", "0"))
            )
            c = self._conn()
            c.execute(
                "INSERT INTO payment_log "
                "(api, payer, amount, network, tx_hash, success) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.api_name,
                    result.payer or "",
                    amount,
                    result.network or "",
                    result.transaction or "",
                    int(result.success),
                ),
            )
            c.commit()
            print(
                f"[pay-log] settled api={self.api_name} "
                f"payer={result.payer or '?'} "
                f"amount={amount} net={result.network or '?'} "
                f"tx={(result.transaction or '')[:16]}...",
                file=sys.stderr,
            )
        except Exception as exc:
            try:
                self._conn().rollback()
            except Exception:
                pass
            self._log_drop(exc)

    def log_failure(self, ctx) -> None:
        """on_settle_failure hook — logs failed settlement with severity."""
        if not self._enabled and not self._try_recover():
            return
        try:
            payload = ctx.payment_payload
            accepted = getattr(payload, "accepted", None)
            amount = (
                accepted.amount
                if accepted is not None
                else str(getattr(payload, "maxAmountRequired", "0"))
            )
            network = (
                accepted.network
                if accepted is not None
                else ""
            )
            error_msg = str(ctx.error)[:500] if ctx.error else ""
            severity = _classify_error(error_msg)
            tagged_error = f"[{severity}] {error_msg}"

            c = self._conn()
            c.execute(
                "INSERT INTO payment_log "
                "(api, payer, amount, network, tx_hash, success, error) "
                "VALUES (?, '', ?, ?, '', 0, ?)",
                (
                    self.api_name,
                    amount,
                    network,
                    tagged_error[:500],
                ),
            )
            c.commit()
            print(
                f"[pay-log] FAILED api={self.api_name} severity={severity} "
                f"amount={amount} error={error_msg[:120]}",
                file=sys.stderr,
            )
            self._check_failure_rate()
        except Exception as exc:
            try:
                self._conn().rollback()
            except Exception:
                pass
            self._log_drop(exc)
