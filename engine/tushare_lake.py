"""Read-only adapter for the Tushare Parquet lake layout; no network or secrets."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
from pathlib import Path
import pyarrow.dataset as ds

from contracts import Instrument, Venue, CorporateAction
from engine.backtest import BacktestBar, at
from engine.reproducibility import digest


def parse_day(value):
    return datetime.strptime(str(value).replace('-', ''), '%Y%m%d').date() if value else None


@dataclass
class LakeInputs:
    instruments: list
    bars: list
    rules: dict
    trading_days: list
    corporate_actions: list
    snapshot: dict


def load_tushare_lake(root, symbols, start, end):
    """Load explicit SSE/SZSE equities; missing facts raise, suspension stays explicit.

    Current stock master provides listing dates, not a verified last-trading-day
    history. This adapter does not construct a historical strategy universe.
    """
    root=Path(root);base=root/'lake';symbols=sorted(set(symbols))
    start,end=parse_day(start),parse_day(end)
    if not symbols or start>end or any(not s.endswith(('.SH','.SZ')) for s in symbols):
        raise ValueError('Explicit Shanghai/Shenzhen stock symbols and valid dates required')
    used={}
    def read(pattern, filter=None):
        paths=sorted(base.glob(pattern))
        if not paths:raise FileNotFoundError(f'Missing lake table: {pattern}')
        for p in paths:
            h=hashlib.sha256()
            with p.open('rb') as f:
                for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
            used[p.relative_to(root).as_posix()]=h.hexdigest()
        return ds.dataset([str(p) for p in paths],format='parquet').to_table(filter=filter).to_pylist()
    codes=ds.field('ts_code').isin(symbols)
    span=(ds.field('trade_date')>=start.strftime('%Y%m%d')) & (ds.field('trade_date')<=end.strftime('%Y%m%d'))
    master=read('meta/stock_basic.parquet',codes)
    indexed={r['ts_code']:r for r in master}
    if len(indexed)!=len(symbols) or len(master)!=len(indexed):raise ValueError('Missing or duplicate security master')
    instruments=[Instrument(symbol=s,exchange=Venue.SSE if s.endswith('.SH') else Venue.SZSE,
        list_date=parse_day(indexed[s]['list_date']),lot_size=100,
        min_order_qty=200 if s.startswith('688') else 100) for s in symbols]
    calendar=read('meta/trade_cal.parquet', (ds.field('cal_date')>=start.strftime('%Y%m%d')) &
        (ds.field('cal_date')<=end.strftime('%Y%m%d')))
    official={}
    for row in calendar:
        if row['exchange']!='SSE':continue
        day=parse_day(row['cal_date'])
        if day in official or row['is_open'] not in (0,1):raise ValueError('Invalid or duplicate exchange calendar row')
        official[day]=row['is_open']
    expected={start+timedelta(days=n) for n in range((end-start).days+1)}
    if set(official)!=expected:raise ValueError('Missing exchange calendar dates')
    sessions=sorted(day for day,is_open in official.items() if is_open)
    if not sessions:raise ValueError('Missing exchange calendar sessions')
    rows,limits,suspensions=[],[],[]
    for year in range(start.year,end.year+1):
        rows+=read(f'market/daily/year={year}/*.parquet',codes & span)
        limits+=read(f'market/stk_limit/year={year}/*.parquet',codes & span)
        suspensions+=read(f'market/suspend_d/year={year}/*.parquet',codes & span)
    def unique(data,label):
        out={}
        for row in data:
            key=row['ts_code'],parse_day(row['trade_date'])
            if key in out:raise ValueError(f'Duplicate {label}: {key}')
            out[key]=row
        return out
    daily=unique(rows,'bar');bands=unique(limits,'price band')
    if any(day not in sessions for _,day in daily):raise ValueError('Market bar outside official calendar sessions')
    halted={}
    for r in suspensions:
        if r['suspend_type']=='S':
            key=r['ts_code'],parse_day(r['trade_date'])
            # Intraday suspension is explicitly blocked for this daily adapter.
            halted[key]='DAY_BLOCKED' if r.get('suspend_timing') else 'SUSPENDED'
    bars=[];rules={}
    for inst in instruments:
        for day in sessions:
            if inst.list_date and day<inst.list_date:continue
            key=inst.symbol,day;r=daily.get(key);band=bands.get(key)
            status=halted.get(key,'OPEN')
            if r is None and status=='OPEN':raise ValueError(f'Missing OPEN bar: {key}')
            if band is None and status=='OPEN':raise ValueError(f'Missing official price band: {key}; unlimited sessions require explicit rules')
            rules[key]=dict(trade_status=status,limit_state='BOUNDED' if band else 'UNAVAILABLE',
                buy_min=inst.min_order_qty,buy_step=1 if inst.symbol.startswith('688') else 100,
                sell_min=200 if inst.symbol.startswith('688') else 100,sell_step=1 if inst.symbol.startswith('688') else 100,
                max_order=100000 if inst.symbol.startswith('688') else 1000000)
            if band:rules[key].update(up_limit=band['up_limit'],down_limit=band['down_limit'])
            if r is not None:
                vol=Decimal(str(r['volume_shares']))
                source_vol=Decimal(str(r['vol']))*100
                if not vol.is_finite() or vol<0 or vol!=vol.to_integral_value() or abs(vol-source_vol)>Decimal('.01'):
                    raise ValueError(f'Invalid or inconsistent share volume: {key}')
                bars.append(BacktestBar(inst.symbol,at(day,9,30),at(day,15),
                    *[Decimal(str(r[k])) for k in ('open','high','low','close')],int(vol)))
    # Include events already ex-dividend but paid/listed during the sample so they
    # cannot silently enter without the record-date and holding state required.
    action_rows=read('actions/dividend.parquet',codes)
    actions={};duplicates=0;unimplemented=0
    for r in action_rows:
        status=str(r.get('div_proc') or '').strip()
        if status not in {'实施','股东大会通过','预披露','股东大会未通过','未通过','预案','停止实施','其他','股东提议'}:
            raise ValueError(f'Unknown corporate action implementation status: {r["ts_code"]}')
        if status!='实施':unimplemented+=1;continue
        ex=parse_day(r.get('ex_date'))
        record=parse_day(r.get('record_date'));pay=parse_day(r.get('pay_date'));listing=parse_day(r.get('div_listdate'))
        if ex is None:
            if any(day and start<=day<=end for day in (record,pay,listing)):
                raise ValueError(f'Missing corporate action ex-date: {r["ts_code"]}')
            continue
        relevant=start<=ex<=end or (pay and start<=pay<=end) or (listing and start<=listing<=end)
        if not relevant:continue
        if ex<start:raise ValueError(f'Corporate action spans start; extend sample to record date: {r["ts_code"]} {ex}')
        cash=Decimal(str(r.get('cash_div_tax') or 0))
        bonus=Decimal(str(r.get('stk_bo_rate') or 0));reserve=Decimal(str(r.get('stk_co_rate') or 0))
        total=Decimal(str(r.get('stk_div') or 0))
        if abs(total-bonus-reserve)>Decimal('.00000001'):raise ValueError('Inconsistent corporate action share ratios')
        if not (cash or total):continue
        if not record or (cash and not pay) or (total and not listing):raise ValueError('Missing corporate action event dates')
        if record<start:raise ValueError('Corporate action record date before sample; extend start')
        a=CorporateAction(symbol=r['ts_code'],record_date=record,ex_date=ex,pay_date=pay,
            announcement_date=parse_day(r.get('imp_ann_date') or r.get('ann_date')),
            cash_div=cash,bonus_ratio=bonus,reserve_ratio=reserve,share_listing_date=listing,fractional_policy='reject')
        key=a.symbol,a.ex_date
        if key in actions:
            if actions[key]!=a:raise ValueError(f'Conflicting corporate action: {key}')
            duplicates+=1
        else:actions[key]=a
    snapshot=dict(schema_version=1,source='local-tushare-parquet',source_files_sha256=used,
        historical_vintage_pit=False,price_adjustment='raw',volume_unit='shares',
        security_universe='explicit symbols; current master listing date only',
        deduplicated_actions=duplicates,excluded_unimplemented_actions=unimplemented,
        partial_suspension_policy='DAY_BLOCKED')
    snapshot['snapshot_id']=digest(snapshot)
    return LakeInputs(instruments,sorted(bars,key=lambda b:(b.start,b.symbol)),rules,sessions,
                      sorted(actions.values(),key=lambda a:(a.ex_date,a.symbol)),snapshot)
