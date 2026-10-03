"""币种定义：小数位（原子刻度）。"""

from .errors import UnknownCurrencyError

CURRENCIES: dict[str, int] = {
    "BTC": 8,
    "ETH": 18,
    "USDT": 6,
    "USDC": 6,
    "LOCAL": 2,
}

QUOTE_CURRENCY = "USDT"


def atom_scale(currency: str) -> int:
    """返回 1 个币种单位等于多少原子（10^decimals）。"""
    try:
        decimals = CURRENCIES[currency]
    except KeyError:
        raise UnknownCurrencyError(f"不支持的币种: {currency!r}") from None
    return 10**decimals
