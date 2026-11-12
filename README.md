# A 股账户级研究回测引擎

基于 NautilusTrader 的沪深股票与可转债研究回测内核，适用于现金多头、日线/分钟线、多证券共享资金的策略验证。

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

`state` 提供只读的 `cash`、`positions`、`sellable` 和 `history`。历史快照固定在回调时点，保存后不会随未来行情增长。

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

- [研究使用指南](docs/research-guide.md)：组合信号、调仓顺序、成本情景与运行归档。
- [数据契约](docs/data-contract.md)：字段、日期、生命周期和异常语义。
- [执行模型](docs/execution-model.md)：合成深度与价格路径假设。
- [本地 Tushare 验证](docs/local-tushare-validation.md)：本轮场景、复验命令和证据边界。
- [历史验证记录](docs/validation.md)：既有测试与修复记录。
- [环境说明](docs/environment.md)：安装、ABI、打包与 CI。
