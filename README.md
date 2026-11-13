# A 股账户级研究回测引擎

基于 NautilusTrader（NT）的 A 股规则与账户适配内核，面向上游研究筛出的候选策略，执行沪深现金多头、日线/分钟线、多证券共享资金的账户回放与独立核验。支持普通股及基础可转债交易，具体边界见 [能力矩阵](docs/capabilities.md)。

推荐分工：**列式数据与信号研究 → 快速账户初筛 → 本项目的 NT 执行回放与独立核验 → 未来实盘系统**。上游负责大范围因子计算、参数搜索和快速账户筛选；本项目负责规则、委托、成交与账本检查。未来实盘接入、风控与恢复需另行建设和验收；分层方案不表示现有自动化对接已完成。详见 [分层架构](docs/architecture.md)。

- **账户闭环**：原生订单与成交驱动资金、持仓变化，每日与独立参考账本核对。
- **交易约束**：股票 `T+1`、可转债 `T+0`、整手/零股、停牌、涨跌停和订单资金预留。
- **公司行为**：现金红利登记、应收与到账，送转及新增股份上市日管理。
- **可追溯结果**：成交、拒单、每日净值，以及输入内容哈希、策略参数和环境版本清单。

## 5 分钟跑通

> [!IMPORTANT]
> 完整送转功能的已验证环境为 **Windows x64 / Python 3.12 / NautilusTrader 1.231.0**。送转桥接使用固定 ABI，加载时校验二进制指纹。安装与维护见 [环境说明](docs/environment.md)。

```powershell
python -m pip install -e ".[test]"
python -m pytest tests -q
python examples/regular_backtest.py --bars examples/data/bars_daily.csv --rules examples/data/rules.csv --output daily-result.json
```

样例包含三天合成行情，输出原生成交与逐日对账结果，便于检查安装和接口。分钟样例每天一根 Bar：

```powershell
python examples/regular_backtest.py --bars examples/data/bars_minute.csv --frequency MINUTE --rules examples/data/rules.csv --output minute-result.json
```

## 最小接口

```python
from decimal import Decimal
from contracts import Instrument, Venue
from engine import OrderIntent, run_backtest
from engine.data import load_bars_csv, load_rules_csv

bars = load_bars_csv("examples/data/bars_daily.csv")
rules = load_rules_csv("examples/data/rules.csv")
days = sorted({day for symbol, day in rules})  # 样例日期；正式研究传权威交易日历

def signal(bar, state):
    if bar.end.date() == days[0]:
        return [OrderIntent(bar.symbol, "BUY", 100)]
    return []

result = run_backtest(
    [Instrument(symbol="600519.SH", exchange=Venue.SSE)],
    bars, rules, signal,
    initial_cash=Decimal("100000"),
    participation_rate=0.01,
    trading_days=days,
    strategy_id="example-buy",
    strategy_params={"quantity": 100},
)
print(result.to_dict())
```

`state` 提供只读的 `cash`、`positions`、`sellable` 和 `history`，以及 `as_of`、`available_cash`、`reserved_cash`、`receivables`、`nav`、`valuation_prices`、`valuation_times`。历史快照固定在回调时点，保存后不会随未来行情增长；各证券估值时间可能不同，详见 [账户快照契约](docs/account-contract.md)。

`OrderIntent` 可附带运行内唯一的 `intent_id` 和带时区的 `earliest_execution`；时间门槛不改变 IOC/DAY 语义，详见 [意图契约](docs/order-intents.md)。已有目标权重可通过纯函数 `plan_rebalance` 生成可检查的普通股数量计划，再由调用者交给回放；它按当前快照估算费用和现金，不提交订单，详见 [离线调仓适配](docs/rebalance.md)。

### 信号与执行

| 接口/意图 | 执行行为 |
| --- | --- |
| `signal(bar, state)` | 每根 Bar 结束回调，意图最早在该证券下一根 Bar 开始执行 |
| `portfolio_signal(closed_bars, state)` | 同一结束时刻的输入 Bar 全部到齐后回调一次；与 `signal` 二选一 |
| 无 `limit_price` | 观察下一 Bar 开盘报价，提交该价及配置滑点范围内的 IOC 限价单；余量取消 |
| 显式 `limit_price` | DAY 限价单；开盘允许价格改善，盘中触价按委托限价成交；日终撤单 |
| 同一开盘时点的组合订单 | 先卖后买；已成交卖单回款可用于买入，买单按证券代码和意图顺序处理 |
| 买单资金预留 | 预留本金与预计费用，部分成交重算，终态释放；不足时记录 `INSUFFICIENT_CASH_WITH_FEES` |
| 样本末尾的意图 | 保留到 `pending_intents`，标记 `END_OF_DATA` |

组合信号、费用配置、滑点压力测试示例见 [研究使用指南](docs/research-guide.md)。

### 日频重复实验

上游已经给出明确数量的调仓指令时，使用 `prepare_order_tape(...)` 冻结决策时间与意图，再调用 `run_order_tape(prepared, tape)`。此入口专注执行与账户核验，每天只向 NT 投递实际持仓和已知待执行标的的行情；完整市场输入仍先校验并保留指纹。它不调用研究信号函数，详见 [冻结订单回放](docs/order-tape.md)。

```powershell
python examples/replay_order_tape.py --bars examples/data/bars_daily.csv --rules examples/data/rules.csv --orders examples/data/orders_daily.csv --output daily-result.json
```

通过 `prepare_backtest(...)` 固定校验后的输入，再用 `run_prepared_backtest(prepared, portfolio_signal=..., data_mode="sparse")` 运行候选；每轮重新建立账户和订单状态。完整用法见 [准备数据复用](docs/prepared-data.md)。

| `data_mode` | 数据处理方式 |
| --- | --- |
| `full`（默认） | 全量装载合成事件，保留原调用行为 |
| `session` | 按交易日流式装载相同合成事件，支持日线与分钟线 |
| `sparse` | 仅日线；为已知待执行意图保留完整合成路径，其余证券保留清簿事件和 Bar；全市场信号、历史与估值继续处理 |

模型与时钟边界见 [数据投递模式](docs/data-delivery.md)，共同契约下的首差定位见 [结果比较](docs/result-comparison.md)。

```powershell
python examples/regular_backtest.py --bars examples/data/bars_daily.csv --rules examples/data/rules.csv --data-mode sparse --output daily-result.json
```

### 成交规则速查

- **成交量预算**：每根 Bar 共享 `floor(volume × participation_rate)`，买卖及所有订单共同消耗；申报量另受上一根非零 Bar 已知成交量约束。
- **价格与状态**：按历史证券日规则校验；涨停买入、跌停卖出保守阻断；进入 `SUSPENDED` / `DAY_BLOCKED` 的待提交意图在当日拒绝。
- **撮合路径**：模型 `bar-synthetic-l2-v1` 将 OHLC 转为 O→H→L→C 合成深度，由 NT 原生 L2 撮合。
- **滑点压力参数**：`slippage_bps=10` 表示成交价格向不利方向移动 10 个基点，按最小价位取整并受限价约束。默认值为 `0`。

## 输入数据契约

> [!NOTE]
> 成交、持仓估值使用**未复权价格**，分红送转单独输入。因子复权信号与资金回测的原始价格应分别管理。

| 数据 | 必需内容 |
| --- | --- |
| 行情 | 明确时区的 `start/end`、原始 OHLC、以股/张计的整数成交量；分钟 Bar 位于连续交易时段内 |
| 日历与规则 | 权威 `trading_days`；每个存续证券交易日的状态、官方价格边界与数量规则；`OPEN` 缺行情时报错 |
| 生命周期 | `list_date` / `delist_date` 使用经确认的生命周期语义；边界日由历史日规则确定可交易性 |
| 公司行为 | 登记日、除权日、派息日、新股上市日及税前每股权益；零碎股显式采用 `reject` 或 `cash_in_lieu` |

完整字段、单位、缺口处理和公司行为映射见 [数据契约](docs/data-contract.md)。

本地 Tushare Parquet 湖可直接只读接入，使用官方日历、涨跌停和显式停牌记录：

```powershell
python examples/validate_tushare_lake.py --root "C:\path\to\Tushare_DB" --output local-validation.json --benchmark
```

该命令运行固定工程场景并输出摘要、输入指纹和负载记录。数据准备与复验见 [本地 Tushare 验证](docs/local-tushare-validation.md)。

## 输出与日终对账

| 字段 | 内容 |
| --- | --- |
| `fills` / `orders` | 原生成交与委托终态 |
| `rejections` / `pending_intents` | 拒单原因、期末未执行意图 |
| `native_cash` / `ledger_cash` / `positions` | 账户资金与期末持仓 |
| `daily_nav` / `account_journal` | 每日净值、应收红利及公司行为现金日志 |
| `manifest` / `execution_model` | 输入/运行哈希、策略参数、费用政策、滑点、源码指纹和环境版本 |

每日核对以下关系，不一致时终止并报告差异：

```text
原生现金 = 参考现金
原生持仓 = 网关持仓 = 参考持仓
原生现金 + 持仓估值 + 独立应收 = 参考 NAV
```

## 技术栈与适用边界

本项目使用 OHLC 派生合成深度，不重建真实 L2。已完成一次 500 股、5 年的本地工程运行；该记录不构成全市场分钟容量、多人并发或生产服务等级（SLA）承诺。历史版本时点一致性（vintage PIT）、真实转债数据回放与实盘尚未验收，完整状态见 [能力矩阵](docs/capabilities.md)。

| 维度 | 当前能力与使用条件 | 待扩展或验证范围 |
| --- | --- | --- |
| 技术栈 | NT 1.231.0（Python + Cython/Rust 核心）及 Python 规则/账本层；固定 Windows 送转 ABI | 跨平台送转构建、依赖升级迁移 |
| 策略与资产 | 沪深现金多头；股票 `T+1`、转债 `T+0` 的基础交易回测 | 融资融券、衍生品保证金、转股/赎回/到期结算 |
| 成交模型 | Bar 合成深度、固定价格路径、共享量预算、可配置逆向滑点 | 真实盘口队列、集合竞价、盘中临停重建、校准后的市场冲击 |
| 费用与权益 | 内置日期费率或自定义费率；税前分红、送转、零碎股显式政策 | 个人差别化股息税、配股、要约收购、退市清算、券商账单逐笔校准 |
| 数据质量 | 输入检查、显式日历、来源及内容指纹 | 历史 vintage PIT、分钟完整性、历史股票池与所有公司行为的上游治理 |
| 规模与运行 | 单次共享现金组合；已测规模与结果见验证文档 | 全市场多年容量、多人服务负载、生产 SLA、券商接入及重启恢复 |
| 证据含义 | 测试、真实供应商数据回放和账户对账各自记录 | 策略 alpha、实盘成交可实现性和生产验收需独立验证 |

## 延伸文档

- [活动撤单与规划复核](docs/active-cancellation-validation.md)：2.5.3 日终撤单过滤、340 项测试及不继续扩大低收益优化的依据。
- [湖输入内存验收](docs/lake-memory-validation.md)：2.5.2 生产湖分批读取、339 项测试及同数据前后三次测量。
- [必要优化验收](docs/essential-validation.md)：2.5.1 摘要标量路径、332 项测试及宽持仓密集订单回归。
- [数据准备优化验收](docs/preparation-validation.md)：2.5.0 共享规则结构、日期与摘要优化、327 项测试及相同输入复测。
- [冻结订单专用回放验收](docs/order-tape-validation.md)：2.4.0 固定指令回放、315 项测试、计时边界及未达到的准备/内存门槛。
- [分层改造第二批验收](docs/layered-batch2-validation.md)：2.3.0 输入复用、稀疏原型、财务一致性及性能门槛。
- [分层改造第一批验收](docs/layered-batch1-validation.md)：2.2.0 接口、完整测试、500 股五年三次复测及尚未达到的性能目标。

- [分层架构](docs/architecture.md)：研究、初筛、执行核验与未来实盘的职责。
- [能力矩阵](docs/capabilities.md)：普通股、基础可转债及证据边界。
- [账户快照契约](docs/account-contract.md)：可用资金、应收、估值时间与日终核验。
- [准备数据复用](docs/prepared-data.md)：一次校验、输入指纹及跨轮账户隔离。
- [冻结订单回放](docs/order-tape.md)：上游指令的日线执行核验、时间门槛及 CSV 示例。
- [数据投递模式](docs/data-delivery.md)：完整、逐日流式与日线稀疏事件路径。
- [结果比较](docs/result-comparison.md)：相同输入与模型条件下的业务首差。
- [意图契约](docs/order-intents.md)：字段校验、唯一标识与最早执行时间。
- [离线调仓适配](docs/rebalance.md)：目标权重到数量计划及费用/成交假设。
- [研究使用指南](docs/research-guide.md)：组合信号、调仓顺序、成本情景与运行归档。
- [数据契约](docs/data-contract.md)：字段、日期、生命周期和异常语义。
- [执行模型](docs/execution-model.md)：合成深度与价格路径假设。
- [本地 Tushare 验证](docs/local-tushare-validation.md)：本轮场景、复验命令和证据边界。
- [历史验证记录](docs/validation.md)：既有测试与修复记录。
- [环境说明](docs/environment.md)：安装、ABI、打包与 CI。
