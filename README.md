# fxsettle — 本地换币结算库

用 Python + SQLite 实现的本地换币结算库，处理同一用户两种币的钱包兑换。
一次结算要么完整改变资金并留下回执，要么完全不改变资金；任何重试都不会
重复记账。

## 币种

| 币种 | 小数位 | 币种 | 小数位 |
|------|--------|------|--------|
| BTC  | 8      | USDC | 6      |
| ETH  | 18     | LOCAL| 2      |
| USDT | 6（报价基准，价格固定为 1） | |

所有金额在库内以「原子」（最小单位）整数精确计算；因为 ETH 原子数可超出
SQLite 64 位整数，原子数在库内以 Python 大整数运算、在库表中以 TEXT 存储。

## 接口

```python
from fxsettle import SettlementLibrary, BusinessRejection

lib = SettlementLibrary(
    "fx.db",
    customer={"BTC": "2", "USDT": "50000"},      # 客户钱包初始余额
    reserve={"BTC": "100", "USDT": "1000000"},   # 各币种兑换储备
    fee_account={},                               # 手续费账户（缺省为 0）
)

version = lib.set_rates({"BTC": "100000", "ETH": "3000",
                         "USDC": "1", "LOCAL": "7.2"})  # 返回 1，单调递增

receipt = lib.exchange("order-001", "BTC", "USDT", "0.1")
# Receipt(key='order-001', rate_version=1, src='BTC', dst='USDT',
#         principal='0.1', fee='0.00005', credited='10000')
```

- `set_rates(rates)`：整组替换各币种兑 USDT 的价格（正十进制字符串，
  USDT 固定为 1，可省略），返回单调递增的版本号。
- `exchange(key, src, dst, amount)`：兑换指定本金，返回持久化 `Receipt`。
  `amount` 必须能精确表示为付款币种原子数（`"1.0"` 与 `"1.00"` 等价）。
- `balance(kind, currency)`：查询 `customer` / `reserve` / `fee` 账户余额。
- `ledger_entries(key)`：查询某笔回执的全部分录。

数据库已存在账户时，构造函数忽略初始余额参数，可直接
`SettlementLibrary("fx.db")` 重新打开。

## 结算规则

- 本金 ≥ 0.00001 个付款币种；按本次汇率折算的价值 ≤ 10000 USDT
  （限额不含手续费）。一次结算只使用一个完整汇率版本。
- 到账额 = 本金等值换算结果向目标币种原子向下取整；为零则拒绝。
- 手续费从付款钱包额外扣除（付款币种），取两者较小值：
  本金千分之一向上取整；5 USDT 换算为付款币种后向下取整。
  因此封顶不足一个原子时手续费为零。
- 一次结算的资金变动：付款钱包 −(本金+手续费)，付款币种储备 +本金，
  收款币种储备 −到账额，收款钱包 +到账额，手续费账户 +手续费。
  任一参与账户余额不足则拒绝。

## 幂等与一致性

- `key` 全库唯一。同 key 同参数重试返回原结果（成功回执或原业务拒绝，
  换价、余额变化都不会使已提交的结果翻转）；同 key 不同参数抛
  `RequestConflictError` 且不动资金。
- 业务拒绝（`BusinessRejection`，含 `reason`）会持久化并占用 key；
  参数错误与执行异常不占用 key，重试可成功。
- 余额变化、全部分录、回执在同一 SQLite 事务（`BEGIN IMMEDIATE`）内
  提交，具有同一可见边界；异常整体回滚。
- 每个币种的原子总量在客户、储备、手续费账户间独立守恒；所有账户
  余额始终非负。

## 运行测试

```bash
python3 -m pytest tests/ -q
```

测试覆盖：手续费封顶不足一原子的零手续费兑换与限额边界（用独立的
`Fraction` 精确计算核对）、换价后重放仍返回原回执、已提交业务拒绝不因
换价翻转、原子守恒、回执与分录一致、key 冲突、异常不占 key、
持久化重开等 18 个用例。
