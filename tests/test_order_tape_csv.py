from datetime import datetime
from decimal import Decimal

import pytest

from examples.replay_order_tape import load_order_csv


def test_csv_preserves_ids_prices_and_explicit_times(tmp_path):
    path = tmp_path / 'orders.csv'
    path.write_text('decision_time,symbol,side,quantity,limit_price,intent_id,earliest_execution\n'
                    '2025-05-06T15:00:00+08:00,600519.SH,BUY,100,10.50,0007,2025-05-07T09:30:00+08:00\n', encoding='utf-8')
    entries = load_order_csv(path)
    assert isinstance(entries, tuple)
    assert len(entries) == 1
    assert entries[0].decision_time == datetime.fromisoformat('2025-05-06T15:00:00+08:00')
    assert entries[0].intent.intent_id == '0007'
    assert entries[0].intent.limit_price == Decimal('10.50')
    assert isinstance(entries[0].intent.limit_price, Decimal)
    assert entries[0].intent.earliest_execution == datetime.fromisoformat('2025-05-07T09:30:00+08:00')


def test_optional_values_are_none(tmp_path):
    path = tmp_path / 'orders.csv'
    path.write_text('decision_time,symbol,side,quantity\n2025-05-06T15:00:00+08:00,600519.SH,BUY,100\n', encoding='utf-8')
    intent = load_order_csv(path)[0].intent
    assert (intent.limit_price, intent.intent_id, intent.earliest_execution) == (None, None, None)


@pytest.mark.parametrize('text', [
    'decision_time,symbol,side,quantity,typo\n',
    'decision_time,symbol,side\n',
    'decision_time,symbol,side,quantity,quantity\n',
    'decision_time,symbol,side,quantity\n2025-05-06T15:00:00+08:00,600519.SH,BUY\n',
    'decision_time,symbol,side,quantity\n2025-05-06T15:00:00+08:00,600519.SH,BUY,100,extra\n',
    *[f'decision_time,symbol,side,quantity\n2025-05-06T15:00:00+08:00,600519.SH,BUY,{quantity}\n'
      for quantity in ('0', '-1', '1.5', '100.0', '1e2', '', 'True')],
    *[f'decision_time,symbol,side,quantity\n{stamp},600519.SH,BUY,100\n'
      for stamp in ('2025-05-06', '2025-05-06T15:00:00', 'invalid')],
    'decision_time,symbol,side,quantity,earliest_execution\n2025-05-06T15:00:00+08:00,600519.SH,BUY,100,2025-05-07\n',
    'decision_time,symbol,side,quantity,limit_price\n2025-05-06T15:00:00+08:00,600519.SH,BUY,100,invalid\n',
])
def test_csv_rejects_malformed_inputs(tmp_path, text):
    path = tmp_path / 'orders.csv'
    path.write_text(text, encoding='utf-8')
    with pytest.raises(ValueError):
        load_order_csv(path)
