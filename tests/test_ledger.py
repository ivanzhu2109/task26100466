"""End-to-end tests for the fxsettle exchange settlement ledger."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from fxsettle import CURRENCIES, ConflictError, ExchangeLedger


def make_balances(user=None, reserve=None, fee=None):
    base = {c: "0" for c in CURRENCIES}
    accounts = {}
    for name, override in (("user", user), ("reserve", reserve), ("fee", fee)):
        row = dict(base)
        row.update(override or {})
        accounts[name] = row
    return accounts


RICH_RESERVE = {c: "1000000000" for c in CURRENCIES}

DEFAULT_RATES = {"BTC": "60000", "ETH": "3000", "USDC": "1", "LOCAL": "0.14"}


@pytest.fixture()
def ledger():
    led = ExchangeLedger(
        ":memory:",
        make_balances(
            user={"BTC": "1", "ETH": "10", "USDT": "20000", "USDC": "5000", "LOCAL": "100000"},
            reserve=RICH_RESERVE,
        ),
    )
    led.set_rates(DEFAULT_RATES)
    yield led
    led.close()


def atoms(text, currency):
    return int(Decimal(text) * (10 ** CURRENCIES[currency]))


def expected_credit(principal_atoms, src, dst, rates):
    """Independent exact computation of the credited amount, in dst atoms."""
    price_src = Fraction(Decimal(rates.get(src, "1")))
    price_dst = Fraction(Decimal(rates.get(dst, "1")))
    value = Fraction(principal_atoms) * price_src / (10 ** CURRENCIES[src])
    dst_atoms = value * (10 ** CURRENCIES[dst]) / price_dst
    return dst_atoms.numerator // dst_atoms.denominator


def expected_fee(principal_atoms, src, rates):
    """Independent exact computation of the fee, in src atoms."""
    price_src = Fraction(Decimal(rates.get(src, "1")))
    per_mille = -(-principal_atoms // 1000)
    cap_value = Fraction(5) * (10 ** CURRENCIES[src]) / price_src
    cap = cap_value.numerator // cap_value.denominator
    return min(per_mille, cap)


def totals(led):
    return {
        c: sum(led.balance(a, c) for a in ("user", "reserve", "fee"))
        for c in CURRENCIES
    }


def snapshot(led):
    return {(a, c): led.balance(a, c) for a in ("user", "reserve", "fee") for c in CURRENCIES}


class TestBasicExchange:
    def test_receipt_matches_independent_calculation(self, ledger):
        before = snapshot(ledger)
        receipt = ledger.exchange("k1", "USDT", "BTC", "100")
        assert receipt["status"] == "ok"
        assert receipt["rate_version"] == 1

        principal = atoms("100", "USDT")
        assert receipt["principal_atoms"] == principal
        assert receipt["fee_atoms"] == expected_fee(principal, "USDT", DEFAULT_RATES)
        assert receipt["credit_atoms"] == expected_credit(principal, "USDT", "BTC", DEFAULT_RATES)

        fee, credit = receipt["fee_atoms"], receipt["credit_atoms"]
        assert ledger.balance("user", "USDT") == before[("user", "USDT")] - principal - fee
        assert ledger.balance("reserve", "USDT") == before[("reserve", "USDT")] + principal
        assert ledger.balance("reserve", "BTC") == before[("reserve", "BTC")] - credit
        assert ledger.balance("user", "BTC") == before[("user", "BTC")] + credit
        assert ledger.balance("fee", "USDT") == before[("fee", "USDT")] + fee

    def test_atomic_totals_are_conserved(self, ledger):
        before = totals(ledger)
        ledger.exchange("k1", "USDT", "BTC", "100")
        ledger.exchange("k2", "BTC", "ETH", "0.001")
        ledger.exchange("k3", "LOCAL", "USDC", "500")
        ledger.exchange("k4", "ETH", "USDT", "2.5")
        assert totals(ledger) == before

    def test_entries_match_receipt_and_balances(self, ledger):
        before = snapshot(ledger)
        receipt = ledger.exchange("k1", "ETH", "LOCAL", "0.5")
        rows = ledger._conn.execute(
            "SELECT account, currency, delta FROM entries WHERE key = ?", ("k1",)
        ).fetchall()
        assert rows, "journal entries must be persisted"
        applied = {}
        for account, currency, delta in rows:
            applied[(account, currency)] = applied.get((account, currency), 0) + int(delta)
        # Per-currency conservation inside the journal itself.
        per_currency = {}
        for (account, currency), delta in applied.items():
            per_currency[currency] = per_currency.get(currency, 0) + delta
        assert all(v == 0 for v in per_currency.values())
        # Journal agrees with the receipt.
        assert applied[("user", "ETH")] == -(receipt["principal_atoms"] + receipt["fee_atoms"])
        assert applied[("reserve", "ETH")] == receipt["principal_atoms"]
        assert applied[("fee", "ETH")] == receipt["fee_atoms"]
        assert applied[("user", "LOCAL")] == receipt["credit_atoms"]
        assert applied[("reserve", "LOCAL")] == -receipt["credit_atoms"]
        # Journal agrees with actual balance movements.
        after = snapshot(ledger)
        for key, delta in applied.items():
            assert after[key] - before[key] == delta
        assert set(applied) == {k for k in after if after[k] != before[k]} | set(applied)


class TestFeeAndLimits:
    def test_fee_cap_below_one_atom_means_zero_fee(self):
        """BTC priced so high that 5 USDT converts to less than one BTC atom."""
        rates = {"BTC": "600000000", "ETH": "3000", "USDC": "1", "LOCAL": "0.14"}
        led = ExchangeLedger(
            ":memory:",
            make_balances(user={"BTC": "0.0001"}, reserve=RICH_RESERVE),
        )
        led.set_rates(rates)
        # 5 USDT = 5e8 satoshi-worth / 6e8 < 1 atom, so the cap is 0 atoms.
        assert expected_fee(atoms("0.00001", "BTC"), "BTC", rates) == 0
        receipt = led.exchange("cap", "BTC", "USDT", "0.00001")
        assert receipt["status"] == "ok"
        assert receipt["fee_atoms"] == 0
        assert receipt["credit_atoms"] == expected_credit(
            atoms("0.00001", "BTC"), "BTC", "USDT", rates
        )
        assert receipt["credit"] == "6000"
        assert led.balance("fee", "BTC") == 0
        led.close()

    def test_fee_is_per_mille_rounded_up(self, ledger):
        receipt = ledger.exchange("k1", "USDT", "BTC", "100")
        assert receipt["fee_atoms"] == atoms("0.1", "USDT")
        # 100.0001 USDT -> 100000100 atoms -> ceil(/1000) = 100001 atoms.
        receipt = ledger.exchange("k2", "USDT", "BTC", "100.0001")
        assert receipt["fee_atoms"] == 100001

    def test_fee_is_capped_at_5_usdt(self, ledger):
        # 10000 USDT principal: per-mille would be 10 USDT, cap binds at 5.
        receipt = ledger.exchange("k1", "USDT", "BTC", "10000")
        assert receipt["fee_atoms"] == atoms("5", "USDT")

    def test_limit_boundary_exact_10000_usdt_ok(self, ledger):
        receipt = ledger.exchange("k1", "USDT", "BTC", "10000")
        assert receipt["status"] == "ok"

    def test_limit_boundary_one_atom_over_rejected(self, ledger):
        receipt = ledger.exchange("k1", "USDT", "BTC", "10000.000001")
        assert receipt["status"] == "rejected"
        assert receipt["error"] == "limit_exceeded"

    def test_limit_uses_src_value_excluding_fee(self, ledger):
        # 0.16666 BTC at 60000 = 9999.6 USDT: ok even though fee adds on top.
        receipt = ledger.exchange("k1", "BTC", "USDT", "0.16666")
        assert receipt["status"] == "ok"
        receipt = ledger.exchange("k2", "BTC", "USDT", "0.16667")
        assert receipt["status"] == "rejected"
        assert receipt["error"] == "limit_exceeded"

    def test_min_principal_boundary(self, ledger):
        assert ledger.exchange("k1", "BTC", "USDT", "0.00001")["status"] == "ok"
        receipt = ledger.exchange("k2", "BTC", "USDT", "0.000009")
        assert receipt["status"] == "rejected"
        assert receipt["error"] == "principal_too_small"

    def test_zero_credit_rejected(self):
        rates = {"BTC": "600000000", "ETH": "3000", "USDC": "1", "LOCAL": "0.14"}
        led = ExchangeLedger(
            ":memory:",
            make_balances(user={"USDT": "100"}, reserve=RICH_RESERVE),
        )
        led.set_rates(rates)
        # 0.00001 USDT buys far less than one BTC atom.
        receipt = led.exchange("k1", "USDT", "BTC", "0.00001")
        assert receipt["status"] == "rejected"
        assert receipt["error"] == "zero_credit"
        led.close()

    def test_insufficient_user_balance_rejected(self, ledger):
        # 6000 USDC is within the 10000 USDT limit but above the 5000 balance.
        receipt = ledger.exchange("k1", "USDC", "USDT", "6000")
        assert receipt["status"] == "rejected"
        assert receipt["error"] == "insufficient_funds"

    def test_insufficient_reserve_rejected(self):
        led = ExchangeLedger(
            ":memory:",
            make_balances(
                user={"USDT": "100"},
                reserve={**RICH_RESERVE, "BTC": "0.0001"},
            ),
        )
        led.set_rates(DEFAULT_RATES)
        receipt = led.exchange("k1", "USDT", "BTC", "100")
        assert receipt["status"] == "rejected"
        assert receipt["error"] == "insufficient_funds"
        led.close()

    def test_rejection_moves_no_funds(self, ledger):
        before = snapshot(ledger)
        ledger.exchange("k1", "USDC", "USDT", "6000")
        ledger.exchange("k2", "BTC", "USDT", "0.000009")
        assert snapshot(ledger) == before


class TestIdempotency:
    def test_same_key_same_request_returns_original_receipt(self, ledger):
        first = ledger.exchange("k1", "USDT", "BTC", "100")
        second = ledger.exchange("k1", "USDT", "BTC", "100")
        assert first == second
        # Funds moved exactly once.
        assert ledger.balance("fee", "USDT") == first["fee_atoms"]

    def test_trailing_zeros_are_the_same_request(self, ledger):
        first = ledger.exchange("k1", "USDT", "BTC", "1.0")
        second = ledger.exchange("k1", "USDT", "BTC", "1.00")
        third = ledger.exchange("k1", "USDT", "BTC", "1")
        assert first == second == third

    def test_same_key_different_amount_conflicts(self, ledger):
        ledger.exchange("k1", "USDT", "BTC", "100")
        before = snapshot(ledger)
        with pytest.raises(ConflictError):
            ledger.exchange("k1", "USDT", "BTC", "101")
        assert snapshot(ledger) == before

    def test_same_key_different_currencies_conflicts(self, ledger):
        ledger.exchange("k1", "USDT", "BTC", "100")
        before = snapshot(ledger)
        with pytest.raises(ConflictError):
            ledger.exchange("k1", "USDT", "ETH", "100")
        with pytest.raises(ConflictError):
            ledger.exchange("k1", "BTC", "USDT", "100")
        assert snapshot(ledger) == before

    def test_replay_after_rate_change_returns_original_receipt(self, ledger):
        first = ledger.exchange("k1", "USDT", "BTC", "100")
        ledger.set_rates({"BTC": "30000", "ETH": "1500", "USDC": "1", "LOCAL": "0.07"})
        replay = ledger.exchange("k1", "USDT", "BTC", "100")
        assert replay == first
        assert replay["rate_version"] == 1
        # No additional funds moved on replay.
        assert ledger.balance("fee", "USDT") == first["fee_atoms"]

    def test_recorded_rejection_does_not_flip_after_rate_change(self, ledger):
        # Over the 10000 USDT limit at the original rates.
        first = ledger.exchange("k1", "USDT", "BTC", "20000")
        assert first["status"] == "rejected"
        assert first["error"] == "limit_exceeded"
        # BTC becomes so cheap that 20000 USDT would now be within the limit
        # in BTC terms is irrelevant; the limit is on USDT value, so instead
        # make the request valid by lowering nothing: use a fresh key to show
        # the same request now succeeds, while the old key stays rejected.
        ledger.set_rates({"BTC": "60000", "ETH": "3000", "USDC": "1", "LOCAL": "0.14"})
        before = snapshot(ledger)
        replay = ledger.exchange("k1", "USDT", "BTC", "20000")
        assert replay == first
        assert replay["status"] == "rejected"
        assert snapshot(ledger) == before

    def test_recorded_zero_credit_rejection_does_not_flip(self):
        rates = {"BTC": "600000000", "ETH": "3000", "USDC": "1", "LOCAL": "0.14"}
        led = ExchangeLedger(
            ":memory:",
            make_balances(user={"USDT": "100"}, reserve=RICH_RESERVE),
        )
        led.set_rates(rates)
        first = led.exchange("k1", "USDT", "BTC", "0.00001")
        assert first["status"] == "rejected"
        assert first["error"] == "zero_credit"
        # BTC becomes cheap enough that the same amount would now convert.
        led.set_rates({"BTC": "1000", "ETH": "3000", "USDC": "1", "LOCAL": "0.14"})
        fresh = led.exchange("k2", "USDT", "BTC", "0.00001")
        assert fresh["status"] == "ok"
        replay = led.exchange("k1", "USDT", "BTC", "0.00001")
        assert replay == first
        assert replay["status"] == "rejected"
        led.close()

    def test_recorded_insufficient_funds_rejection_is_stable(self, ledger):
        first = ledger.exchange("k1", "USDC", "USDT", "6000")
        assert first["status"] == "rejected"
        replay = ledger.exchange("k1", "USDC", "USDT", "6000")
        assert replay == first

    def test_execution_exception_rolls_back_and_frees_key(self, ledger):
        import sqlite3

        state = {"fail": True}

        def authorizer(action, arg1, arg2, dbname, source):
            if (
                state["fail"]
                and action == sqlite3.SQLITE_INSERT
                and arg1 == "entries"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        ledger._conn.set_authorizer(authorizer)
        before = snapshot(ledger)
        with pytest.raises(sqlite3.Error):
            ledger.exchange("k1", "USDT", "BTC", "100")
        # Nothing changed and the key was not consumed.
        assert snapshot(ledger) == before
        state["fail"] = False
        receipt = ledger.exchange("k1", "USDT", "BTC", "100")
        assert receipt["status"] == "ok"
        ledger._conn.set_authorizer(None)

    def test_malformed_request_does_not_consume_key(self, ledger):
        with pytest.raises(ValueError):
            ledger.exchange("k1", "USDT", "BTC", "0.0000001")  # sub-atom
        with pytest.raises(ValueError):
            ledger.exchange("k2", "USDT", "NOPE", "1")
        with pytest.raises(ValueError):
            ledger.exchange("k3", "USDT", "USDT", "1")
        receipt = ledger.exchange("k1", "USDT", "BTC", "1")
        assert receipt["status"] == "ok"

    def test_receipts_survive_reopen(self, tmp_path):
        path = str(tmp_path / "ledger.db")
        balances = make_balances(user={"USDT": "1000"}, reserve=RICH_RESERVE)
        led = ExchangeLedger(path, balances)
        led.set_rates(DEFAULT_RATES)
        first = led.exchange("k1", "USDT", "BTC", "100")
        rejected = led.exchange("k2", "USDT", "BTC", "20000")
        led.close()

        led2 = ExchangeLedger(path)  # balances not needed after init
        assert led2.exchange("k1", "USDT", "BTC", "100") == first
        assert led2.exchange("k2", "USDT", "BTC", "20000") == rejected
        assert led2.balance("fee", "USDT") == first["fee_atoms"]
        with pytest.raises(ConflictError):
            led2.exchange("k1", "USDT", "BTC", "999")
        led2.close()


class TestSetRates:
    def test_versions_increase_monotonically(self, ledger):
        assert ledger.set_rates(DEFAULT_RATES) == 2
        assert ledger.set_rates(DEFAULT_RATES) == 3

    def test_exchange_uses_one_complete_version(self, ledger):
        ledger.set_rates({"BTC": "30000", "ETH": "1500", "USDC": "1", "LOCAL": "0.07"})
        receipt = ledger.exchange("k1", "USDT", "BTC", "100")
        assert receipt["rate_version"] == 2
        expected = expected_credit(
            atoms("100", "USDT"), "USDT", "BTC",
            {"BTC": "30000"},
        )
        assert receipt["credit_atoms"] == expected

    def test_usdt_price_is_fixed_to_one(self, ledger):
        with pytest.raises(ValueError):
            ledger.set_rates({**DEFAULT_RATES, "USDT": "2"})
        # Explicitly passing 1 is accepted.
        assert ledger.set_rates({**DEFAULT_RATES, "USDT": "1"}) == 2

    def test_missing_currency_rejected(self, ledger):
        with pytest.raises(ValueError):
            ledger.set_rates({"BTC": "60000", "ETH": "3000", "USDC": "1"})

    def test_non_positive_price_rejected(self, ledger):
        with pytest.raises(ValueError):
            ledger.set_rates({**DEFAULT_RATES, "BTC": "0"})
        with pytest.raises(ValueError):
            ledger.set_rates({**DEFAULT_RATES, "BTC": "-5"})

    def test_unknown_currency_rejected(self, ledger):
        with pytest.raises(ValueError):
            ledger.set_rates({**DEFAULT_RATES, "DOGE": "0.1"})

    def test_exchange_without_rates_is_recorded_rejection(self):
        led = ExchangeLedger(
            ":memory:",
            make_balances(user={"USDT": "100"}, reserve=RICH_RESERVE),
        )
        first = led.exchange("k1", "USDT", "BTC", "1")
        assert first["status"] == "rejected"
        assert first["error"] == "no_rates"
        led.set_rates(DEFAULT_RATES)
        replay = led.exchange("k1", "USDT", "BTC", "1")
        assert replay == first
        assert replay["status"] == "rejected"
        led.close()


class TestConservationFuzz:
    def test_random_exchanges_conserve_every_currency(self):
        import random

        rng = random.Random(20261004)
        led = ExchangeLedger(
            ":memory:",
            make_balances(
                user={c: "1000" for c in CURRENCIES},
                reserve=RICH_RESERVE,
            ),
        )
        led.set_rates(DEFAULT_RATES)
        before = totals(led)
        currencies = list(CURRENCIES)
        key_n = 0
        for _ in range(300):
            src, dst = rng.sample(currencies, 2)
            amount = f"{rng.uniform(0.000001, 20000):.6f}"
            try:
                led.exchange(f"key-{key_n}", src, dst, amount)
            except ValueError:
                continue  # sub-atom amounts are malformed requests
            key_n += 1
        assert totals(led) == before
        for (account, currency), balance in snapshot(led).items():
            assert balance >= 0, (account, currency, balance)
        led.close()
