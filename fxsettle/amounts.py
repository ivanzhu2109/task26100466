"""十进制字符串与原子整数之间的精确转换。"""

import re
from decimal import Decimal

from .currencies import CURRENCIES, atom_scale
from .errors import InvalidAmountError

_AMOUNT_RE = re.compile(r"\d+(\.\d+)?")


def parse_amount(text: str, currency: str) -> int:
    """把十进制字符串解析为原子整数；无法精确表示时抛 InvalidAmountError。

    "1.0" 与 "1.00" 解析为同一原子数。
    """
    scale = atom_scale(currency)  # 未知币种在此抛 UnknownCurrencyError
    if not isinstance(text, str) or not _AMOUNT_RE.fullmatch(text):
        raise InvalidAmountError(f"非法金额字符串: {text!r}")
    value = Decimal(text)
    atoms = value * scale
    if atoms != atoms.to_integral_value():
        raise InvalidAmountError(
            f"金额 {text!r} 无法精确表示为 {currency} 原子（小数位 {CURRENCIES[currency]}）"
        )
    return int(atoms)


def format_amount(atoms: int, currency: str) -> str:
    """把原子整数格式化为十进制字符串（去掉多余的尾零）。"""
    scale = atom_scale(currency)
    if atoms < 0:
        raise ValueError("原子数不能为负")
    units, frac = divmod(atoms, scale)
    if frac == 0:
        return str(units)
    decimals = CURRENCIES[currency]
    frac_text = f"{frac:0{decimals}d}".rstrip("0")
    return f"{units}.{frac_text}"
