"""fxsettle 自动化测试。"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from fxsettle import (
    BusinessRejection,
    InvalidAmountError,
    RatesNotSetError,
    RequestConflictError,
    SettlementLibrary,
)
from fxsettle.currencies import CURRENCIES, atom_scale

RATES = {"BTC": "100000", "ETH": "3000", "USDC": "1", "LOCAL": "7.2"}


def make_lib(path=":memory:", **overrides):
    customer = overrides.pop("customer", {"BTC": "2", "USDT": "50000", "LOCAL": "1000"})
    reserve = overrides.pop(
        "reserve",
        {"BTC": "100", "ETH": "1000", "USDT": "1000000", "USDC": "500000",
         "LOCAL": "100000"},
    )
    fee_account = overrides.pop("fee_account", {})
    return SettlementLibrary(
        path, customer=customer, reserve=reserve, fee_account=fee_account
    )


def totals(lib):
    """每个币种三类账户的原子总量。"""
    out = {}
    for currency in CURRENCIES:
        out[currency] = sum(
            parse_to_atoms(lib.balance(kind, currency), currency)
            for kind in ("customer", "reserve", "fee")
        )
    return out


def parse_to_atoms(text, currency):
    return int(Decimal(text) * atom_scale(currency))


def expected_credited(src_atoms, src, dst, rates):
    """独立精确计算到账原子数。"""
    price_src = Fraction(Decimal(rates[src] if src != "USDT" else "1"))
    price_dst = Fraction(Decimal(rates[dst] if dst != "USDT" else "1"))
    value = Fraction(src_atoms, atom_scale(src)) * price_src / price_dst
    return (value * atom_scale(dst)).numerator // (value * atom_scale(dst)).denominator


def expected_fee(src_atoms, src, rates):
    """独立精确计算手续费原子数。"""
    price_src = Fraction(Decimal(rates[src] if src != "USDT" else "1"))
    mille = -(-src_atoms // 1000)
    cap_value = Fraction(5) / price_src * atom_scale(src)
    cap = cap_value.numerator // cap_value.denominator
    return min(mille, cap)


# ---------- 基本兑换与守恒 ----------


def test_exchange_moves_funds_and_conserves_atoms():
    lib = make_lib()
    lib.set_rates(RATES)
    before = totals(lib)
    receipt = lib.exchange("k1", "BTC", "USDT", "0.1")
    assert receipt.rate_version == 1
    assert receipt.principal == "0.1"
    src_atoms = parse_to_atoms("0.1", "BTC")
    assert parse_to_atoms(receipt.credited, "USDT") == expected_credited(
        src_atoms, "BTC", "USDT", RATES)
    assert parse_to_atoms(receipt.fee, "BTC") == expected_fee(
        src_atoms, "BTC", RATES)
    after = totals(lib)
    assert before == after  # 每个币种原子总量独立守恒


def test_receipt_matches_entries():
    lib = make_lib()
    lib.set_rates(RATES)
    receipt = lib.exchange("k2", "BTC", "ETH", "0.05")
    entries = lib.ledger_entries("k2")
    by_pair = {}
    for kind, currency, delta in entries:
        by_pair[(kind, currency)] = by_pair.get((kind, currency), Decimal(0)) + Decimal(delta)
    # 分录与回执完全一致
    assert by_pair[("customer", "BTC")] == -(
        Decimal(receipt.principal) + Decimal(receipt.fee))
    assert by_pair[("reserve", "BTC")] == Decimal(receipt.principal)
    assert by_pair[("fee", "BTC")] == Decimal(receipt.fee)
    assert by_pair[("reserve", "ETH")] == -Decimal(receipt.credited)
    assert by_pair[("customer", "ETH")] == Decimal(receipt.credited)
    # 每个币种分录净额为零
    for currency in CURRENCIES:
        net = sum(Decimal(d) for k, c, d in entries if c == currency)
        assert net == 0


# ---------- 手续费封顶不足一个原子 + 限额边界 ----------


def test_fee_cap_below_one_atom_means_zero_fee():
    # LOCAL 价格 20000 USDT：5 USDT = 0.00025 LOCAL = 0.025 原子，向下取整为 0
    rates = {"BTC": "100000", "ETH": "3000", "USDC": "1", "LOCAL": "20000"}
    lib = make_lib()
    lib.set_rates(rates)
    # 本金 0.5 LOCAL，等值恰好 10000 USDT（限额边界，含边界）
    receipt = lib.exchange("cap", "LOCAL", "USDT", "0.5")
    src_atoms = parse_to_atoms("0.5", "LOCAL")
    assert expected_fee(src_atoms, "LOCAL", rates) == 0  # 独立核算：封顶不足一原子
    assert receipt.fee == "0"
    assert parse_to_atoms(receipt.credited, "USDT") == expected_credited(
        src_atoms, "LOCAL", "USDT", rates)
    assert receipt.credited == "10000"
    # 千分之一向上取整本应为 1 原子，被封顶压到 0
    assert -(-src_atoms // 1000) == 1
    # 客户 LOCAL 只减少本金，手续费账户为零
    assert lib.balance("customer", "LOCAL") == "999.5"
    assert lib.balance("fee", "LOCAL") == "0"


def test_value_limit_boundary():
    rates = {"BTC": "100000", "ETH": "3000", "USDC": "1", "LOCAL": "20000"}
    lib = make_lib()
    lib.set_rates(rates)
    # 0.5 LOCAL = 10000 USDT 恰好通过；0.51 LOCAL = 10200 USDT 超限
    lib.exchange("at-limit", "LOCAL", "USDT", "0.5")
    with pytest.raises(BusinessRejection) as exc:
        lib.exchange("over-limit", "LOCAL", "USDT", "0.51")
    assert exc.value.reason == "value_limit_exceeded"


def test_min_principal_boundary():
    lib = make_lib()
    lib.set_rates(RATES)
    lib.exchange("min-ok", "BTC", "USDT", "0.00001")  # 恰好下限
    with pytest.raises(BusinessRejection) as exc:
        lib.exchange("min-low", "BTC", "USDT", "0.000001")
    assert exc.value.reason == "principal_below_minimum"


def test_zero_credited_rejected():
    lib = make_lib()
    lib.set_rates(RATES)
    # 0.00001 USDT 换成 BTC：1e-5 / 100000 * 1e8 = 0.01 原子，向下取整为 0
    with pytest.raises(BusinessRejection) as exc:
        lib.exchange("dust", "USDT", "BTC", "0.00001")
    assert exc.value.reason == "zero_credited_amount"


def test_insufficient_funds_rejected():
    lib = make_lib(customer={"BTC": "0.05", "USDT": "100"})
    lib.set_rates(RATES)
    with pytest.raises(BusinessRejection) as exc:
        # 本金 0.08 BTC（8000 USDT，未超限）+ 手续费超过客户余额 0.05
        lib.exchange("poor", "BTC", "USDT", "0.08")
    assert exc.value.reason == "insufficient_customer_funds"


def test_insufficient_reserve_rejected():
    lib = make_lib(reserve={"BTC": "100", "ETH": "1000", "USDT": "9000",
                            "USDC": "500000", "LOCAL": "100000"})
    lib.set_rates(RATES)
    with pytest.raises(BusinessRejection) as exc:
        lib.exchange("reserve-short", "BTC", "USDT", "0.1")  # 到账 10000 > 9000
    assert exc.value.reason == "insufficient_reserve"


# ---------- 幂等与冲突 ----------


def test_same_key_same_request_replays_original():
    lib = make_lib()
    lib.set_rates(RATES)
    first = lib.exchange("idem", "BTC", "USDT", "0.1")
    balances_after_first = {
        (k, c): lib.balance(k, c)
        for k in ("customer", "reserve", "fee") for c in CURRENCIES
    }
    # "1.0" 与 "1.00" 视为相同
    second = lib.exchange("idem", "BTC", "USDT", "0.100")
    assert second == first
    # 重试不重复记账
    for (kind, currency), text in balances_after_first.items():
        assert lib.balance(kind, currency) == text


def test_same_key_different_request_conflicts():
    lib = make_lib()
    lib.set_rates(RATES)
    lib.exchange("conflict", "BTC", "USDT", "0.1")
    snapshot = totals(lib)
    with pytest.raises(RequestConflictError):
        lib.exchange("conflict", "BTC", "USDT", "0.2")
    with pytest.raises(RequestConflictError):
        lib.exchange("conflict", "BTC", "ETH", "0.1")
    assert totals(lib) == snapshot  # 冲突不动资金


def test_replay_after_rate_change_returns_original_receipt():
    lib = make_lib()
    lib.set_rates(RATES)
    first = lib.exchange("rate-replay", "BTC", "USDT", "0.1")
    assert first.rate_version == 1
    # 换价后重放仍返回原回执
    lib.set_rates({"BTC": "50000", "ETH": "1500", "USDC": "1", "LOCAL": "3.6"})
    replayed = lib.exchange("rate-replay", "BTC", "USDT", "0.1")
    assert replayed == first
    assert replayed.rate_version == 1
    assert replayed.credited == "10000"  # 按旧价，而非新价 5000


def test_committed_rejection_does_not_flip_after_rate_change():
    lib = make_lib()
    # BTC 价格 300000：0.1 BTC = 30000 USDT，超限被拒
    lib.set_rates({"BTC": "300000", "ETH": "3000", "USDC": "1", "LOCAL": "7.2"})
    with pytest.raises(BusinessRejection) as first:
        lib.exchange("flip", "BTC", "USDT", "0.1")
    assert first.value.reason == "value_limit_exceeded"
    # 换价使同样请求本可通过，重放仍须是原拒绝
    lib.set_rates(RATES)
    with pytest.raises(BusinessRejection) as second:
        lib.exchange("flip", "BTC", "USDT", "0.1")
    assert second.value.reason == first.value.reason
    # 资金未变
    assert lib.balance("customer", "BTC") == "2"
    assert lib.balance("reserve", "USDT") == "1000000"


def test_rejection_consumes_key_for_different_params():
    lib = make_lib()
    lib.set_rates(RATES)
    with pytest.raises(BusinessRejection):
        lib.exchange("rej-key", "BTC", "USDT", "3")
    with pytest.raises(RequestConflictError):
        lib.exchange("rej-key", "BTC", "USDT", "0.1")


# ---------- 异常不占用 key ----------


def test_rates_not_set_does_not_consume_key():
    lib = make_lib()
    with pytest.raises(RatesNotSetError):
        lib.exchange("early", "BTC", "USDT", "0.1")
    lib.set_rates(RATES)
    receipt = lib.exchange("early", "BTC", "USDT", "0.1")  # 同 key 可成功
    assert receipt.credited == "10000"


def test_invalid_amount_does_not_consume_key():
    lib = make_lib()
    lib.set_rates(RATES)
    with pytest.raises(InvalidAmountError):
        lib.exchange("bad-amount", "BTC", "USDT", "0.123456789")  # 超过 8 位小数
    with pytest.raises(InvalidAmountError):
        lib.exchange("bad-amount", "BTC", "USDT", "abc")
    receipt = lib.exchange("bad-amount", "BTC", "USDT", "0.1")
    assert receipt.principal == "0.1"


# ---------- 持久化与可见边界 ----------


def test_receipt_and_rejection_survive_reopen(tmp_path):
    db = str(tmp_path / "fx.db")
    lib = make_lib(db, customer={"BTC": "0.05", "USDT": "50000"})
    lib.set_rates(RATES)
    first = lib.exchange("persist", "BTC", "USDT", "0.01")
    with pytest.raises(BusinessRejection):
        lib.exchange("persist-rej", "BTC", "USDT", "0.08")
    lib.close()

    reopened = SettlementLibrary(db)
    assert reopened.exchange("persist", "BTC", "USDT", "0.01") == first
    with pytest.raises(BusinessRejection) as exc:
        reopened.exchange("persist-rej", "BTC", "USDT", "0.08")
    assert exc.value.reason == "insufficient_customer_funds"
    # 版本号跨重开单调递增
    assert reopened.set_rates(RATES) == 2
    reopened.close()


def test_set_rates_versions_monotonic():
    lib = make_lib()
    assert lib.set_rates(RATES) == 1
    assert lib.set_rates(RATES) == 2
    assert lib.set_rates({"BTC": "1", "ETH": "1", "USDC": "1", "LOCAL": "1"}) == 3


def test_exchange_uses_single_complete_rate_version():
    lib = make_lib()
    lib.set_rates(RATES)
    lib.set_rates({"BTC": "50000", "ETH": "1500", "USDC": "0.5", "LOCAL": "3.6"})
    receipt = lib.exchange("one-version", "BTC", "USDT", "0.1")
    # 全部价格来自版本 2：0.1 BTC = 5000 USDT，而非混用版本 1 的价格
    assert receipt.rate_version == 2
    assert receipt.credited == "5000"
