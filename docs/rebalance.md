# 离线目标权重适配

`engine.rebalance.plan_rebalance`（也从 `engine` 导出）把已有 `contracts.models.TargetWeights` 转为可检查的 `RebalancePlan`，复用 `engine.intents.OrderIntent`。它是纯计划函数，不提交订单，也不修改账户。

```python
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from contracts.models import TargetWeights
from engine.market_rules import DailyMarketRule
from engine.rebalance import plan_rebalance

tz = ZoneInfo('Asia/Shanghai')
target = TargetWeights(signal_date='2026-11-11', execution_date='2026-11-12',
                       weights={'600000.SH': 1.0})
plan = plan_rebalance(
    target, cash=Decimal('2005.02'), holdings={}, sellable={},
    prices={'600000.SH': Decimal('10')},
    rules={'600000.SH': DailyMarketRule('OPEN', 'UNBOUNDED')},
    valuation_at=datetime(2026, 11, 11, 15, tzinfo=tz),
    decision_at=datetime(2026, 11, 11, 16, tzinfo=tz),
    cash_reserve=Decimal('0'),
)
assert plan.intents[0].quantity == 200
assert plan.estimated_fees == Decimal('5.02')
assert plan.estimated_cash == 0
assert plan.intents[0].earliest_execution.isoformat() == '2026-11-12T09:30:00+08:00'
```

输入描述人民币、现金多头的普通股账户。`cash` 不包括应收及预留/待用资金；持仓和可卖量为整数股，可卖量应已扣除 T+1、公司行为和未结订单锁定。未提供的可卖证券视为零。调用者须先处理已有未结订单，再取此快照；目标未列出的持仓目标为零。每个持仓及目标证券都必须提供正且有限的价格和显式 `DailyMarketRule`；持仓缺价会报错，避免低估账户权益。

全部价格共用显式带时区的 `valuation_at`，不得晚于 `decision_at`；决策的上海日期须位于信号日至最早执行日之间。调用者负责历史来源与可得时间证据，时间戳本身不能证明时点一致性（PIT）或价格新鲜度。分证券估值时间不同的账户快照不能仅靠设置统一时间戳就变成同步价格快照。

可选 `receivables` 默认为零，用于传入快照时已确认但尚未到账的应收款（例如应收股息），必须有限且非负。初始权益与费后权重分母均包含该金额，但它不增加买入可用现金，也不计入 `estimated_cash`；输出 `RebalancePlan.receivables` 保留原值。例如现金 1005.01、应收 1000、无持仓、股价 10、目标权重 100% 时，权益为 2005.01、目标 200 股，但只能计划买 100 股，费用 5.01，剩余现金为零。应收到账后由真实账户状态把金额转入现金，再重新计划，避免重复计入。

目标股数按费前权益和快照价格向下取整。计划复用 `round_order` 的最小申报、数量步长、单笔上限和整笔零股清仓规则；每个证券最多一笔意图，先卖后买，两组内均按证券代码排序。顺序分配是确定性的，不是组合优化；可卖锁定也可能使部分可卖的零股无法卖出。

费用复用 `calculate_expected_fee`，默认取目标执行日的普通股日期费率，每笔意图估算一次最低佣金。买入支付能力同时扣除费用与显式额外 `cash_reserve`；储备是买入支出下限，初始账户低于储备时不会创造现金。卖出回款用于后续买入的估算，条件是此前每笔卖单均按给定快照价格全部成交；提交依赖买单前应按实际账户与成交状态重新计划。

每个 allocation 保留请求权重、向下取整后的目标股数、计划股数和估计费后权重，可据此检查取整和费用偏离。约束原因包括 `UNSELLABLE`、`UNITS`（含单笔上限）、`INACTIVE`（非开放状态或边界不可用）、`CASH_SHORTAGE`。输出还保留带日期的完整目标、两个时间戳、初始权益、储备、估计费用和剩余现金。

可通过可选 `fee_model` 传入与回放相同配置的 `AShareFeeModel`，使用公开的 `rates_for(symbol, trade_date)`，包括自定义最低佣金；返回费率必须有限且非负。本适配器仅声明普通股账户能力，不做拆单优化。

意图携带数量而非可执行价格承诺（`limit_price=None`）。每笔 `earliest_execution` 取 `target.execution_date` 上海时间 09:30 与 `decision_at` 的较晚者，由引擎时间门槛防止更早 Bar 消耗未来目标。这只是最早时间，不证明该日开市或当时有报价；调用者仍提供交易日历和实际后续行情。

无效或非有限的 BOUNDED 价格边界会被拒绝，但当前估值在涨停价不代表未来未知开盘一定阻断买入。这里不模拟滑点、未来价格、交易日历、成交量/队列、执行时价格边界或成交。OPEN 规则不保证按边界或估值价格成交；下游引擎和网关仍须按实际执行环境检查。这不是券商接入或生产验收。
