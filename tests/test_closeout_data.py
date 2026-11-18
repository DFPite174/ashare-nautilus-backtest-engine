from decimal import Decimal as D
import pytest
from engine.reproducibility import digest
from examples.closeout_data import merge_identical_actions, reconcile_history, verify_zero_entitlements


def test_only_disclosure_metadata_can_be_consolidated():
    a=dict(ts_code='A',ex_date='20150504',cash_div_tax=.1,ann_date='20150201',imp_ann_date='20150427')
    b=dict(a,imp_ann_date='20150428')
    rows,audit=merge_identical_actions([a,b])
    assert len(rows)==1 and rows[0]['imp_ann_date']=='20150428' and len(audit)==1
    rows,_=merge_identical_actions([a,dict(b,cash_div_tax=.2)])
    assert len(rows)==2


def test_missing_ex_event_is_only_harmless_with_zero_record_date_shares():
    event=dict(symbol='A',record_date='20170505')
    nav=[dict(date='2017-05-05',holdings={'A':'0'})]
    verify_zero_entitlements(nav,[event])
    nav[0]['holdings']['A']='100'
    with pytest.raises(ValueError,match='Unsupported entitlement'):
        verify_zero_entitlements(nav,[event])


def test_disclosed_entitlement_is_checked_even_when_provider_omits_row(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    event = dict(symbol='600828.SH', record_date='20190327', row_sha256=None)
    ordinary = dict(ts_code='600828.SH', ex_date='20190423')
    restructuring = dict(ts_code='600399.SH', ex_date='20181227')
    exclusion = dict(symbol='600399.SH', record_date='20181226', row_sha256=digest(restructuring))
    for directory in ('actions', 'meta'):
        (tmp_path / 'lake' / directory).mkdir(parents=True)
    dividends = tmp_path / 'lake/actions/dividend.parquet'
    pq.write_table(pa.Table.from_pylist([ordinary, restructuring]), dividends)
    pq.write_table(pa.table({'exchange': ['SSE'], 'cal_date': ['20190327'], 'is_open': [1]}),
                   tmp_path / 'lake/meta/trade_cal.parquet')
    (tmp_path / 'snapshot_manifest.json').write_text('{"derived_files_sha256": {}}', encoding='utf-8')
    facts = dict(resolutions=[], listing_blocks=[], conditional_zero_entitlement=[event],
                 no_account_entitlement=[exclusion])
    _, events = reconcile_history(tmp_path, facts, '2018-01-01', '2019-12-31')
    assert pq.read_table(dividends).to_pylist() == [ordinary]
    assert events == [event]
    with pytest.raises(ValueError, match=r'600828.SH 2019-03-27 quantity=8200'):
        verify_zero_entitlements([dict(date='2019-03-27', holdings={'600828.SH': '8200'})], events)


def test_zero_bands_are_only_unavailable_for_nontrading_sessions():
    from types import SimpleNamespace
    from examples.closeout_data import apply_declared_lifecycle_rules
    stopped=dict(trade_status='SUSPENDED',limit_state='BOUNDED',up_limit=0,down_limit=0)
    active=dict(trade_status='OPEN',limit_state='BOUNDED',up_limit=0,down_limit=0)
    data=SimpleNamespace(instruments=[],rules={'closed':stopped,'open':active},snapshot={})
    apply_declared_lifecycle_rules(data,{'listing_blocks':[]})
    assert stopped==dict(trade_status='SUSPENDED',limit_state='UNAVAILABLE')
    assert active['limit_state']=='BOUNDED'
