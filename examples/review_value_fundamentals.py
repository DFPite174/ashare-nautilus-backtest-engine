"""P0 review of frozen V losses, disclosed earnings and cash conversion."""
import argparse
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow.dataset as ds

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from examples.value_data_snapshot import sha, check_output_location


def available_reports(frame,signal,metrics):
    data=frame.copy()
    if 'report_type' in data:
        data=data[pd.to_numeric(data.report_type,errors='coerce').eq(1) &
                  pd.to_numeric(data.comp_type,errors='coerce').eq(1)]
    ann=pd.to_datetime(data.ann_date,format='%Y%m%d',errors='coerce')
    actual=pd.to_datetime(data.f_ann_date,format='%Y%m%d',errors='coerce') if 'f_ann_date' in data else ann
    data['available']=pd.concat([ann,actual],axis=1).max(axis=1)
    data=data[ann.notna() & data.available.lt(pd.to_datetime(signal,format='%Y%m%d')) &
              data.end_date.lt(signal)].copy()
    data=data[['ts_code','end_date','available',*metrics]].drop_duplicates()
    data=data[data.available.eq(data.groupby(['ts_code','end_date']).available.transform('max'))]
    if data.duplicated(['ts_code','end_date']).any():
        raise ValueError('Conflicting disclosed financial revisions')
    for metric in metrics:
        data[metric]=pd.to_numeric(data[metric],errors='coerce')
    return data


def ttm_value(reports,symbol,metric,period=None):
    rows=reports[reports.ts_code.eq(symbol)].set_index('end_date')
    if rows.empty:
        return None,[]
    period=period or max(rows.index)
    year=int(period[:4])-1
    periods=[period] if period.endswith('1231') else [period,f'{year}1231',str(year)+period[4:]]
    if any(p not in rows.index or not math.isfinite(float(rows.loc[p,metric])) for p in periods):
        return None,[]
    parts=[dict(period=p,available=rows.loc[p,'available'].strftime('%Y%m%d'),value=float(rows.loc[p,metric])) for p in periods]
    values=[r['value'] for r in parts]
    return (values[0] if len(values)==1 else values[0]+values[1]-values[2]),parts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('run','contributions','strategy-root','root','output'):
        parser.add_argument('--'+flag,type=Path,required=True)
    args=parser.parse_args()
    args.output=check_output_location(args.output,args.run,args.contributions,args.strategy_root,args.root)
    protected={}
    def read_json(path,expected=None):
        protected[path]=sha(path)
        if expected is not None and expected!=protected[path]:
            raise ValueError('Frozen input changed: '+path.name)
        return json.loads(path.read_text(encoding='utf-8'))
    source=read_json(args.run/'summary.json')
    if source['holdout_evaluated'] or not source['status'].startswith('PASS_EXECUTION_ADAPTATION'):
        raise ValueError('Require frozen validation execution')
    contribution=read_json(args.contributions/'summary.json')
    if contribution['source_summary_sha256']!=protected[args.run/'summary.json']:
        raise ValueError('Contribution run differs')
    detail=read_json(args.contributions/'private_details.json',contribution['private_details_sha256'])['V']
    artifact=read_json(args.run/'V.json',next(r['artifact_sha256'] for r in source['runs'] if r['name']=='V'))
    decisions=artifact['decisions']
    if len(decisions)!=36 or any(not '20201231'<=d['date'].replace('-','')<='20231231' for d in decisions):
        raise ValueError('Require the frozen 36 validation decisions')
    pnl={s:float(v) for s,v in detail['security_profit_cny'].items()}
    medical=sorted(s for s in pnl if detail['industry_labels'][s]=='医疗保健')
    worst=sorted(pnl,key=pnl.get)[:10]
    focus=sorted(set(medical+worst))
    selected=set().union(*(d['weights'] for d in decisions))
    cache=args.strategy_root/'data/raw/history_provider_v1'
    manifest=read_json(cache/'manifest.json')
    groups={api:[] for api in ('income_vip','cashflow_vip','balancesheet_vip','fina_indicator_vip')}
    for entry in manifest['completed'].values():
        if entry['api'] not in groups or not '20110101'<=entry['params'].get('period','')<='20231231':
            continue
        if Path(entry['file']).name!=entry['file']:
            raise ValueError('Unsafe cache path')
        raw=read_json(cache/entry['file'],entry['sha256'])
        frame=pd.DataFrame(raw['data'],columns=raw['columns'])
        if len(frame)!=entry['rows']:
            raise ValueError('Cache row count differs')
        subset=frame[frame.ts_code.isin(selected)]
        if not subset.empty:
            groups[entry['api']].append(subset)
    tables={k:pd.concat(v,ignore_index=True) for k,v in groups.items()}
    audit=[]
    panels={}
    for decision in decisions:
        signal=decision['date'].replace('-','')
        filename=f'panel_{signal}.csv'
        path=args.strategy_root/'outputs/history_provider_v1'/filename
        protected[path]=source['upstream']['panels_sha256'][filename]
        if sha(path)!=protected[path]:
            raise ValueError('Signal panel changed')
        panel=pd.read_csv(path,dtype={'ts_code':str}).set_index('ts_code')
        panels[signal]=panel
        symbols=set(decision['weights'])
        reports={}
        for api,metrics in [('fina_indicator_vip',['profit_dedt']),('income_vip',['n_income']),
                            ('cashflow_vip',['n_cashflow_act']),('balancesheet_vip',['total_assets','total_cur_liab'])]:
            reports[api]=available_reports(tables[api][tables[api].ts_code.isin(symbols)],signal,metrics)
        for symbol in sorted(symbols):
            row=panel.loc[symbol]
            profit,parts=ttm_value(reports['fina_indicator_vip'],symbol,'profit_dedt',str(int(row.latest_reporting_period)))
            cash,net,annual_parts=0.,0.,[]
            for year in range(int(row.annual_end_year)-2,int(row.annual_end_year)+1):
                period=f'{year}1231'
                for api,metric in [('cashflow_vip','n_cashflow_act'),('income_vip','n_income'),('balancesheet_vip','total_assets')]:
                    value,items=ttm_value(reports[api],symbol,metric,period)
                    if value is None:
                        raise ValueError('Missing disclosed annual source for selected target')
                    annual_parts.extend(items)
                    if api=='cashflow_vip': cash+=value
                    if api=='income_vip': net+=value
            checks=dict(ttm_profit=profit is not None and math.isclose(profit,float(row.ttm_profit),rel_tol=1e-10,abs_tol=.01),
                cash_quality=net!=0 and math.isclose(cash/net,float(row.cash_quality),rel_tol=1e-10,abs_tol=1e-9),
                value_yield=profit is not None and math.isclose(profit/(float(row.total_mv)*10000),float(row.value_yield),rel_tol=1e-10,abs_tol=1e-10),
                disclosed_before_signal=all(p['available']<signal for p in parts+annual_parts))
            audit.append(dict(symbol=symbol,signal=signal,checks=checks,ttm_sources=parts,annual_sources=annual_parts))
    local={}
    fields={'income':['n_income','n_income_attr_p'], 'cashflow':['n_cashflow_act'],
            'fina_indicator':['profit_dedt','extra_item']}
    for name,metrics in fields.items():
        path=args.root/'lake/statements'/f'{name}.parquet'
        protected[path]=sha(path)
        schema=ds.dataset(path,format='parquet').schema.names
        columns=['ts_code','end_date','ann_date']+[k for k in ['f_ann_date','report_type','comp_type'] if k in schema]+metrics
        local[name]=ds.dataset(path,format='parquet').to_table(columns=columns,
            filter=ds.field('ts_code').isin(focus) & (ds.field('end_date')<='20231231')).to_pandas()
    cases=[]
    for symbol in focus:
        first=next(d for d in decisions if symbol in d['weights'])
        signal=first['date'].replace('-','')
        panel=panels[signal].loc[symbol]
        start=available_reports(local['fina_indicator'][local['fina_indicator'].ts_code.eq(symbol)],signal,['profit_dedt','extra_item'])
        finish=available_reports(local['fina_indicator'][local['fina_indicator'].ts_code.eq(symbol)],'20231229',['profit_dedt','extra_item'])
        first_ttm,first_parts=ttm_value(start,symbol,'profit_dedt')
        end_ttm,end_parts=ttm_value(finish,symbol,'profit_dedt')
        held_days=[r['date'] for r in artifact['result']['daily_nav'] if float(r['holdings'].get(symbol,0))>0]
        fills=[f for f in artifact['result']['fills'] if f['symbol']==symbol]
        fill_days=[datetime.fromtimestamp(f['time']/1e9,ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d') for f in fills]
        position_end=max([d.replace('-','') for d in held_days]+fill_days)
        position_reports=available_reports(local['fina_indicator'][local['fina_indicator'].ts_code.eq(symbol)],position_end,['profit_dedt','extra_item'])
        position_ttm,position_parts=ttm_value(position_reports,symbol,'profit_dedt')
        inc=available_reports(local['income'][local['income'].ts_code.eq(symbol)],signal,['n_income','n_income_attr_p'])
        cf=available_reports(local['cashflow'][local['cashflow'].ts_code.eq(symbol)],signal,['n_cashflow_act'])
        period=str(int(panel.latest_reporting_period))
        prior=int(period[:4])-1
        expected_first=[period] if period.endswith('1231') else [period,f'{prior}1231',str(prior)+period[4:]]
        income_gaps=[p for p in expected_first if p not in set(inc.end_date)]
        latest_position=max(position_reports.end_date) if not position_reports.empty else None
        if latest_position:
            prior_position=int(latest_position[:4])-1
            expected_position=[latest_position] if latest_position.endswith('1231') else [latest_position,f'{prior_position}1231',str(prior_position)+latest_position[4:]]
            values=position_reports.set_index('end_date').profit_dedt
            position_gaps=[p for p in expected_position if p not in values.index or not math.isfinite(float(values[p]))]
        else:
            position_gaps=['NO_DISCLOSED_REPORT']
        income_ttm,income_parts=ttm_value(inc,symbol,'n_income',period)
        parent_ttm,parent_parts=ttm_value(inc,symbol,'n_income_attr_p',period)
        ocf,ocf_parts=ttm_value(cf,symbol,'n_cashflow_act',period)
        peak=[]
        for year in range(int(panel.annual_end_year)-2,int(panel.annual_end_year)+1):
            value,_=ttm_value(start,symbol,'profit_dedt',f'{year}1231')
            if value is not None: peak.append(value)
        annual_mean=sum(peak)/3 if len(peak)==3 else None
        frozen_ttm=float(panel.ttm_profit)
        cases.append(dict(symbol=symbol,name=str(panel['name']),industry=detail['industry_labels'][symbol],
            pnl=pnl[symbol],first_signal=signal,first_target_yield=float(panel.value_yield),
            first_buy_date=min(datetime.fromtimestamp(f['time']/1e9,ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d') for f in fills if f['side']=='BUY'),
            first_reporting_period=period,first_income_period_gaps=income_gaps,
            first_income_gap_announcement_dates=local['income'][local['income'].ts_code.eq(symbol) & local['income'].end_date.isin(income_gaps)][['end_date','ann_date','f_ann_date']].drop_duplicates().to_dict('records'),
            position_end_latest_reporting_period=latest_position,position_end_missing_profit_periods=position_gaps,
            position_end_date=position_end,position_end_known_ttm=position_ttm,
            position_end_to_first_ttm_change=position_ttm/first_ttm-1 if position_ttm is not None and first_ttm and first_ttm>0 else None,
            first_three_year_cash_quality=float(panel.cash_quality),frozen_first_ttm=frozen_ttm,
            local_first_ttm=first_ttm,local_first_matches_frozen=first_ttm is not None and math.isclose(first_ttm,frozen_ttm,rel_tol=1e-8,abs_tol=.01),
            first_ttm_vs_three_annual_mean=frozen_ttm/annual_mean if annual_mean and annual_mean>0 else None,
            first_ttm_ocf_to_net_income=ocf/income_ttm if ocf is not None and income_ttm and income_ttm>0 else None,
            first_ttm_nonrecurring_share=1-first_ttm/parent_ttm if first_ttm is not None and parent_ttm and parent_ttm>0 else None,
            end_2023_known_ttm=end_ttm,end_to_first_ttm_change=end_ttm/first_ttm-1 if end_ttm is not None and first_ttm and first_ttm>0 else None,
            financial_sources=dict(first=first_parts,end=end_parts,position_end=position_parts,income=income_parts,parent=parent_parts,ocf=ocf_parts)))
    def aggregate(rows):
        return dict(count=len(rows),pnl=sum(r['pnl'] for r in rows),
            first_cash_quality_below_one=sum(r['first_three_year_cash_quality']<1 for r in rows),
            first_cash_quality_negative=sum(r['first_three_year_cash_quality']<0 for r in rows),
            first_ttm_at_least_twice_annual_mean=sum(r['first_ttm_vs_three_annual_mean'] is not None and r['first_ttm_vs_three_annual_mean']>=2 for r in rows),
            end_ttm_down_over_half=sum(r['end_to_first_ttm_change'] is not None and r['end_to_first_ttm_change']<-.5 for r in rows),
            position_end_ttm_down_over_half=sum(r['position_end_to_first_ttm_change'] is not None and r['position_end_to_first_ttm_change']<-.5 for r in rows),
            first_nonrecurring_share_observations=sum(r['first_ttm_nonrecurring_share'] is not None for r in rows),
            first_nonrecurring_share_over_30pct=sum(r['first_ttm_nonrecurring_share'] is not None and r['first_ttm_nonrecurring_share']>.3 for r in rows),
            first_ttm_ocf_ratio_observations=sum(r['first_ttm_ocf_to_net_income'] is not None for r in rows),
            first_ttm_ocf_ratio_below_one=sum(r['first_ttm_ocf_to_net_income'] is not None and r['first_ttm_ocf_to_net_income']<1 for r in rows),
            position_end_ttm_observations=sum(r['position_end_to_first_ttm_change'] is not None for r in rows),
            local_first_ttm_mismatches=sum(not r['local_first_matches_frozen'] for r in rows))
    baseline=[]
    for symbol in sorted(selected):
        first=next(d for d in decisions if symbol in d['weights'])
        ratio=float(panels[first['date'].replace('-','')].loc[symbol].cash_quality)
        baseline.append(dict(symbol=symbol,cash_quality=ratio,pnl=pnl.get(symbol,0)))
    report=dict(source_summary_sha256=protected[args.run/'summary.json'],script_sha256=sha(__file__),
        holdout_evaluated=False,ranking_metric='TTM deducted parent earnings / market capitalization',
        audit=dict(target_observations=len(audit),failures=dict(Counter(k for r in audit for k,v in r['checks'].items() if not v))),
        medical=aggregate([r for r in cases if r['symbol'] in medical]),worst_ten=aggregate([r for r in cases if r['symbol'] in worst]),
        first_selection_cash_groups={label:dict(count=sum(test(r) for r in baseline),
            pnl=sum(r['pnl'] for r in baseline if test(r))) for label,test in
            [('below_one',lambda r:r['cash_quality']<1),('at_least_one',lambda r:r['cash_quality']>=1)]},
        financial_availability=dict(position_end_missing_ttm_cases=sum(bool(r['position_end_missing_profit_periods']) for r in cases),
            first_income_period_gap_cases=sum(bool(r['first_income_period_gaps']) for r in cases)),
        focused_cases=len(cases),status='P0_REVIEW_COMPLETED')
    for path,expected in protected.items():
        if sha(path)!=expected:
            raise ValueError('Input changed during review')
    args.output.mkdir(parents=True,exist_ok=True)
    private=args.output/'private_details.json'
    private.write_text(json.dumps(dict(cases=cases,target_audit=audit,first_selection_cash=sorted(baseline,key=lambda r:r['symbol']),input_sha256={str(p):h for p,h in protected.items()}),ensure_ascii=False,indent=2),encoding='utf-8')
    report['private_details_sha256']=sha(private)
    (args.output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
