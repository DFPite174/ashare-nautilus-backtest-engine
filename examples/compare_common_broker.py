"""Bounded independent native-broker check; not an A-share feature comparison."""
import argparse
from datetime import datetime
from decimal import Decimal as D, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import OrderIntent, run_backtest
from engine.tushare_lake import load_tushare_lake
from examples.validate_tushare_lake import check_cash


def compare_accounts(reference, candidate):
    """Compare this experiment's dated fills/accounts; tolerate sub-cent float noise only."""
    first = None
    for name in ('fills', 'daily'):
        left, right = reference[name], candidate[name]
        if len(left) != len(right):
            return dict(equal=False, first_difference=dict(path=f'{name}.count', reference=len(left), candidate=len(right)))
        for index, (a, b) in enumerate(zip(left, right)):
            if a.keys() != b.keys():
                return dict(equal=False, first_difference=dict(path=f'{name}[{index}].fields'))
            for key in a:
                x, y = a[key], b[key]
                if key in ('price', 'fee', 'cash', 'equity'):
                    x, y = D(str(x)), D(str(y))
                    equal = x.is_finite() and y.is_finite() and abs(x-y) <= D('.0000001')
                else:
                    equal = x == y
                if not equal and first is None:
                    first = dict(path=f'{name}[{index}].{key}', reference=str(x), candidate=str(y))
    return dict(equal=first is None, first_difference=first, money_tolerance='0.0000001 CNY')


def run_external(data, source, cent_rounding):
    # Optional comparison dependency; the core engine does not depend on Backtrader.
    sys.path.insert(0, str(source))
    import backtrader as bt
    import pandas as pd
    if not Path(bt.__file__).resolve().is_relative_to(source):
        raise ValueError('Backtrader import does not match requested source')

    class Commission(bt.CommInfoBase):
        params = (('stocklike', True), ('commtype', bt.CommInfoBase.COMM_FIXED))
        def _getcommission(self, size, price, pseudoexec):
            value = abs(size) * price
            fee = max(5., value * .0003) + value * (.00001 + (.0005 if size < 0 else 0)) if size else 0.
            return float(D(str(fee)).quantize(D('.01'), rounding=ROUND_HALF_UP)) if cent_rounding else fee

    class Strategy(bt.Strategy):
        def __init__(self):
            self.fills, self.daily, self.rejections = [], [], []
        def notify_order(self, order):
            if order.status == order.Completed:
                e = order.executed
                self.fills.append(dict(date=bt.num2date(e.dt).date().isoformat(), symbol=order.data._name,
                    side='BUY' if e.size > 0 else 'SELL', quantity=abs(int(e.size)), price=str(e.price), fee=str(e.comm)))
            elif order.status in (order.Canceled, order.Margin, order.Rejected, order.Expired):
                self.rejections.append(order.getstatusname())
        def next(self):
            day = self.datetime.date(0)
            if day in data.trading_days[:2]:
                for feed in self.datas:
                    (self.buy if day == data.trading_days[0] else self.sell)(data=feed, size=100)
            self.daily.append(dict(date=str(day), cash=str(self.broker.getcash()), equity=str(self.broker.getvalue()),
                positions={feed._name: int(self.getposition(feed).size) for feed in self.datas if self.getposition(feed).size}))

    broker = bt.Cerebro(stdstats=False)
    broker.broker.setcash(100000.)
    broker.broker.addcommissioninfo(Commission())
    for instrument in data.instruments:
        frame = pd.DataFrame([dict(date=b.start.date(), open=float(b.open), high=float(b.high),
            low=float(b.low), close=float(b.close), volume=b.volume) for b in data.bars if b.symbol == instrument.symbol])
        frame.index = pd.to_datetime(frame.pop('date'))
        broker.adddata(bt.feeds.PandasData(dataname=frame, openinterest=-1), name=instrument.symbol)
    broker.addstrategy(Strategy)
    strategy = broker.run(runonce=True, preload=True)[0]
    assert not strategy.rejections and len(strategy.fills) == 4
    strategy.fills.sort(key=lambda f: (f['date'], f['symbol'], f['side']))
    return dict(fills=strategy.fills, daily=strategy.daily), bt.__version__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--backtrader-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'summary.json').unlink(missing_ok=True)
    source = args.backtrader_source.resolve()
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True).strip():
        raise ValueError('Require a clean Backtrader source checkout for provenance')
    data = load_tushare_lake(args.root, ['000001.SZ', '600000.SH'], '2025-01-02', '2025-01-10')
    assert not data.corporate_actions
    assert len(data.bars) == len(data.trading_days) * 2
    assert all(r['trade_status'] == 'OPEN' for r in data.rules.values())
    assert all(b.volume * D('.01') >= 100 for b in data.bars)
    assert all(data.rules[b.symbol, b.start.date()]['limit_state'] == 'BOUNDED' and
        D(str(data.rules[b.symbol, b.start.date()]['down_limit'])) < b.open <
        D(str(data.rules[b.symbol, b.start.date()]['up_limit'])) for b in data.bars)
    def signal(bar, state):
        if bar.end.date() in data.trading_days[:2]:
            return [OrderIntent(bar.symbol, 'BUY' if bar.end.date() == data.trading_days[0] else 'SELL', 100)]
        return []
    native = run_backtest(data.instruments, data.bars, data.rules, signal,
        initial_cash=D(100000), participation_rate=.01, trading_days=data.trading_days,
        strategy_id='common-broker-fixed-shares', data_snapshot_id=data.snapshot['snapshot_id'])
    check_cash(native, 100000)
    assert len(native.fills) == 4 and not native.rejections and not native.pending_intents
    own = dict(fills=sorted([dict(date=str(datetime.fromtimestamp(f['time']/1e9, ZoneInfo('Asia/Shanghai')).date()),
        symbol=f['symbol'], side=f['side'], quantity=int(D(f['quantity'])), price=f['price'], fee=f['fee'])
        for f in native.fills], key=lambda f: (f['date'], f['symbol'], f['side'])),
        daily=[dict(date=r['date'], cash=r['cash'], equity=r['total_equity'],
            positions={s: int(D(q)) for s, q in r['holdings'].items() if D(q)}) for r in native.daily_nav])
    assert [f['date'] for f in own['fills']] == [str(day) for day in data.trading_days[1:3] for _ in range(2)]
    raw, version = run_external(data, source, False)
    rounded, _ = run_external(data, source, True)
    report = dict(status='PASS_COMMON_SUBSET', snapshot=data.snapshot, manifest=native.manifest,
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        backtrader=dict(version=version, commit=commit, clean=True),
        counts=dict(symbols=2, sessions=len(data.trading_days), bars=len(data.bars), fills=4),
        unrounded_fees=compare_accounts(own, raw), matched_cent_fees=compare_accounts(own, rounded),
        limits=['next-open market orders only; no active capacity/limit/lifecycle constraints',
                'no T+1 enforcement, corporate actions, halt rejection or partial fills tested in Backtrader',
                'fee hook is custom; cent rounding explicitly adapted; broker/matching remains native',
                'date-level comparison; not intrabar event/queue equivalence or speed ranking'])
    assert report['matched_cent_fees']['equal'], report['matched_cent_fees']
    for name, result in [('own', own), ('backtrader-raw', raw), ('backtrader-cent', rounded)]:
        (args.output / f'{name}.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    (args.output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
