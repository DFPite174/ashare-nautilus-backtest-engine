"""
ashare-nautilus-backtest-engine
NautilusTrader A 股事件驱动回测适配核心引擎
"""

from engine.market_rules import (
    DailyMarketRule,
    validate_rules,
    block_reason,
    round_order,
    calculate_price_limits,
    validate_price_collar,
    get_trading_state,
)
from engine.fee_model import (
    AShareFeeModel,
    ConvertibleBondFeeModel,
    calculate_expected_fee,
    get_ashare_rates_for_date,
    get_cb_rates_for_date,
)
from engine.golden_ledger import GoldenLedger
from engine.order_gateway import AShareOrderGateway, OrderValidationResult
from engine.backtest import BacktestBar, OrderIntent, ResearchState, BacktestResult, run_backtest, run_prepared_backtest
from engine.prepared import PreparedBacktest, prepare_backtest
from engine.comparison import compare_backtests
from engine.rebalance import RebalancePlan, PlannedAllocation, plan_rebalance
from engine.order_tape import TimedOrderIntent, PreparedOrderTape, prepare_order_tape
from engine.tape_replay import run_order_tape

__all__ = [
    "DailyMarketRule",
    "validate_rules",
    "block_reason",
    "round_order",
    "calculate_price_limits",
    "validate_price_collar",
    "get_trading_state",
    "AShareFeeModel",
    "ConvertibleBondFeeModel",
    "calculate_expected_fee",
    "get_ashare_rates_for_date",
    "get_cb_rates_for_date",
    "GoldenLedger",
    "AShareOrderGateway",
    "OrderValidationResult",
    "BacktestBar", "OrderIntent", "ResearchState", "BacktestResult", "run_backtest",
    "RebalancePlan", "PlannedAllocation", "plan_rebalance",
    "PreparedBacktest", "prepare_backtest", "run_prepared_backtest", "compare_backtests",
    "TimedOrderIntent", "PreparedOrderTape", "prepare_order_tape", "run_order_tape",
]
