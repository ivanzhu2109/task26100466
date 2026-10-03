"""Core exchange settlement ledger backed by SQLite."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, localcontext
from typing import Dict, Optional

from .amounts import format_amount, parse_amount, parse_price
from .currencies import CURRENCIES, QUOTE
from .errors import ConflictError

_PRECISION = 80
_MIN_PRINCIPAL = Decimal("0.00001")
_MAX_VALUE_USDT = Decimal("10000")
_FEE_CAP_USDT = Decimal("5")
_ACCOUNTS = ("user", "reserve", "fee")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS balances (
    account  TEXT NOT NULL,
    currency TEXT NOT NULL,
    balance  TEXT NOT NULL,
    PRIMARY KEY (account, currency)
);
CREATE TABLE IF NOT EXISTS rate_versions (
    version INTEGER PRIMARY KEY,
    rates   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    key             TEXT PRIMARY KEY,
    src             TEXT NOT NULL,
    dst             TEXT NOT NULL,
    amount_atoms    TEXT NOT NULL,
    status          TEXT NOT NULL,
    rate_version    INTEGER,
    principal_atoms TEXT,
    fee_atoms       TEXT,
    credit_atoms    TEXT,
    error           TEXT
);
CREATE TABLE IF NOT EXISTS entries (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    key      TEXT NOT NULL REFERENCES requests(key),
    account  TEXT NOT NULL,
    currency TEXT NOT NULL,
    delta    TEXT NOT NULL
);
"""


class ExchangeLedger:
    """Single-user, two-currency exchange settlement ledger.

    All state lives in one SQLite database. Each exchange is fully applied
    together with its journal entries and receipt in one transaction, or not
    applied at all. Idempotency keys are unique across the whole database.
    """

    def __init__(self, path: str, balances: Optional[Dict[str, Dict[str, str]]] = None):
        """Open (or create) the ledger at ``path``.

        ``balances`` is required when the database is first created and is
        ignored afterwards. Shape::

            {"user":    {"BTC": "1.5", "USDT": "1000", ...},
             "reserve": {"BTC": "10",  "USDT": "100000", ...},
             "fee":     {"BTC": "0",   "USDT": "0", ...}}

        Amounts are decimal strings in whole currency units; every currency
        in ``fxsettle.currencies.CURRENCIES`` must be present for each of the
        three accounts.
        """
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        initialized = self._conn.execute(
            "SELECT v FROM meta WHERE k = 'initialized'"
        ).fetchone()
        if initialized is None:
            if balances is None:
                raise ValueError("balances are required to initialize a new ledger")
            self._initialize(balances)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ExchangeLedger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _initialize(self, balances: Dict[str, Dict[str, str]]) -> None:
        rows = []
        for account in _ACCOUNTS:
            if account not in balances:
                raise ValueError(f"missing initial balances for account {account!r}")
            for currency in CURRENCIES:
                if currency not in balances[account]:
                    raise ValueError(
                        f"missing initial balance for {account}/{currency}"
                    )
                rows.append(
                    (account, currency, str(parse_amount(balances[account][currency], currency)))
                )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.executemany(
                "INSERT INTO balances (account, currency, balance) VALUES (?, ?, ?)",
                rows,
            )
            self._conn.execute(
                "INSERT INTO meta (k, v) VALUES ('initialized', '1')"
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def set_rates(self, rates: Dict[str, str]) -> int:
        """Atomically replace the whole rate table; returns the new version.

        ``rates`` maps currency code to a positive decimal string price in
        USDT. USDT itself is fixed to 1 and may be omitted.
        """
        unknown = set(rates) - set(CURRENCIES)
        if unknown:
            raise ValueError(f"unknown currencies in rates: {sorted(unknown)}")
        full: Dict[str, str] = {}
        for currency in CURRENCIES:
            if currency == QUOTE:
                full[currency] = "1"
                continue
            if currency not in rates:
                raise ValueError(f"missing rate for {currency}")
            full[currency] = str(parse_price(rates[currency]))
        if QUOTE in rates and parse_price(rates[QUOTE]) != 1:
            raise ValueError("USDT price is fixed to 1")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM rate_versions"
            ).fetchone()
            version = int(row[0])
            self._conn.execute(
                "INSERT INTO rate_versions (version, rates) VALUES (?, ?)",
                (version, json.dumps(full, sort_keys=True)),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return version

    def _current_rates(self) -> Optional[tuple]:
        row = self._conn.execute(
            "SELECT version, rates FROM rate_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), {k: Decimal(v) for k, v in json.loads(row[1]).items()}

    def exchange(self, key: str, src: str, dst: str, amount: str) -> dict:
        """Exchange ``amount`` of ``src`` into ``dst`` under idempotency ``key``.

        Returns the persisted receipt dict. Replaying the same key with the
        same request returns the original receipt (including recorded
        rejections); reusing the key with a different request raises
        ``ConflictError``.
        """
        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty string")
        if src not in CURRENCIES:
            raise ValueError(f"unknown currency: {src!r}")
        if dst not in CURRENCIES:
            raise ValueError(f"unknown currency: {dst!r}")
        if src == dst:
            raise ValueError("src and dst must differ")
        amount_atoms = parse_amount(amount, src)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            receipt = self._exchange_in_tx(key, src, dst, amount_atoms)
            self._conn.execute("COMMIT")
            return receipt
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _exchange_in_tx(self, key: str, src: str, dst: str, amount_atoms: int) -> dict:
        row = self._conn.execute(
            "SELECT * FROM requests WHERE key = ?", (key,)
        ).fetchone()
        if row is not None:
            stored = dict(
                zip([c[1] for c in self._conn.execute("PRAGMA table_info(requests)")], row)
            )
            if (
                stored["src"] != src
                or stored["dst"] != dst
                or int(stored["amount_atoms"]) != amount_atoms
            ):
                raise ConflictError(f"key {key!r} was used with a different request")
            return self._receipt_from_row(stored)

        current = self._current_rates()
        if current is None:
            return self._record_rejection(key, src, dst, amount_atoms, None, "no_rates")
        version, rates = current

        with localcontext() as ctx:
            ctx.prec = _PRECISION
            src_scale = Decimal(10) ** CURRENCIES[src]
            dst_scale = Decimal(10) ** CURRENCIES[dst]
            principal = Decimal(amount_atoms)

            if principal / src_scale < _MIN_PRINCIPAL:
                return self._record_rejection(
                    key, src, dst, amount_atoms, version, "principal_too_small"
                )
            value_usdt = principal * rates[src] / src_scale
            if value_usdt > _MAX_VALUE_USDT:
                return self._record_rejection(
                    key, src, dst, amount_atoms, version, "limit_exceeded"
                )
            credit = int(
                (principal * rates[src] * dst_scale) // (src_scale * rates[dst])
            )
            if credit == 0:
                return self._record_rejection(
                    key, src, dst, amount_atoms, version, "zero_credit"
                )
            fee_cap = int((_FEE_CAP_USDT * src_scale) // rates[src])
            fee = min(-(-amount_atoms // 1000), fee_cap)

        user_src = self._balance("user", src)
        reserve_dst = self._balance("reserve", dst)
        if user_src < amount_atoms + fee or reserve_dst < credit:
            return self._record_rejection(
                key, src, dst, amount_atoms, version, "insufficient_funds"
            )

        movements = [
            ("user", src, -(amount_atoms + fee)),
            ("reserve", src, amount_atoms),
            ("reserve", dst, -credit),
            ("user", dst, credit),
            ("fee", src, fee),
        ]
        for account, currency, delta in movements:
            new_balance = self._balance(account, currency) + delta
            if new_balance < 0:
                raise RuntimeError("balance would go negative; aborting transaction")
            self._conn.execute(
                "UPDATE balances SET balance = ? WHERE account = ? AND currency = ?",
                (str(new_balance), account, currency),
            )
        self._conn.execute(
            "INSERT INTO requests (key, src, dst, amount_atoms, status, "
            "rate_version, principal_atoms, fee_atoms, credit_atoms, error) "
            "VALUES (?, ?, ?, ?, 'ok', ?, ?, ?, ?, NULL)",
            (key, src, dst, str(amount_atoms), version, str(amount_atoms), str(fee), str(credit)),
        )
        self._conn.executemany(
            "INSERT INTO entries (key, account, currency, delta) VALUES (?, ?, ?, ?)",
            [(key, account, currency, str(delta)) for account, currency, delta in movements],
        )
        return {
            "key": key,
            "status": "ok",
            "src": src,
            "dst": dst,
            "amount": format_amount(amount_atoms, src),
            "rate_version": version,
            "principal": format_amount(amount_atoms, src),
            "principal_atoms": amount_atoms,
            "fee": format_amount(fee, src),
            "fee_atoms": fee,
            "credit": format_amount(credit, dst),
            "credit_atoms": credit,
            "error": None,
        }

    def _record_rejection(
        self,
        key: str,
        src: str,
        dst: str,
        amount_atoms: int,
        version: Optional[int],
        error: str,
    ) -> dict:
        self._conn.execute(
            "INSERT INTO requests (key, src, dst, amount_atoms, status, "
            "rate_version, principal_atoms, fee_atoms, credit_atoms, error) "
            "VALUES (?, ?, ?, ?, 'rejected', ?, NULL, NULL, NULL, ?)",
            (key, src, dst, str(amount_atoms), version, error),
        )
        return {
            "key": key,
            "status": "rejected",
            "src": src,
            "dst": dst,
            "amount": format_amount(amount_atoms, src),
            "rate_version": version,
            "principal": None,
            "principal_atoms": None,
            "fee": None,
            "fee_atoms": None,
            "credit": None,
            "credit_atoms": None,
            "error": error,
        }

    def _receipt_from_row(self, stored: dict) -> dict:
        src, dst = stored["src"], stored["dst"]
        ok = stored["status"] == "ok"
        amount_atoms = int(stored["amount_atoms"])
        principal_atoms = int(stored["principal_atoms"]) if ok else None
        fee_atoms = int(stored["fee_atoms"]) if ok else None
        credit_atoms = int(stored["credit_atoms"]) if ok else None
        return {
            "key": stored["key"],
            "status": stored["status"],
            "src": src,
            "dst": dst,
            "amount": format_amount(amount_atoms, src),
            "rate_version": stored["rate_version"],
            "principal": format_amount(principal_atoms, src) if ok else None,
            "principal_atoms": principal_atoms,
            "fee": format_amount(fee_atoms, src) if ok else None,
            "fee_atoms": fee_atoms,
            "credit": format_amount(credit_atoms, dst) if ok else None,
            "credit_atoms": credit_atoms,
            "error": stored["error"],
        }

    def balance(self, account: str, currency: str) -> int:
        """Current balance of an account (``user``/``reserve``/``fee``) in atoms."""
        if account not in _ACCOUNTS:
            raise ValueError(f"unknown account: {account!r}")
        if currency not in CURRENCIES:
            raise ValueError(f"unknown currency: {currency!r}")
        return self._balance(account, currency)

    def _balance(self, account: str, currency: str) -> int:
        row = self._conn.execute(
            "SELECT balance FROM balances WHERE account = ? AND currency = ?",
            (account, currency),
        ).fetchone()
        return int(row[0])
