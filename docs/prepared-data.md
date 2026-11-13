# 重复使用已准备的市场数据

`prepare_backtest(instruments, bars, rules, *, corporate_actions=(), trading_days=None)`
将输入容器与调用方分离，一次完成市场规则、交易日历、行情覆盖和公司行为校验，
返回只能通过工厂创建的 `PreparedBacktest`。随后使用 `run_prepared_backtest`
重复运行策略，每轮重新创建引擎、账户、订单及流动性状态，并校验本轮资金、
参与率、滑点及策略回调配置。

下面的完整示例直接运行，不需要数据文件。两轮都是空仓策略，用于展示复用接口，
不代表策略收益或真实成交验证。

```python
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from contracts import Instrument, Venue
from engine import BacktestBar, prepare_backtest, run_prepared_backtest

day = date(2025, 5, 6)
tz = ZoneInfo("Asia/Shanghai")
instruments = [Instrument(symbol="600000.SH", exchange=Venue.SSE)]
bars = [BacktestBar(
    "600000.SH", datetime(2025, 5, 6, 9, 30, tzinfo=tz),
    datetime(2025, 5, 6, 15, 0, tzinfo=tz),
    Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), 10000,
)]
rules = {("600000.SH", day): {
    "trade_status": "OPEN", "limit_state": "UNBOUNDED",
    "buy_min": 100, "buy_step": 100, "sell_min": 100,
    "sell_step": 100, "max_order": 1000000,
}}
prepared = prepare_backtest(instruments, bars, rules, trading_days=[day])
del instruments, bars, rules  # 不再使用原始容器时释放引用，避免同时保留两份规则。

def hold_cash(bar, state):
    return []

first = run_prepared_backtest(prepared, hold_cash, initial_cash=Decimal("100000"), data_mode="sparse",
                              strategy_id="hold-cash", data_snapshot_id="example")
second = run_prepared_backtest(prepared, hold_cash, initial_cash=Decimal("200000"), data_mode="sparse",
                               strategy_id="hold-cash", data_snapshot_id="example")
assert first.manifest["input_sha256"] == second.manifest["input_sha256"] == prepared.input_sha256
assert first.ledger_cash == Decimal("100000")
assert second.ledger_cash == Decimal("200000")
```

证券和公司行为使用官方冻结模型重新校验并复制；官方冻结 `BacktestBar` 的不可变
标量字段可共享。序列变为元组，证券主表和规则映射（包括嵌套映射）变为只读映射
代理。规则中的自定义对象不受支持，时间戳仅接受标准库 `timezone` 或 `ZoneInfo`
时区，避免可变自定义时区改变已经校验的时间含义。准备后修改原始列表或规则不会
更新快照；需要使用新数据时重新准备。准备过程复制规则字典，初始常驻内存（RSS）
可能上升；若仍持有原始行或容器，两份规则会同时占用内存。因此大型任务若不再
使用原始行，应释放对原始容器的引用。准备不会把全部输入变成磁盘映射或按需读取。

示例明确选择仅支持日线的 `data_mode="sparse"`；两个运行入口的默认值仍为
`"full"`。分钟行情可选择 `"session"` 按交易日投递。三个模式的适用范围、
事件计数和边界见 [行情投递模式](data-delivery.md)，策略状态的历史保留约定见
[账户快照契约](account-contract.md)。

缓存的 `input_sha256` 与普通运行使用相同的规范化内容；未提供日历时，先由现有
校验器推导交易日再计算哈希。市场校验器源码指纹按函数身份缓存，校验器变化后
必须重新准备。准备快照不包含策略、资金、费率、订单或账户状态。

这是同一进程内的研究优化，不是磁盘或 Pickle 缓存，不会隐式加载数据文件缓存。
只读接口不是防御 Python 反射、`object.__setattr__` 或蓄意修改模型内部结构的安全
边界。调用方应通过公开只读属性使用快照。
