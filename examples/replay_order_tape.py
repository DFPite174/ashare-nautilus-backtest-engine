"""Replay frozen daily CSV orders; synthetic example returns are not investment evidence."""
import argparse
import csv
import json
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contracts import Instrument, Venue
from engine import OrderIntent, TimedOrderIntent, prepare_backtest, prepare_order_tape, run_order_tape
from engine.data import load_bars_csv, load_rules_csv


def _aware_time(value):
    if 'T' not in value and ' ' not in value:
        raise ValueError('Order timestamps require an ISO datetime and explicit timezone')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('Order timestamps require an explicit timezone')
    return result


def load_order_csv(path):
    """Parse exact string IDs and integer shares; the tape compiler validates intent semantics."""
    required = {'decision_time', 'symbol', 'side', 'quantity'}
    optional = {'limit_price', 'intent_id', 'earliest_execution'}
    result = []
    with open(path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f, strict=True)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)) or not required.issubset(headers) or set(headers) - required - optional:
            raise ValueError('Order CSV requires decision_time,symbol,side,quantity and only supported optional columns')
        try:
            for row in reader:
                try:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError('Wrong number of order columns')
                    if any(not row[key] for key in required):
                        raise ValueError('Required order values cannot be blank')
                    if not re.fullmatch(r'[0-9]+', row['quantity']) or int(row['quantity']) <= 0:
                        raise ValueError('quantity must be a positive integer number of shares')
                    intent = OrderIntent(
                        row['symbol'], row['side'], int(row['quantity']),
                        Decimal(row['limit_price']) if row.get('limit_price') else None,
                        row.get('intent_id') or None,
                        _aware_time(row['earliest_execution']) if row.get('earliest_execution') else None,
                    )
                    result.append(TimedOrderIntent(_aware_time(row['decision_time']), intent))
                except (ValueError, InvalidOperation) as exc:
                    raise ValueError(f'Order CSV line {reader.line_num}: {exc}') from exc
        except csv.Error as exc:
            raise ValueError(f'Order CSV line {reader.line_num}: {exc}') from exc
    return tuple(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bars', 'rules', 'orders', 'output'):
        parser.add_argument(f'--{name}', type=Path, required=True)
    args = parser.parse_args()
    bars = load_bars_csv(args.bars, frequency='DAY')
    rules = load_rules_csv(args.rules)
    instruments = []
    for symbol in sorted({bar.symbol for bar in bars}):
        if not symbol.endswith(('.SH', '.SZ')):
            raise ValueError('Use Shanghai .SH or Shenzhen .SZ symbols')
        instruments.append(Instrument(symbol=symbol, exchange=Venue.SSE if symbol.endswith('.SH') else Venue.SZSE))
    prepared = prepare_backtest(instruments, bars, rules, trading_days=sorted({day for _, day in rules}))
    tape = prepare_order_tape(prepared, load_order_csv(args.orders))
    result = run_order_tape(prepared, tape, initial_cash=Decimal(100000), participation_rate=.1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{len(result.fills)} native fills; {len(result.daily_nav)} reconciled sessions; cash={result.native_cash}; output={args.output}')


if __name__ == '__main__':
    main()
