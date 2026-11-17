import pandas as pd
import pytest
from examples.review_value_fundamentals import available_reports, ttm_value


def test_announcement_and_actual_date_must_both_precede_signal():
    frame=pd.DataFrame([
        dict(ts_code='A',end_date='20201231',ann_date='20210301',f_ann_date='20210302',profit=10),
        dict(ts_code='A',end_date='20201231',ann_date='20210301',f_ann_date='20210401',profit=20),
        dict(ts_code='B',end_date='20201231',ann_date='20210331',f_ann_date='',profit=30)])
    selected=available_reports(frame,'20210331',['profit'])
    assert list(selected.ts_code)==['A']
    assert selected.iloc[0].profit==10


def test_ttm_uses_matching_prior_period_and_blocks_conflicting_revisions():
    rows=pd.DataFrame([dict(ts_code='A',end_date=p,ann_date='20210401',profit=v)
                       for p,v in [('20191231',30),('20190930',20),('20200930',50)]])
    selected=available_reports(rows,'20210402',['profit'])
    value,parts=ttm_value(selected,'A','profit',period='20200930')
    assert value==60
    assert len(parts)==3
    assert ttm_value(selected[selected.end_date.ne('20190930')],'A','profit',period='20200930')==(None,[])
    conflict=pd.concat([rows,rows.iloc[[0]].assign(profit=31)])
    with pytest.raises(ValueError,match='Conflicting'):
        available_reports(conflict,'20210402',['profit'])
