# Research hardening implementation plan

执行方式：当前会话逐项实施；用户已授权直接同步 main。

**Goal:** 可追溯、费用一致、执行政策明确的低频研究回测。
**Architecture:** 保留原生撮合与三方对账，改进适配层边界。
**Tech Stack:** Windows x64 / Python 3.12 / NT 1.231.0 / PyArrow / pytest。

## Tasks

- [x] 费用与资产：tests/test_research_hardening.py 先复现自定义费率和混合资产异常，再修改 fee_model/backtest；扩展 T+0 的 gateway/ledger 独立语义与测试。
- [x] 执行：同开盘卖出先于买入、组合信号、逆向滑点；测试现金复用、无未来 Bar、限价约束、零滑点兼容。
- [x] 性能与归档：只读历史快照、输入内容哈希和环境清单；测试旧快照不增长、输入变化产生不同指纹。
- [x] 本地验证：engine/tushare_lake.py 只读加载；测试日历/价格/规则/公司行为缺口与单位；examples/validate_tushare_lake.py 运行真实场景并输出汇总与负载指标。
- [x] 交付：README 改写、数据与运行文档、Windows CI 和 wheel 资源；运行全套、CLI、安装包与真实数据验证，审查差异后提交推送并核查 origin/main。

每项依次执行 pytest 定向红灯、最小实现、绿灯；财务预期独立手算，不以同一计算函数同时生成预期和实际。
