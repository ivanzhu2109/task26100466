"""异常类型定义。"""


class FxError(Exception):
    """库内所有异常的基类。"""


class RequestConflictError(FxError):
    """同一 key 提交了不同参数的请求。"""


class BusinessRejection(FxError):
    """业务规则拒绝（限额、余额不足、到账为零等）。

    拒绝结果已持久化：同一 key 重试将抛出携带同一 reason 的同一异常。
    """

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"{key}: {reason}")


class UnknownCurrencyError(FxError, ValueError):
    """使用了不支持的币种。"""


class InvalidAmountError(FxError, ValueError):
    """金额字符串无法精确表示为原子数。"""


class InvalidRateError(FxError, ValueError):
    """汇率不是正十进制字符串或缺少币种。"""


class RatesNotSetError(FxError):
    """尚未设置任何汇率就发起兑换。"""


class ConsistencyError(FxError):
    """内部不变量被破坏（实现缺陷）；触发时事务整体回滚。"""
