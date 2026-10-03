"""换币结算库主入口。"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction

from .amounts import format_amount, parse_amount
from .currencies import CURRENCIES, QUOTE_CURRENCY, atom_scale
from .errors import (
    BusinessRejection,
    ConsistencyError,
    InvalidRateError,
    RatesNotSetError,
    RequestConflictError,
)
from .store import connect

_ACCOUNT_KINDS = ("customer", "reserve", "fee")
_PRICE_RE = re.compile(r"\d+(\.\d+)?")

MIN_PRINCIPAL = Fraction(1, 100000)  # 0.00001 个付款币种
MAX_VALUE_USDT = Fraction(10000)  # 本金等值上限（不含手续费）
FEE_CAP_USDT = Fraction(5)  # 手续费封顶


@dataclass(frozen=True)
class Receipt:
    """持久化回执。金额均为十进制字符串。"""

    key: str
    rate_version: int
    src: str
    dst: str
    principal: str
    fee: str
    credited: str


class SettlementLibrary:
    """本地换币结算库。

    首次初始化时由可信参数创建客户钱包、各币种兑换储备和手续费账户及初始
    余额（缺省币种为 0）；数据库已存在账户时忽略初始化参数。
    """

    def __init__(
        self,
        path: str,
        customer: dict[str, str] | None = None,
        reserve: dict[str, str] | None = None,
        fee_account: dict[str, str] | None = None,
    ) -> None:
        self._conn = connect(path)
        row = self._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()
        if row[0] == 0:
            self._init_accounts(customer or {}, reserve or {}, fee_account or {})

    def _init_accounts(
        self,
        customer: dict[str, str],
        reserve: dict[str, str],
        fee_account: dict[str, str],
    ) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for kind, balances in (
                ("customer", customer),
                ("reserve", reserve),
                ("fee", fee_account),
            ):
                unknown = set(balances) - set(CURRENCIES)
                if unknown:
                    from .errors import UnknownCurrencyError

                    raise UnknownCurrencyError(f"不支持的币种: {sorted(unknown)}")
                for currency in CURRENCIES:
                    atoms = parse_amount(balances.get(currency, "0"), currency)
                    self._conn.execute(
                        "INSERT INTO accounts(kind, currency, balance_atoms)"
                        " VALUES (?, ?, ?)",
                        (kind, currency, str(atoms)),
                    )
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def set_rates(self, rates: dict[str, str]) -> int:
        """整组替换各币种兑 USDT 的价格，返回单调递增的版本号。"""
        prices: dict[str, str] = {}
        for currency in CURRENCIES:
            if currency == QUOTE_CURRENCY:
                text = rates.get(currency, "1")
                if not isinstance(text, str) or not _PRICE_RE.fullmatch(text):
                    raise InvalidRateError(f"非法的 USDT 汇率: {text!r}")
                if Decimal(text) != 1:
                    raise InvalidRateError("USDT 价格固定为 1")
                prices[currency] = "1"
                continue
            text = rates.get(currency)
            if not isinstance(text, str) or not _PRICE_RE.fullmatch(text):
                raise InvalidRateError(f"缺少或非法的 {currency} 汇率: {text!r}")
            if Decimal(text) <= 0:
                raise InvalidRateError(f"{currency} 汇率必须为正: {text!r}")
            prices[currency] = text
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM rate_groups"
            ).fetchone()
            version = row[0] + 1
            now = _utcnow()
            self._conn.execute(
                "INSERT INTO rate_groups(version, created_at) VALUES (?, ?)",
                (version, now),
            )
            self._conn.executemany(
                "INSERT INTO rates(version, currency, price) VALUES (?, ?, ?)",
                [(version, c, p) for c, p in prices.items()],
            )
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        return version

    def exchange(self, key: str, src: str, dst: str, amount: str) -> Receipt:
        """兑换指定本金，返回持久化回执。业务拒绝时抛 BusinessRejection。"""
        if src == dst:
            raise ValueError("付款币种与收款币种必须不同")
        src_atoms = parse_amount(amount, src)  # 同时校验币种与金额格式
        atom_scale(dst)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self._conn.execute(
                "SELECT src, dst, amount_atoms, status, reason, receipt_id"
                " FROM requests WHERE key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                ex_src, ex_dst, ex_atoms, status, reason, receipt_id = existing
                result = self._replay(
                    key, src, dst, src_atoms,
                    (ex_src, ex_dst, int(ex_atoms), status, reason, receipt_id),
                )
                self._conn.execute("COMMIT")
                return result
            try:
                receipt = self._settle(key, src, dst, src_atoms)
            except BusinessRejection as rejection:
                self._conn.execute(
                    "INSERT INTO requests(key, src, dst, amount_atoms, status, reason)"
                    " VALUES (?, ?, ?, ?, 'rejected', ?)",
                    (key, src, dst, str(src_atoms), rejection.reason),
                )
                self._conn.execute("COMMIT")
                raise
            self._conn.execute("COMMIT")
            return receipt
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _replay(
        self,
        key: str,
        src: str,
        dst: str,
        src_atoms: int,
        existing: tuple,
    ) -> Receipt:
        ex_src, ex_dst, ex_atoms, status, reason, receipt_id = existing
        if (ex_src, ex_dst, ex_atoms) != (src, dst, src_atoms):
            raise RequestConflictError(f"key {key!r} 已用于不同参数的请求")
        if status == "rejected":
            raise BusinessRejection(key, reason)
        row = self._conn.execute(
            "SELECT rate_version, principal_atoms, fee_atoms, credited_atoms"
            " FROM receipts WHERE id = ?",
            (receipt_id,),
        ).fetchone()
        return Receipt(
            key=key,
            rate_version=row[0],
            src=src,
            dst=dst,
            principal=format_amount(int(row[1]), src),
            fee=format_amount(int(row[2]), src),
            credited=format_amount(int(row[3]), dst),
        )

    def _settle(self, key: str, src: str, dst: str, src_atoms: int) -> Receipt:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM rate_groups"
        ).fetchone()
        version = row[0]
        if version == 0:
            raise RatesNotSetError("尚未设置汇率")
        prices = {
            currency: Fraction(Decimal(text))
            for currency, text in self._conn.execute(
                "SELECT currency, price FROM rates WHERE version = ?", (version,)
            )
        }
        price_src, price_dst = prices[src], prices[dst]
        scale_src, scale_dst = atom_scale(src), atom_scale(dst)

        principal_value = Fraction(src_atoms, scale_src)
        if principal_value < MIN_PRINCIPAL:
            raise BusinessRejection(key, "principal_below_minimum")
        value_usdt = principal_value * price_src
        if value_usdt > MAX_VALUE_USDT:
            raise BusinessRejection(key, "value_limit_exceeded")
        credited = _floor(value_usdt / price_dst * scale_dst)
        if credited == 0:
            raise BusinessRejection(key, "zero_credited_amount")
        fee = min(
            -(-src_atoms // 1000),  # 本金千分之一向上取整
            _floor(FEE_CAP_USDT / price_src * scale_src),
        )

        self._require_balance(key, "customer", src, src_atoms + fee,
                              "insufficient_customer_funds")
        self._require_balance(key, "reserve", dst, credited,
                              "insufficient_reserve")

        now = _utcnow()
        cursor = self._conn.execute(
            "INSERT INTO receipts(key, rate_version, src, dst, principal_atoms,"
            " fee_atoms, credited_atoms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, version, src, dst, str(src_atoms), str(fee), str(credited), now),
        )
        receipt_id = cursor.lastrowid
        movements = [
            ("customer", src, -(src_atoms + fee)),
            ("reserve", src, src_atoms),
            ("fee", src, fee),
            ("reserve", dst, -credited),
            ("customer", dst, credited),
        ]
        for kind, currency, delta in movements:
            self._apply_delta(kind, currency, delta)
            self._conn.execute(
                "INSERT INTO entries(receipt_id, kind, currency, delta_atoms)"
                " VALUES (?, ?, ?, ?)",
                (receipt_id, kind, currency, str(delta)),
            )
        self._conn.execute(
            "INSERT INTO requests(key, src, dst, amount_atoms, status, receipt_id)"
            " VALUES (?, ?, ?, ?, 'success', ?)",
            (key, src, dst, str(src_atoms), receipt_id),
        )
        return Receipt(
            key=key,
            rate_version=version,
            src=src,
            dst=dst,
            principal=format_amount(src_atoms, src),
            fee=format_amount(fee, src),
            credited=format_amount(credited, dst),
        )

    def _apply_delta(self, kind: str, currency: str, delta: int) -> None:
        row = self._conn.execute(
            "SELECT balance_atoms FROM accounts WHERE kind = ? AND currency = ?",
            (kind, currency),
        ).fetchone()
        new_balance = int(row[0]) + delta
        if new_balance < 0:
            raise ConsistencyError(f"{kind}/{currency} 余额将为负")
        self._conn.execute(
            "UPDATE accounts SET balance_atoms = ? WHERE kind = ? AND currency = ?",
            (str(new_balance), kind, currency),
        )

    def _require_balance(
        self, key: str, kind: str, currency: str, needed: int, reason: str
    ) -> None:
        row = self._conn.execute(
            "SELECT balance_atoms FROM accounts WHERE kind = ? AND currency = ?",
            (kind, currency),
        ).fetchone()
        if int(row[0]) < needed:
            raise BusinessRejection(key, reason)

    def balance(self, kind: str, currency: str) -> str:
        """查询账户余额（kind: customer/reserve/fee），返回十进制字符串。"""
        if kind not in _ACCOUNT_KINDS:
            raise ValueError(f"未知账户类型: {kind!r}")
        row = self._conn.execute(
            "SELECT balance_atoms FROM accounts WHERE kind = ? AND currency = ?",
            (kind, currency),
        ).fetchone()
        if row is None:
            atom_scale(currency)  # 抛 UnknownCurrencyError
            raise ValueError(f"账户不存在: {kind}/{currency}")
        return format_amount(int(row[0]), currency)

    def ledger_entries(self, key: str) -> list[tuple[str, str, str]]:
        """按 key 查询分录，返回 (账户类型, 币种, 变动额) 列表。"""
        rows = self._conn.execute(
            "SELECT e.kind, e.currency, e.delta_atoms FROM entries e"
            " JOIN receipts r ON r.id = e.receipt_id"
            " WHERE r.key = ? ORDER BY e.id",
            (key,),
        ).fetchall()
        return [
            (kind, cur, _format_signed(int(delta), cur))
            for kind, cur, delta in rows
        ]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SettlementLibrary":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _floor(value: Fraction) -> int:
    return value.numerator // value.denominator


def _format_signed(atoms: int, currency: str) -> str:
    if atoms < 0:
        return "-" + format_amount(-atoms, currency)
    return format_amount(atoms, currency)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
