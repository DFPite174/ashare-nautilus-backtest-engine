from dataclasses import FrozenInstanceError, replace
from datetime import timedelta, timezone, tzinfo
from decimal import Decimal

import pytest

from engine.intents import OrderIntent
from engine.order_tape import PreparedOrderTape, TimedOrderIntent, prepare_order_tape
from engine.prepared import prepare_backtest
from test_prepared_inputs import market


def prepared():
    return prepare_backtest(*market())


def entry(p, **kwargs):
    return TimedOrderIntent(p.bars[0].end, OrderIntent('600000.SH', 'BUY', 100, **kwargs))


def test_snapshot_factory_hash_and_anonymous_duplicates():
    p = prepared()
    source = [entry(p, limit_price='10.00')] * 2
    tape = prepare_order_tape(p, source)
    source.clear()
    assert len(tape.entries) == 2
    assert tape.entries[0] is not tape.entries[1]
    assert tape.entries[0].intent.limit_price == Decimal(10)
    assert type(tape.entries[0].intent.limit_price) is Decimal
    assert tape.by_day[p.sessions[0]] == tape.entries
    assert tape.market_input_sha256 == p.input_sha256
    assert tape.sha256 == prepare_order_tape(p, [entry(p, limit_price=10)] * 2).sha256
    assert tape.sha256 != prepare_order_tape(p, []).sha256
    with pytest.raises(TypeError):
        tape.by_day[p.sessions[0]] = ()
    with pytest.raises(FrozenInstanceError):
        tape.entries = ()
    with pytest.raises(TypeError, match='prepare_order_tape'):
        PreparedOrderTape()


@pytest.mark.parametrize('change', [
    lambda t: t.replace(tzinfo=None), lambda t: t - timedelta(minutes=1),
    lambda t: t + timedelta(days=1),
])
def test_decision_must_be_session_close(change):
    p = prepared()
    with pytest.raises(ValueError):
        prepare_order_tape(p, [replace(entry(p), decision_time=change(p.bars[0].end))])


@pytest.mark.parametrize('kwargs', [
    {'intent_id': ''}, {'limit_price': '10.001'}, {'limit_price': 'NaN'},
    {'earliest_execution': None, 'quantity': 0},
])
def test_invalid_intents(kwargs):
    p = prepared()
    intent = replace(entry(p).intent, **kwargs)
    with pytest.raises(ValueError):
        prepare_order_tape(p, [TimedOrderIntent(p.bars[0].end, intent)])


def test_ids_and_earliest_are_checked_before_return():
    p = prepared()
    with pytest.raises(ValueError, match='Duplicate'):
        prepare_order_tape(p, [entry(p, intent_id='same')] * 2)
    with pytest.raises(ValueError, match='precedes'):
        prepare_order_tape(p, [entry(p, earliest_execution=p.bars[0].end - timedelta(seconds=1))])


def test_timezone_and_price_inputs_cannot_retain_mutable_objects():
    class MutableZone(tzinfo):
        def utcoffset(self, dt): return timedelta(hours=8)
        def dst(self, dt): return timedelta(0)
    p = prepared()
    for field in ('decision_time', 'earliest_execution'):
        changed = p.bars[0].end.replace(tzinfo=MutableZone())
        value = replace(entry(p), decision_time=changed) if field == 'decision_time' else entry(p, earliest_execution=changed)
        with pytest.raises(TypeError, match='immutable timezone'):
            prepare_order_tape(p, [value])
    utc = replace(entry(p), decision_time=p.bars[0].end.astimezone(timezone.utc))
    assert prepare_order_tape(p, [utc]).sha256 == prepare_order_tape(p, [entry(p)]).sha256


def test_exact_types_market_binding_and_compatibility(monkeypatch):
    p = prepared()
    class CustomIntent(OrderIntent): pass
    with pytest.raises(TypeError):
        prepare_order_tape(p, [replace(entry(p), intent=CustomIntent('600000.SH', 'BUY', 100))])
    with pytest.raises(TypeError):
        prepare_order_tape(object(), [])
    instruments, bars, rules = market()
    bars[0] = replace(bars[0], volume=20000)
    other = prepare_backtest(instruments, bars, rules)
    assert prepare_order_tape(p, []).sha256 != prepare_order_tape(other, []).sha256
    def incompatible(self): raise ValueError('changed validator')
    monkeypatch.setattr(type(p), 'assert_compatible', incompatible)
    with pytest.raises(ValueError, match='changed validator'):
        prepare_order_tape(p, [])


def test_sort_is_chronological_and_stable():
    instruments, bars, rules = market()
    bars.append(replace(bars[0], start=bars[0].start + timedelta(days=1), end=bars[0].end + timedelta(days=1)))
    rules[(bars[1].symbol, bars[1].end.date())] = dict(next(iter(rules.values())))
    p = prepare_backtest(instruments, bars, rules)
    first = entry(p, intent_id='first')
    second = entry(p, intent_id='second')
    later = replace(entry(p, intent_id='later'), decision_time=bars[1].end)
    tape = prepare_order_tape(p, [later, first, second])
    assert [e.intent.intent_id for e in tape.entries] == ['first', 'second', 'later']
    with pytest.raises(ValueError, match='Duplicate'):
        prepare_order_tape(p, [first, replace(later, intent=first.intent)])


def test_minute_market_and_nonofficial_timed_entries_are_rejected():
    instruments, bars, rules = market()
    bars[0] = replace(bars[0], end=bars[0].start + timedelta(minutes=1))
    minute = prepare_backtest(instruments, bars, rules)
    with pytest.raises(ValueError, match='daily-close'):
        prepare_order_tape(minute, [])
    p = prepared()
    class CustomTimed(TimedOrderIntent): pass
    with pytest.raises(TypeError, match='official'):
        prepare_order_tape(p, [CustomTimed(p.bars[0].end, entry(p).intent)])
    class MutablePrice:
        def __str__(self): return '10'
    with pytest.raises(TypeError, match='price'):
        prepare_order_tape(p, [entry(p, limit_price=MutablePrice())])
