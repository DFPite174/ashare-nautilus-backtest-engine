"""Offline target-weight planning; estimated trades are never submitted here."""
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, time
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from zoneinfo import ZoneInfo

from contracts.models import TargetWeights
from engine.intents import OrderIntent
from engine.fee_model import calculate_expected_fee
from engine.market_rules import DailyMarketRule, round_order


@dataclass(frozen=True)
class PlannedAllocation:
    symbol: str
    target_weight: Decimal
    target_quantity: int
    planned_quantity: int
    estimated_weight: Decimal
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RebalancePlan:
    valuation_at: datetime
    decision_at: datetime
    target: TargetWeights
    initial_equity: Decimal
    cash_reserve: Decimal
    intents: tuple[OrderIntent, ...]
    allocations: tuple[PlannedAllocation, ...]
    estimated_fees: Decimal
    estimated_cash: Decimal
    receivables: Decimal = Decimal(0)


def _number(value, name, *, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid {name}") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"Invalid {name}")
    return result


def plan_rebalance(
    target: TargetWeights, *, cash: Decimal, holdings: Mapping[str, int],
    sellable: Mapping[str, int], prices: Mapping[str, Decimal],
    rules: Mapping[str, DailyMarketRule], valuation_at: datetime,
    decision_at: datetime, cash_reserve: Decimal, fee_model=None,
    receivables: Decimal = Decimal(0),
) -> RebalancePlan:
    """Plan equity quantities at one known price snapshot (see docs/rebalance.md).

    Sell proceeds fund later buys conditionally on complete fills at those prices.
    ``cash_reserve`` is extra cash retained after the estimated per-intent fees.
    """
    # Revalidate constructed models before accessing their dates or weights.
    if not isinstance(target, TargetWeights):
        raise ValueError('TargetWeights required')
    target = TargetWeights.model_validate(target.model_dump())
    for stamp in (valuation_at, decision_at):
        if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
            raise ValueError("Timezone-aware valuation_at and decision_at required")
    day = decision_at.astimezone(ZoneInfo('Asia/Shanghai')).date()
    if valuation_at > decision_at or not target.signal_date <= day <= target.execution_date:
        raise ValueError("Invalid valuation/decision/target chronology")
    earliest = max(decision_at, datetime.combine(target.execution_date, time(9, 30),
                                                ZoneInfo('Asia/Shanghai')))
    cash = _number(cash, 'cash')
    reserve = _number(cash_reserve, 'cash_reserve')
    receivables = _number(receivables, 'receivables')
    quantities = {}
    available = {}
    for values, output, name in ((holdings, quantities, 'holdings'), (sellable, available, 'sellable')):
        for symbol, value in values.items():
            number = _number(value, f'{name} {symbol}')
            if isinstance(value, bool) or number != number.to_integral_value():
                raise ValueError(f'Invalid integer {name} {symbol}')
            output[symbol] = int(number)
    for symbol, quantity in available.items():
        if quantity > quantities.get(symbol, 0):
            raise ValueError(f'Sellable exceeds holdings: {symbol}')
    symbols = sorted(set(target.weights) | set(quantities))
    px, rule_dict, fee_rates = {}, {}, {}
    for symbol in symbols:
        if symbol not in prices:
            raise ValueError(f'Missing price for {symbol}')
        px[symbol] = _number(prices[symbol], f'price {symbol}', positive=True)
        rule = rules.get(symbol)
        if not isinstance(rule, DailyMarketRule):
            raise ValueError(f'Missing DailyMarketRule for {symbol}')
        for name in ('buy_min', 'buy_step', 'sell_min', 'sell_step', 'max_order'):
            value = getattr(rule, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f'Invalid rule {name} for {symbol}')
        if rule.trade_status not in ('OPEN', 'SUSPENDED', 'DAY_BLOCKED'):
            raise ValueError(f'Invalid trade status for {symbol}')
        if rule.limit_state not in ('BOUNDED', 'UNBOUNDED', 'UNAVAILABLE'):
            raise ValueError(f'Invalid limit state for {symbol}')
        if rule.limit_state == 'BOUNDED':
            low = _number(rule.down_limit, f'down_limit {symbol}', positive=True)
            high = _number(rule.up_limit, f'up_limit {symbol}', positive=True)
            if low >= high:
                raise ValueError(f'Invalid price band for {symbol}')
        rule_dict[symbol] = asdict(rule)
        if fee_model is not None:
            rates = fee_model.rates_for(symbol, str(target.execution_date))
            fee_rates[symbol] = {key: _number(rates[key], f'{key} {symbol}') for key in
                                 ('commission_rate', 'min_commission', 'stamp_duty_rate', 'transfer_fee_rate')}
    equity = cash + receivables + sum((px[s] * q for s, q in quantities.items()), Decimal(0))
    if equity <= 0:
        raise ValueError('Positive total equity required')
    desired = {s: int((equity * Decimal(str(target.weights.get(s, 0))) / px[s])
                      .to_integral_value(rounding=ROUND_FLOOR)) for s in symbols}
    planned = {s: quantities.get(s, 0) for s in symbols}
    reasons = {s: [] for s in symbols}
    intents, fees = [], Decimal(0)
    remaining = cash
    for side in ('SELL', 'BUY'):
        for symbol in symbols:
            held = quantities.get(symbol, 0)
            delta = desired[symbol] - held
            if not delta or (delta > 0) != (side == 'BUY'):
                continue
            rule = rule_dict[symbol]
            if rule['trade_status'] != 'OPEN' or rule['limit_state'] == 'UNAVAILABLE':
                reasons[symbol].append('INACTIVE')
                continue
            bounded = delta
            if side == 'SELL':
                bounded = -min(-delta, available.get(symbol, 0))
                if bounded != delta:
                    reasons[symbol].append('UNSELLABLE')
            quantity = abs(round_order(bounded, held, rule)) if bounded else 0
            if quantity != abs(bounded):
                reasons[symbol].append('UNITS')
            def fee(q):
                return calculate_expected_fee(side, Decimal(q), px[symbol],
                                              trade_date=str(target.execution_date),
                                              rates=fee_rates.get(symbol)) if q else Decimal(0)
            if side == 'BUY' and quantity:
                # Binary search whole lot counts, including the minimum commission.
                low, high = 0, quantity // rule['buy_step']
                while low < high:
                    mid = (low + high + 1) // 2
                    q = mid * rule['buy_step']
                    if q * px[symbol] + fee(q) <= remaining - reserve:
                        low = mid
                    else:
                        high = mid - 1
                affordable = low * rule['buy_step']
                if affordable < rule['buy_min']:
                    affordable = 0
                if affordable < quantity:
                    reasons[symbol].append('CASH_SHORTAGE')
                quantity = affordable
            charge = fee(quantity)
            if side == 'SELL' and remaining + quantity * px[symbol] < charge:
                reasons[symbol].append('CASH_SHORTAGE')
                continue
            if quantity:
                intents.append(OrderIntent(symbol, side, quantity, earliest_execution=earliest))
                signed = quantity if side == 'BUY' else -quantity
                planned[symbol] += signed
                remaining -= signed * px[symbol] + charge
                fees += charge
    final_equity = equity - fees
    allocations = tuple(PlannedAllocation(
        s, Decimal(str(target.weights.get(s, 0))), desired[s], planned[s],
        planned[s] * px[s] / final_equity if final_equity else Decimal(0), tuple(reasons[s]),
    ) for s in symbols)
    return RebalancePlan(valuation_at, decision_at, target, equity, reserve,
                         tuple(intents), allocations, fees, remaining, receivables)
