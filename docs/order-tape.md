# 冻结日线订单清单

`prepare_order_tape(prepared, entries)` 将上游策略请求编译成同一进程内可重复使用的冻结订单清单（Order Tape）。`prepared` 必须是 `prepare_backtest` 创建的官方 `PreparedBacktest`，编译前会检查市场校验器兼容性。分钟行情不在本接口范围内。

```python
from engine import OrderIntent, TimedOrderIntent, prepare_order_tape, run_order_tape

tape = prepare_order_tape(prepared, [
    TimedOrderIntent(
        prepared.bars[0].end,
        OrderIntent('600000.SH', 'BUY', 100, limit_price='10.00', intent_id='buy-1'),
    ),
])
result = run_order_tape(prepared, tape)
```

示例要求使用日线行情，标的在市场快照中存在，价格满足该标的最小报价单位。`run_order_tape(prepared, tape, ...)` 使用原有 NautilusTrader 执行与账户机制回放请求；订单带编译本身不执行订单。

## 编译与冻结契约

每项必须是精确的官方 `TimedOrderIntent` 类型，内部请求必须是精确的官方 `OrderIntent` 类型。决策时间必须带时区，且对应上海时间 15:00，日期必须属于 prepared 的交易日历；当日所有标的均停牌时，也允许记录收盘决策。接受标准 `datetime.timezone` 和 `zoneinfo.ZoneInfo`，并统一成上海时区；拒绝自定义可变时区。可选 `earliest_execution` 遵守相同的时区要求，且不得早于决策时间；它是执行时间门槛，不是成交承诺。

工厂在返回前校验全部请求：标的存在、数量为正整数、方向为 BUY/SELL、限价有限且为正并符合报价单位、ID 非空且在整条订单带中唯一。价格接受标准 Decimal、字符串、整数或浮点数，并通过字符串表示复制为 Decimal。没有 ID 的请求即使内容相同，也分别保留。

请求按决策时间稳定排序，同一时刻保持输入顺序。工厂复制来源迭代器和条目：`entries` 为元组，`by_day` 为日期到元组的只读映射，仅包含有请求的日期。允许空订单带。`PreparedOrderTape` 只能通过工厂创建。

## 执行与时间边界

回放仍要求完整的 prepared 市场输入经过校验并保留其市场摘要。优化的是送入原生引擎的事件集合：每天开盘时，根据实际持仓与当时已经满足执行时间门槛的待执行请求选择标的。不会根据未来请求或未来持仓选择当天事件。

回放不调用研究信号回调。请求只在对应交易日的上海时间 15:00 加入待执行队列。实际持仓及当日请求涉及的开放证券收盘参考价，在收盘时更新；全部标的最近一次非零成交量也只在对应 Bar 结束后更新。开盘申报量约束取当时已经知道的历史量，不提前使用当日收盘价。成交容量仍沿用既有模型的“本 Bar 事后总量 × 参与率”假设，不能解释为盘前已知的真实容量。实际持仓、下单规则、成交、费用、公司行动和逐日账户核对复用原有执行机制。

`market_input_sha256` 将订单带绑定到完整市场快照；换用市场摘要不同的 prepared 输入会被拒绝。订单带的 `sha256` 同时包含市场绑定与排序后的请求，复用引擎的规范化摘要逻辑：表示同一时刻的不同时区写法、等值 Decimal 的不同小数位数产生相同摘要；更改同一时刻的请求顺序或市场输入会改变摘要。

结果清单（manifest）记录 `order_tape_sha256` 和 `decision_contract='daily-close-frozen-tape-v1'`。相同市场上的不同订单带可以有相同的 `input_sha256`，但其订单摘要不同。接口不提供磁盘序列化或 pickle 契约。

## 原始 CSV 示例

在引擎项目目录执行：

```powershell
python examples/replay_order_tape.py --bars examples/data/bars_daily.csv --rules examples/data/rules.csv --orders examples/data/orders_daily.csv --output daily-result.json
```

订单 CSV 必需列为 `decision_time,symbol,side,quantity`，可选列为 `limit_price,intent_id,earliest_execution`。时间必须是包含显式时区的 ISO 日期时间；数量必须是正整数字符串，单位为股。CSV 解析后仍经过完整订单带校验。

订单带只记录请求行为。交易规则、执行限制、实际成交、费用与账户状态共同决定回放结果。合成样例用于接口验收，不构成策略有效性、真实投资收益或实盘能力证据；本页不作性能提升承诺。
