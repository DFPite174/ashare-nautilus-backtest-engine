"""Read-only real-data engineering checks; outputs contain no source price tables."""
import argparse
from decimal import Decimal as D, ROUND_HALF_UP
import json
from pathlib import Path
import sys
import time
import tracemalloc

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import run_backtest, OrderIntent, AShareFeeModel
from engine.tushare_lake import load_tushare_lake


def check_cash(result, initial):
    """Independent arithmetic from native fills, not the engine's fee function."""
    cash=D(initial)
    for fill in result.fills:
        value=D(fill['quantity'])*D(fill['price'])
        cash+=value-D(fill['fee']) if fill['side']=='SELL' else -value-D(fill['fee'])
    cash+=sum((D(row['amount']) for row in result.account_journal),D(0))
    assert cash==result.native_cash==result.ledger_cash
    assert all(row['cash_matches'] and row['positions_match'] for row in result.daily_nav)


def execute(root, symbols, start, end, kind, slippage=0):
    data=load_tushare_lake(root,symbols,start,end)
    days=data.trading_days
    initial=D(1200) if kind=='rebalance' else D(1000000)
    def signal(bar,state):
        day=bar.end.date()
        if kind=='rebalance':
            if bar.symbol!='600000.SH':return []
            if day==days[0]:return [OrderIntent('600000.SH','BUY',100)]
            if day==days[1]:return [OrderIntent('000001.SZ','BUY',100),OrderIntent('600000.SH','SELL',100)]
            return []
        if kind=='suspended-holding':
            if day==days[0]:return [OrderIntent(bar.symbol,'BUY',100)]
            if str(day) in {'2025-04-30','2025-05-19'}:return [OrderIntent(bar.symbol,'SELL',100)]
            return []
        if kind=='suspension':
            return [OrderIntent(bar.symbol,'BUY',100)] if str(day)=='2025-04-30' else []
        if day==days[0]:return [OrderIntent(bar.symbol,'BUY',200 if bar.symbol.startswith('688') else 100)]
        if kind=='roundtrip' and day==days[-2]:
            return [OrderIntent(bar.symbol,'SELL',int(state.positions.get(bar.symbol,0)))] if state.positions.get(bar.symbol,0) else []
        return []
    started=time.perf_counter()
    fee=AShareFeeModel(commission_rate=.0002,min_commission=3)
    result=run_backtest(data.instruments,data.bars,data.rules,signal,
        trading_days=days,corporate_actions=data.corporate_actions,initial_cash=initial,
        participation_rate=.01,fee_model=fee,slippage_bps=slippage,
        strategy_id=f'engineering-{kind}',strategy_params={'quantity':'100; KCB 200'},
        data_snapshot_id=data.snapshot['snapshot_id'])
    check_cash(result,initial)
    if kind=='roundtrip':
        assert len(result.fills)==2*len(symbols)
        assert all(q==0 for q in result.positions.values())
    if kind=='dividend':
        assert len(result.fills)==1
        expected=(D(100)*D('27.673')).quantize(D('.01'),rounding=ROUND_HALF_UP)
        assert sum((D(row['amount']) for row in result.account_journal),D(0))==expected==D('2767.30')
    if kind=='split':
        assert len(result.fills)==1 and result.positions['688603.SH']==290
    if kind=='suspension':
        assert not result.fills
        assert any(row['reason']=='SUSPENDED' for row in result.rejections)
    if kind=='suspended-holding':
        assert len(result.fills)==2 and result.positions['002128.SZ']==0
        assert any(row['reason']=='SUSPENDED' for row in result.rejections)
        frozen=[r for r in result.daily_nav if '2025-05-06'<=r['date']<='2025-05-16']
        assert len(frozen)==9 and all(D(r['holdings']['002128.SZ'])==100 for r in frozen)
        assert len({r['total_equity'] for r in frozen})==1
    if kind=='rebalance':
        assert [(r['symbol'],r['side']) for r in result.fills]==[
            ('600000.SH','BUY'),('600000.SH','SELL'),('000001.SZ','BUY')]
        assert not result.rejections
    return dict(status='PASS',kind=kind,symbols=symbols,start=start,end=end,
        bars=len(data.bars),sessions=len(days),fills=len(result.fills),
        rejections=result.rejections,final_cash=str(result.native_cash),
        final_equity=result.daily_nav[-1]['total_equity'],positions={s:str(q) for s,q in result.positions.items()},
        elapsed_seconds=round(time.perf_counter()-started,3),snapshot=data.snapshot,
        manifest=result.manifest),result


def benchmark(root,symbols,start,end):
    data=load_tushare_lake(root,symbols,start,end)
    calls=0
    def signal(bar,state):
        nonlocal calls
        calls+=1
        assert state.history[bar.symbol][-1].end==bar.end
        return []
    tracemalloc.start();started=time.perf_counter()
    result=run_backtest(data.instruments,data.bars,data.rules,signal,corporate_actions=data.corporate_actions,
        trading_days=data.trading_days,data_snapshot_id=data.snapshot['snapshot_id'],strategy_id='no-order-load')
    elapsed=time.perf_counter()-started;_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    check_cash(result,100000)
    assert calls==len(data.bars)
    return dict(status='PASS',symbols=len(symbols),sessions=len(data.trading_days),bars=len(data.bars),
        signal_calls=calls,seconds=round(elapsed,3),python_peak_mib=round(peak/1024**2,2),
        memory_scope='tracemalloc Python allocations; excludes some native allocations; inputs loaded before timing',
        workload='no-order callbacks and daily reconciliation',snapshot_id=data.snapshot['snapshot_id'],manifest=result.manifest)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--benchmark',action='store_true')
    args=p.parse_args()
    report=dict(schema_version=1,validation='real-provider-data engineering replay',
        historical_vintage_pit=False,cases=[],gaps=[])
    cases=[(['600000.SH'],'2025-01-02','2025-01-10','roundtrip'),
           (['600519.SH'],'2025-06-20','2025-06-30','dividend'),
           (['688603.SH'],'2025-01-02','2025-01-10','split'),
           (['002128.SZ'],'2025-04-28','2025-06-30','suspension'),
           (['002128.SZ'],'2025-04-28','2025-06-30','suspended-holding'),
           (['600000.SH','000001.SZ'],'2025-01-02','2025-01-10','rebalance'),
           (['600000.SH','000001.SZ'],'2025-01-02','2025-01-10','roundtrip')]
    for symbols,start,end,kind in cases:
        try:
            row,_=execute(args.root,symbols,start,end,kind)
            report['cases'].append(row)
            print(kind,symbols,'PASS',flush=True)
        except (ValueError,FileNotFoundError) as e:
            report['gaps'].append(dict(kind=kind,symbols=symbols,status='BLOCKED_DATA',reason=str(e)))
    baseline,a=execute(args.root,['600000.SH'],'2025-01-02','2025-01-10','roundtrip')
    stress,b=execute(args.root,['600000.SH'],'2025-01-02','2025-01-10','roundtrip',slippage=10)
    assert b.native_cash<a.native_cash
    report['cases'].append(dict(status='PASS',kind='slippage-pressure',baseline_cash=str(a.native_cash),
        stress_cash=str(b.native_cash),stress=stress))
    if args.benchmark:
        symbols=['000001.SZ','000002.SZ','000063.SZ','000333.SZ','000651.SZ','000858.SZ',
            '002027.SZ','002415.SZ','002475.SZ','002594.SZ','600000.SH','600009.SH','600016.SH',
            '600028.SH','600030.SH','600036.SH','600048.SH','600050.SH','600104.SH','600519.SH']
        report['benchmark']=benchmark(args.root,symbols,'2025-01-02','2025-12-31')
    report['status']='PASS' if not report['gaps'] else 'PARTIAL_BLOCKED_DATA'
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(report['status'],args.output)
    if report['gaps']:raise SystemExit(2)


if __name__=='__main__':main()
