from decimal import Decimal as D
import pytest
from examples.closeout_data import merge_identical_actions, verify_zero_entitlements


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


def test_zero_bands_are_only_unavailable_for_nontrading_sessions():
    from types import SimpleNamespace
    from examples.closeout_data import apply_declared_lifecycle_rules
    stopped=dict(trade_status='SUSPENDED',limit_state='BOUNDED',up_limit=0,down_limit=0)
    active=dict(trade_status='OPEN',limit_state='BOUNDED',up_limit=0,down_limit=0)
    data=SimpleNamespace(instruments=[],rules={'closed':stopped,'open':active},snapshot={})
    apply_declared_lifecycle_rules(data,{'listing_blocks':[]})
    assert stopped==dict(trade_status='SUSPENDED',limit_state='UNAVAILABLE')
    assert active['limit_state']=='BOUNDED'
