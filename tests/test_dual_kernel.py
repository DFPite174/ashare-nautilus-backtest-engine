import numpy as np
import pandas as pd
import pytest

from examples.compare_dual_kernel import candidate_orders, target_drift


def test_only_frozen_execution_rows_can_submit_orders():
    days = pd.Index(['2021-01-04', '2021-01-05', '2021-01-06'])
    quotes = pd.DataFrame({'A': [10, 11, 12], 'B': [10, np.nan, 12]}, index=days)
    targets = [dict(signal_date=days[0], execution_date=days[1], weights={'A': .5})]
    sizes = candidate_orders(quotes, targets)
    assert sizes.loc[days[1], 'A'] == .5
    assert pd.isna(sizes.loc[days[1], 'B'])
    assert sizes.loc[[days[0], days[2]]].isna().all().all()
    targets[0]['signal_date'] = days[1]
    with pytest.raises(ValueError, match='chronology'):
        candidate_orders(quotes, targets)


def test_drift_keeps_residual_positions_and_underweight():
    assert target_drift({'A': .5}, {'A': .4, 'B': .2}) == pytest.approx(.3)
