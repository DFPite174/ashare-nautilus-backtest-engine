from collections.abc import Mapping
from decimal import Decimal
import weakref

import pytest

from engine.reproducibility import HistorySnapshot
from engine.snapshots import SnapshotMap


def test_snapshots_retain_values_and_hide_future_keys():
    source = SnapshotMap()
    empty = source.snapshot()
    source.update('b', Decimal(1))
    first = source.snapshot()
    source.update('a', Decimal(2))
    source.update('b', Decimal(3))
    second = source.snapshot()
    source.update('b', Decimal(0))
    assert isinstance(first, Mapping)
    assert dict(empty) == {} and len(empty) == 0
    assert dict(first) == {'b': Decimal(1)} and len(first) == 1
    assert first == {'b': Decimal(1)}
    assert list(second) == ['b', 'a']
    assert dict(second) == {'b': Decimal(3), 'a': Decimal(2)}
    assert source.snapshot()['b'] == 0
    assert first.get('a', 'missing') == 'missing'
    assert first.get('unknown') is None
    with pytest.raises(KeyError):
        first['a']
    with pytest.raises(KeyError):
        first['unknown']
    with pytest.raises(TypeError):
        first['b'] = Decimal(9)


def test_unchanged_updates_do_not_retain_duplicate_versions():
    source = SnapshotMap()
    source.update('a', Decimal(1))
    for _ in range(10):
        source.update('a', Decimal('1.0'))
    assert len(source._revisions['a']) == 1
    saved = source.snapshot()
    source.update('a', Decimal(2))
    assert len(source._revisions['a']) == 2
    assert saved['a'] == Decimal(1)


def test_nested_history_keeps_its_captured_prefix():
    bars = [1]
    source = SnapshotMap()
    source.update('a', HistorySnapshot(bars))
    before = source.snapshot()
    bars.append(2)
    source.update('a', HistorySnapshot(bars))
    after = source.snapshot()
    bars.append(3)
    assert list(before['a']) == [1]
    assert list(after['a']) == [1, 2]


class ImmutableValue:
    __slots__ = ('__weakref__',)


def test_obsolete_values_are_released_without_saved_snapshots():
    source = SnapshotMap()
    value = ImmutableValue()
    obsolete = weakref.ref(value)
    source.update('a', value)
    del value
    source.update('a', ImmutableValue())
    assert obsolete() is None


def test_old_values_survive_live_snapshots_then_are_reclaimed():
    source = SnapshotMap()
    value = ImmutableValue()
    obsolete = weakref.ref(value)
    source.update('a', value)
    saved = source.snapshot()
    saved_ref = weakref.ref(saved)
    del value
    source.update('a', ImmutableValue())
    assert saved['a'] is obsolete()
    later = source.snapshot()
    source.update('a', ImmutableValue())
    assert saved['a'] is obsolete()
    del saved
    assert saved_ref() is None
    source.update('a', ImmutableValue())
    assert obsolete() is None
    assert isinstance(later['a'], ImmutableValue)


@pytest.mark.parametrize('retain_old', [False, True])
def test_temporary_snapshot_tracking_is_reclaimed_without_updates(retain_old):
    source = SnapshotMap()
    saved = source.snapshot() if retain_old else None
    for _ in range(10000):
        source.snapshot()
    assert len(source._snapshots) == int(retain_old)
    if retain_old:
        assert dict(saved) == {}
        del saved
        assert len(source._snapshots) == 0
