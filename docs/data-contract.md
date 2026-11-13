# 输入数据契约

## 行情与日历

`BacktestBar(symbol, start, end, open, high, low, close, volume)` 为不可变记录。`start/end` 须携带显式时区，交易日统一按 `Asia/Shanghai` 解释。日线为 09:30–15:00；分钟线位于连续交易时段，不跨午休、不重叠。成交量为以股/张计的非负整数。

CSV 读取：`load_bars_csv(path, frequency="DAY", volume_multiplier=1)`。Tushare 日线 `vol` 以手计时显式使用 `volume_multiplier=100`；湖仓已有的 `volume_shares` 不再乘 100。分钟接口的时间标签和单位由接入方确认。

正式研究传入权威 `trading_days`，覆盖整个研究区间。省略时使用行情首末日内规则日期与 Bar 日期的并集；该模式只能发现已声明交易日的缺口。分钟内部缺口由上游检查。

## 证券日规则

`rules[(symbol, date)]` 每个存续证券、每个交易日一条：

| 字段 | 语义 |
| --- | --- |
| `trade_status` | `OPEN` / `SUSPENDED` / `DAY_BLOCKED` |
| `limit_state` | `BOUNDED` / `UNBOUNDED`；不可交易日可用 `UNAVAILABLE` |
| `down_limit` / `up_limit` | `BOUNDED` 时的历史官方边界 |
| `buy_min` / `buy_step` | 最小买入数量与数量步长 |
| `sell_min` / `sell_step` | 非清仓卖出的最小数量与步长 |
| `max_order` | 单笔申报最大数量 |
| `prev_close` / `issue_price` | 独立网关集合竞价范围检查的参考字段 |

`OPEN` 缺整日行情、价格越界或单位错误均明确失败。停牌可无 Bar；零量不能成交。停牌占位 Bar 会影响策略历史，数据集须统一表示方式。历史板块、ST、上市状态和边界以对应时点的事实为准。

## 生命周期

主数据允许 `list_date` / `delist_date`，区间外 Bar 和 `OPEN` 规则被拒绝。尚未上市或已离开样本的空仓证券不要求伪造行情。退市公告日期与最后交易日是不同语义，接入方须确认字段。持有退市遗留证券时应补充估值/清算政策后再运行相应研究。

## 公司行为

`CorporateAction` 使用 `record_date`、`ex_date`、`pay_date`、`share_listing_date`。现金红利为税前每股金额，登记日收盘锁定权益，除息确认应收、派息转为现金，采用分币 `ROUND_HALF_UP`。

送转同时更新原生持仓、网关和参考账本，新增股上市前不可卖。除权无行情时，结转估值单位按 `(上一估值价 - 每股现金红利) / (1 + 送转比例)` 调整。事件字段拼错或未知字段会报错。

公共回测遇到账户级非整数权益时，使用 `fractional_policy="reject"` 或明确的 `cash_in_lieu`。后者属于研究假设；可指定 `fractional_cash_price`。旧独立账本的 `floor` 模式保留作兼容接口。

## 本地 Tushare 湖

`engine.tushare_lake.load_tushare_lake(root, symbols, start, end)` 返回 `LakeInputs`，含 `instruments/bars/rules/trading_days/corporate_actions/snapshot`。

- 只读 Parquet，不导入数据库的 `config.py`，不读取 `.env`，不联网。
- 使用 `daily` 原始价、`volume_shares`，并与 `vol × 100` 交叉校验。
- 使用 `stk_limit` 官方边界；缺失边界时失败。无涨跌幅阶段应另行提供明确日规则。
- `suspend_d` 的 `S` 记录区分整日和盘中停牌；盘中停牌在此日线接入器中按 `DAY_BLOCKED` 处理。
- 数量规则采用当前明确编码的沪深普通股票基础模板；更早历史制度或特殊证券需由上游提供对应日规则。
- `dividend.cash_div_tax` 映射为税前每股红利，`stk_bo_rate/stk_co_rate` 映射为送股/转增；实施公告日期用于事件标识。只接收 `div_proc="实施"`；相同条款去重，冲突失败。
- 主档用于明确证券的上市日期检查，不用于从当前成分构造历史股票池；没有可靠的最后交易日时不推断退市状态。
- 每个读取文件的 SHA-256 写入 `snapshot`；内容哈希不构成历史版本 PIT 证明。
- 市场表按所需列分批扫描，临时行采用紧凑元组，转换后释放索引；最终仍返回完整 Python Bar 列表和规则字典。源文件指纹覆盖整个文件，分批不会跳过跨批重复与缺口检查。测量口径见[湖输入内存验收](lake-memory-validation.md)。
