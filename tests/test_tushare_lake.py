from pathlib import Path
from decimal import Decimal as D
import hashlib
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def write(root, path, rows):
    target = root / 'lake' / path
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), target)


def lake(root, missing_limit=False, missing_bar=False, suspended=False, duplicate_action=False):
    write(root, 'meta/stock_basic.parquet', [dict(ts_code='600000.SH',list_date='19990101',name='test')])
    write(root, 'meta/trade_cal.parquet', [dict(exchange='SSE',cal_date=f'2025050{n}',is_open=1) for n in (6,7,8)])
    write(root, 'market/daily/year=2025/daily_2025.parquet', [dict(ts_code='600000.SH',trade_date=f'2025050{n}',
        open=10.,high=11.,low=9.,close=10.,vol=100.,volume_shares=10000) for n in (6,7,8) if not missing_bar or n!=7])
    write(root, 'market/stk_limit/year=2025/stk_limit_2025.parquet', [dict(ts_code='600000.SH',trade_date=f'2025050{n}',
        up_limit=11.,down_limit=9.) for n in (6,7,8) if not missing_limit or n!=7])
    write(root, 'market/suspend_d/year=2025/suspend_d_2025.parquet', [dict(ts_code='600000.SH' if suspended else '000001.SZ',
        trade_date='20250507',suspend_type='S',suspend_timing=None)])
    action=dict(ts_code='600000.SH',div_proc='实施',ann_date='20250430',imp_ann_date='20250501',record_date='20250506',ex_date='20250507',
        pay_date='20250508',div_listdate=None,cash_div_tax=.5,stk_div=0.,stk_bo_rate=0.,stk_co_rate=0.)
    write(root, 'actions/dividend.parquet', [action, dict(action,ann_date='20250429')] if duplicate_action else [action])


def test_lake_uses_official_calendar_raw_prices_share_units_and_deduped_actions(tmp_path):
    from engine.tushare_lake import load_tushare_lake
    lake(tmp_path, duplicate_action=True)
    files={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob('*.parquet')}
    data=load_tushare_lake(tmp_path,['600000.SH'],'2025-05-06','2025-05-08')
    assert len(data.bars)==3 and len(data.trading_days)==3
    assert data.bars[0].volume==10000 and data.bars[0].open==D(10)
    assert len(data.corporate_actions)==1 and data.corporate_actions[0].cash_div==D('.5')
    assert data.snapshot['historical_vintage_pit'] is False
    assert data.snapshot['deduplicated_actions']==1
    assert files=={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


@pytest.mark.parametrize('missing_limit,missing_bar',[(True,False),(False,True)])
def test_lake_rejects_missing_market_facts(tmp_path,missing_limit,missing_bar):
    from engine.tushare_lake import load_tushare_lake
    lake(tmp_path,missing_limit=missing_limit,missing_bar=missing_bar)
    with pytest.raises(ValueError,match='Missing'):
        load_tushare_lake(tmp_path,['600000.SH'],'2025-05-06','2025-05-08')


def test_explicit_full_day_suspension_is_not_missing_data(tmp_path):
    from engine.tushare_lake import load_tushare_lake
    lake(tmp_path,missing_bar=True,suspended=True)
    data=load_tushare_lake(tmp_path,['600000.SH'],'2025-05-06','2025-05-08')
    assert len(data.bars)==2
    assert data.rules['600000.SH',data.trading_days[1]]['trade_status']=='SUSPENDED'


def test_missing_calendar_date_cannot_silently_drop_market_bar(tmp_path):
    from engine.tushare_lake import load_tushare_lake
    lake(tmp_path)
    write(tmp_path,'meta/trade_cal.parquet',[dict(exchange='SSE',cal_date=f'2025050{n}',is_open=1) for n in (6,8)])
    with pytest.raises(ValueError,match='calendar'):
        load_tushare_lake(tmp_path,['600000.SH'],'2025-05-06','2025-05-08')


def test_relevant_paid_dividend_without_ex_date_is_a_data_gap(tmp_path):
    from engine.tushare_lake import load_tushare_lake
    lake(tmp_path)
    p=tmp_path/'lake/actions/dividend.parquet'
    rows=pq.ParquetFile(p).read().to_pylist();rows[0]['ex_date']=None
    write(tmp_path,'actions/dividend.parquet',rows)
    with pytest.raises(ValueError,match='ex-date'):
        load_tushare_lake(tmp_path,['600000.SH'],'2025-05-06','2025-05-08')


def test_closed_calendar_dates_are_covered_but_not_executable(tmp_path):
    from engine.tushare_lake import load_tushare_lake
    lake(tmp_path)
    rows=[dict(exchange='SSE',cal_date=f'2025050{n}',is_open=int(n!=7)) for n in (6,7,8)]
    write(tmp_path,'meta/trade_cal.parquet',rows)
    p=tmp_path/'lake/market/daily/year=2025/daily_2025.parquet'
    write(tmp_path,'market/daily/year=2025/daily_2025.parquet',
          [r for r in pq.ParquetFile(p).read().to_pylist() if r['trade_date']!='20250507'])
    data=load_tushare_lake(tmp_path,['600000.SH'],'2025-05-06','2025-05-08')
    assert len(data.trading_days)==2 and len(data.bars)==2


def test_dividend_proposal_is_not_an_implemented_distribution(tmp_path):
    from engine.tushare_lake import load_tushare_lake
    lake(tmp_path)
    p=tmp_path/'lake/actions/dividend.parquet'
    rows=pq.ParquetFile(p).read().to_pylist();rows[0].update(div_proc='预案',ex_date=None)
    write(tmp_path,'actions/dividend.parquet',rows)
    data=load_tushare_lake(tmp_path,['600000.SH'],'2025-05-06','2025-05-08')
    assert not data.corporate_actions
    assert data.snapshot['excluded_unimplemented_actions']==1


def test_market_tables_are_projected_and_processed_in_batches(tmp_path, monkeypatch):
    import engine.tushare_lake as module
    lake(tmp_path)
    original = module.ds.dataset
    scans = []
    class Dataset:
        def __init__(self, paths, **kwargs):
            self.value = original(paths, **kwargs)
            self.market = any('/market/' in str(path).replace('\\', '/') for path in paths)
        def to_table(self, **kwargs):
            assert not self.market, 'Market input must not materialize all dictionary rows'
            return self.value.to_table(**kwargs)
        def to_batches(self, **kwargs):
            scans.append(tuple(kwargs['columns']))
            assert 0 < kwargs['batch_size'] <= 8192
            return self.value.to_batches(**{**kwargs, 'batch_size': 1})
        @property
        def schema(self):
            return self.value.schema
    monkeypatch.setattr(module.ds, 'dataset', Dataset)
    data = module.load_tushare_lake(tmp_path, ['600000.SH'], '2025-05-06', '2025-05-08')
    assert len(data.bars) == 3 and len(data.rules) == 3
    assert len(scans) == 3
    assert all('ts_code' in columns and 'trade_date' in columns for columns in scans)
    assert data.bars[0].volume == 10000


@pytest.mark.parametrize('table', ['daily', 'stk_limit'])
def test_duplicate_market_row_is_rejected_across_input_batches(tmp_path, table):
    from engine.tushare_lake import load_tushare_lake
    lake(tmp_path)
    path = tmp_path / f'lake/market/{table}/year=2025/{table}_2025.parquet'
    rows = pq.ParquetFile(path).read().to_pylist()
    rows.append(dict(rows[0]))
    pq.write_table(pa.Table.from_pylist(rows), path, row_group_size=1)
    with pytest.raises(ValueError, match='Duplicate'):
        load_tushare_lake(tmp_path, ['600000.SH'], '2025-05-06', '2025-05-08')


def test_suspension_timing_column_remains_optional(tmp_path):
    from engine.tushare_lake import load_tushare_lake
    lake(tmp_path, missing_bar=True, suspended=True)
    path = tmp_path / 'lake/market/suspend_d/year=2025/suspend_d_2025.parquet'
    pq.write_table(pq.ParquetFile(path).read().drop(['suspend_timing']), path)
    data = load_tushare_lake(tmp_path, ['600000.SH'], '2025-05-06', '2025-05-08')
    assert len(data.bars) == 2
    assert data.rules['600000.SH', data.trading_days[1]]['trade_status'] == 'SUSPENDED'


@pytest.mark.parametrize('shares', [10001, -100, 10000.5])
def test_batched_reader_preserves_volume_consistency_checks(tmp_path, shares):
    from engine.tushare_lake import load_tushare_lake
    lake(tmp_path)
    path = tmp_path / 'lake/market/daily/year=2025/daily_2025.parquet'
    rows = pq.ParquetFile(path).read().to_pylist()
    rows[0]['volume_shares'] = shares
    pq.write_table(pa.Table.from_pylist(rows), path, row_group_size=1)
    with pytest.raises(ValueError, match='share volume'):
        load_tushare_lake(tmp_path, ['600000.SH'], '2025-05-06', '2025-05-08')
