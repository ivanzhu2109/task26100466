# fxsettle — 本地换币结算库

基于 Python 标准库（`sqlite3` + `decimal`）的本地换币结算库，处理同一用户
两种币之间的钱包兑换。无第三方依赖，无服务进程，持久化只用一个 SQLite 文件。

## 币种

| 币种  | 小数位 |
|-------|--------|
| BTC   | 8      |
| ETH   | 18     |
| USDT  | 6      |
| USDC  | 6      |
| LOCAL | 2      |

所有金额在库内以最小单位（原子）的精确整数保存（SQLite TEXT 列存十进制整数
字符串，避免 ETH 18 位小数溢出 int64）。

## 快速开始

```python
from fxsettle import ExchangeLedger

balances = {
    "user":    {"BTC": "1", "ETH": "10", "USDT": "20000", "USDC": "5000", "LOCAL": "100000"},
    "reserve": {"BTC": "100", "ETH": "1000", "USDT": "1000000", "USDC": "1000000", "LOCAL": "1000000"},
    "fee":     {"BTC": "0", "ETH": "0", "USDT": "0", "USDC": "0", "LOCAL": "0"},
}

with ExchangeLedger("ledger.db", balances) as led:   # 仅首次建库需要 balances
    version = led.set_rates({"BTC": "60000", "ETH": "3000", "USDC": "1", "LOCAL": "0.14"})
    receipt = led.exchange("order-001", "USDT", "BTC", "100")
    print(receipt)
    # {"status": "ok", "rate_version": 1, "principal_atoms": 100000000,
    #  "fee_atoms": 100000, "credit_atoms": 166666, ...}
```

## API

- `ExchangeLedger(path, balances=None)`：打开或创建账本。首次创建时必须提供
  `user` / `reserve` / `fee` 三个账户全部五种币的初始余额（十进制字符串）；
  之后打开已有库时忽略该参数。
- `set_rates(rates) -> int`：整组替换各币种兑 USDT 价格（正十进制字符串，
  USDT 固定为 1，可省略），返回单调递增的版本号。
- `exchange(key, src, dst, amount) -> dict`：兑换并返回持久化回执。
  - `amount` 必须能精确表示为付款币种原子数，否则抛 `ValueError`（不占用 key）。
  - 业务拒绝（本金过小、超 10000 USDT 限额、到账为零、余额不足）作为
    `status="rejected"` 的回执持久化，重放永远返回原结果。
  - 同 key 同请求（`"1.0"` 与 `"1.00"` 视为相同）重试返回原回执；
    同 key 不同请求抛 `ConflictError` 且不动资金。
  - 执行异常整体回滚，不占用 key。
- `balance(account, currency) -> int`：查询账户余额（原子）。

## 结算规则

- 本金 >= 0.00001 个付款币种，且按本次汇率折算价值 <= 10000 USDT（限额不含手续费）。
- 一次结算只使用一个完整汇率版本，回执记录 `rate_version`。
- 到账 = 本金等值换算结果向目标币种原子向下取整，为零则拒绝。
- 手续费从付款钱包额外扣除（付款币种）：
  `min(ceil(本金原子/1000), floor(5 USDT 折算付款币原子))`，
  封顶不足一个原子时手续费为零。
- 资金变动：付款钱包 -(本金+手续费)，付款币储备 +本金，收款币储备 -到账，
  收款钱包 +到账，手续费账户 +手续费。余额变动、全部 5 条分录与回执在同一
  SQLite 事务（`BEGIN IMMEDIATE`）内提交，任一环失败全部回滚。
- 每个币种在 user/reserve/fee 三账户间的原子总量独立守恒，余额恒非负。

## 运行测试

```bash
python3 -m pytest tests/ -q
```

## 代码结构

- `fxsettle/currencies.py` — 币种与小数位定义
- `fxsettle/amounts.py` — 十进制字符串与原子整数的精确换算
- `fxsettle/ledger.py` — SQLite 账本：汇率版本、幂等键、分录、回执
- `fxsettle/errors.py` — `ConflictError` 等异常类型
- `tests/test_ledger.py` — 自动化测试
