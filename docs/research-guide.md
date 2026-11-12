# 低频研究使用指南

## 组合信号

同一结束时刻的所有输入 Bar 到齐后，`portfolio_signal` 回调一次；缺失证券不会自动补入。正式研究在上游验证预期股票池与行情完整性。

```python
def portfolio_signal(closed_bars, state):
    # 使用本时点已结束行情计算目标数量。
    return []

result = run_backtest(
    instruments, bars, rules,
    portfolio_signal=portfolio_signal,
    trading_days=trading_days,
    corporate_actions=corporate_actions,
    strategy_id="weekly-rotation",
    strategy_params={"holding_count": 20},
    data_snapshot_id=snapshot_id,
)
```

`signal` 和 `portfolio_signal` 二选一。`closed_bars` 按证券代码排序，`state.history` 的每个序列是固定长度只读视图，历史保存后不会增长。访问切片返回 tuple，访问整段历史时自行控制成本。

## 组合执行顺序

同一 Bar 起始时点，先观察所有开盘报价，再分阶段提交卖单和买单。模拟技术时间为开盘后 2/3 纳秒，用于确定事件顺序，不代表真实交易延迟。原生引擎先结算卖单成交，再处理买单。

只有已成交卖单产生可用回款。未成交卖单不能为买单融资。不同证券代码按排序处理，同证券保持意图顺序；同 Bar 买卖方向共享实际成交量与上一已知 Bar 的申报预算。待成交卖单与新买单仍可能在后续合成路径上竞争容量。

该政策模拟连续执行的组合调仓。若策略必须在同一次开盘集合竞价中同时申报且资金提前冻结，应采用更严格的资金预算并单独验证该执行方式。

## 费用与滑点

```python
from engine import AShareFeeModel

result = run_backtest(
    instruments, bars, rules, signal,
    fee_model=AShareFeeModel(commission_rate=0.0002, min_commission=3),
    slippage_bps=10,
    trading_days=trading_days,
)
```

费用模型通过 `rates_for(symbol, trade_date)` 向独立参考计算导出配置；原生收费与参考计算分别累计，不共享收费缓存。每次运行重置费用模型状态；同一个可变费用实例应串行使用。支持 `AShareFeeModel` 及其 `ConvertibleBondFeeModel` 子类，转债专用模型用于纯转债组合。

内置佣金和转债费用是默认研究参数，实际使用以账户约定与历史制度为准。旧股票 API 中，税率显式填入基准 `0.0005` / `0.00001` 时仍按已编码日期匹配。模型覆盖的历史分界点见 `engine/fee_model.py`。

滑点向不利方向移动价格，按 tick 买入向上、卖出向下取整，并受订单限价保护。限价触价后按限价成交时，配置滑点不能突破该价格；因此滑点情景主要改变开盘成交的价格改善和无价格 IOC 的价格。它是一项确定性压力假设，实际价差、排队和冲击另行校准。

建议预先固定多个成本与参与率情景，比较收益、换手、拒单及未成交数量，不根据结果反向挑选最有利假设。

## 归档与复验

`result.manifest` 包含归一化输入内容 SHA-256、来源快照 ID、策略标识/参数/可读取的函数源码哈希、代码提交、Python 源码与送转二进制指纹、Python/NT 版本、费用政策、滑点与执行政策及运行哈希。

运行哈希不含随机原生订单 ID。调用方须显式填入策略参数和外部数据 ID，归档完整策略代码及依赖；函数闭包、全局变量、外部模型状态和凭据不被自动抓取。动态函数源码不可读取时，`strategy_source_sha256` 为 `null`，使用者应依赖策略版本与参数归档。

将结果 JSON、输入快照、策略源码和环境锁定文件一起保存。输入文件可保存在受控存储中，通过哈希对应；共享报告时按数据许可管理原始行情。
