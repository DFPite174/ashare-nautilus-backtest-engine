# 原生回测结果差分

`engine.comparison.compare_backtests` 比较两个 `BacktestResult`，或与其 `to_dict()` 相同结构的字典。第三方引擎需自行适配为这一原生业务契约；此模块不提供通用跨引擎导入器。

```python
from engine.comparison import compare_backtests

report = compare_backtests(reference_result, candidate_result)
assert report['comparable_model']
assert report['equal_within_compared_fields']
```

两侧必须提供非空且相同的 `manifest.input_sha256`。默认还要求顶层 `execution_model` 与 `manifest.config` 中的 `execution_model`、`execution_policy`、`initial_cash`、`participation_rate`、`slippage_bps`、`fee_schedules`、`symbol_fee_policy` 一致，否则抛出 `ValueError`。

有意比较不同执行或经济假设时，显式传入 `allow_model_difference=True`。报告会把每项差异及两侧值放进 `assumption_differences`，并设置 `comparable_model=False`；输入指纹仍必须相同。即使业务结果相等，这种情况也不表示经济模型等价。

比较范围：

- 成交按原有顺序比较 `symbol/side/quantity/price/fee/time`，并保留可选 `intent_id`。只忽略原生生成的 `order_id`，其他成交扩展字段不在比较范围。
- 拒绝记录按原有顺序比较 `symbol/reason/time` 和可选 `intent_id`。
- 每日净值比较整行字典，包括现金、持仓、可交易持仓、应收股息和原生账户核对字段等所有已提供的字段。不会投影丢弃未知字段；新增每日字段也可能构成差异。
- 最终原生现金、账本现金和所有最终持仓逐项比较。缺少某个持仓键与显式零持仓不同。

`fills`、`rejections`、`daily_nav` 各返回两侧数量、`count_difference`（候选减参考），以及 `first_difference`。首个差异包含零起始 `index` 与两侧整行（越界为 `None`）；无差异则为 `None`。最终余额差异在 `final_cash_differences` 和 `final_position_differences` 中列出两侧值。

结果 `schema_version` 不同会写入 `schema_difference`，只要必需业务字段完整，不会仅因版本不同拒绝比较。必需字段缺失或列表/持仓结构不合法时抛出 `ValueError`。每日必需字段为 `date/cash/positions_value/total_equity/receivable_dividends/holdings/tradable_holdings`。

数值遵循 `BacktestResult.to_dict()` 的序列化约定：`Decimal` 转为字符串，其余 JSON 兼容值原样保留。采用精确比较、不设容差，不自动把数值字符串转成数字，因此 `"1"`、`"1.0"`、数值 `1` 可能不同。调用方应统一序列化方式；标识符 `"001"` 和拒绝原因等文本不会被误当成数字。

`equal_within_compared_fields` 只说明上述比较字段相等；不比较订单状态、账户流水、待执行意图和全部运行元数据。两个结果一致并不能证明独立正确性、真实成交、历史复现或生产可用性。
