from datetime import date, datetime, timedelta, tzinfo
from decimal import Decimal

import pytest

from engine.frozen_rules import freeze_rules
from engine.prepared import _freeze
from engine.reproducibility import canonical, digest


def test_rows_detach_and_freeze_all_containers():
    key = ('600000.SH', date(2025, 5, 6))
    source = {key: {'limit': 10, 'metadata': {'tags': ['original']}}}
    frozen = freeze_rules(source, _freeze)
    expected = digest(_freeze(source))
    source[key]['metadata']['tags'].append('changed')
    source[key]['limit'] = 20
    assert frozen[key]['limit'] == 10
    assert frozen[key]['metadata']['tags'] == ('original',)
    assert digest(frozen) == expected
    with pytest.raises(TypeError):
        frozen[key] = {}
    with pytest.raises(TypeError):
        frozen[key]['limit'] = 30
    with pytest.raises(TypeError):
        frozen[key]['metadata']['tags'] = ()
    with pytest.raises(TypeError):
        frozen[key]._values = ()
    with pytest.raises(TypeError):
        del frozen[key]._schema
    with pytest.raises(TypeError):
        frozen[key]._schema[1]['limit'] = 1


def test_validated_scalar_tuple_key_is_reused_after_source_replacement():
    key = ('600000.SH', date(2025, 5, 6))
    source = {key: {'limit': 10}}
    frozen = freeze_rules(source, _freeze)
    assert next(iter(frozen)) is key
    source.clear()
    source[key] = {'limit': 99}
    assert list(frozen) == [key] and frozen[key]['limit'] == 10


def test_nested_and_subclass_tuple_keys_keep_validated_clones():
    class TupleSubclass(tuple):
        pass

    for key in ((('nested',),), TupleSubclass(('symbol', date(2025, 5, 6)))):
        frozen = freeze_rules({key: {'limit': 10}}, _freeze)
        actual = next(iter(frozen))
        assert actual == key and actual is not key and type(actual) is tuple


def test_schema_order_missing_none_and_exact_numeric_types():
    source = {'a': {'x': None, 'n': 2 ** 100, 'price': Decimal('1.2300')},
              'b': {'x': None, 'n': 7, 'price': Decimal('2')},
              'c': {'n': 8, 'x': None}, 'd': {'n': 9}}
    frozen = freeze_rules(source, _freeze)
    assert frozen['a']._schema is frozen['b']._schema
    assert frozen['a']._schema is not frozen['c']._schema
    assert list(frozen['c']) == ['n', 'x']
    assert list(frozen['a'].items()) == list(source['a'].items())
    items = frozen['a'].items()
    assert len(items) == 3 and ('x', None) in items
    assert list(items) == list(items)
    assert frozen['a'] == source['a']
    assert type(frozen['a']['n']) is int and frozen['a']['n'] == 2 ** 100
    assert frozen['a']['price'].as_tuple() == Decimal('1.2300').as_tuple()
    assert frozen['c']['x'] is None and 'x' not in frozen['d']
    with pytest.raises(KeyError):
        frozen['d']['x']
    assert canonical(frozen) == canonical(_freeze(source))
    assert digest({'rules': frozen}) == digest({'rules': _freeze(source)})


def test_nonstring_fields_and_string_collisions_keep_canonical_hash():
    source = {('symbol', date(2025, 5, 6)): {1: 'integer', '1': 'string',
              (1, 2): {'nested': [Decimal('3.20')]}, None: False}}
    frozen = freeze_rules(source, _freeze)
    row = next(iter(frozen.values()))
    assert list(row) == list(next(iter(source.values())))
    assert row[1] == 'integer' and row['1'] == 'string'
    assert canonical(frozen) == canonical(_freeze(source))
    assert digest({'rules': frozen}) == digest({'rules': _freeze(source)})


def test_equal_field_keys_retain_their_original_types():
    source = {'integer': {1: 'a'}, 'boolean': {True: 'b'},
              'decimal': {Decimal(1): 'c'}, 'tuple_int': {(1,): 'd'},
              'tuple_bool': {(True,): 'e'}}
    frozen = freeze_rules(source, _freeze)
    for key, row in source.items():
        original = next(iter(row))
        actual = next(iter(frozen[key]))
        assert type(actual) is type(original)
        if isinstance(original, tuple):
            assert type(actual[0]) is type(original[0])
    assert digest(frozen) == digest(_freeze(source))


def test_equal_nonstring_keys_retain_decimal_scale_and_signed_zero():
    source = {'decimal_short': {Decimal('1.0'): 'a'},
              'decimal_long': {Decimal('1.00'): 'b'},
              'negative_zero': {-0.0: 'c'}, 'positive_zero': {0.0: 'd'}}
    frozen = freeze_rules(source, _freeze)
    for name, row in source.items():
        assert str(next(iter(frozen[name]))) == str(next(iter(row)))
    assert canonical(frozen) == canonical(_freeze(source))
    assert digest(frozen) == digest(_freeze(source))


def test_unsupported_values_and_mutable_datetime_timezone_are_rejected():
    class MutableZone(tzinfo):
        def utcoffset(self, dt):
            return timedelta(hours=8)

    for value in (set(), {'tags': [set()]}, datetime(2025, 5, 6, tzinfo=MutableZone())):
        with pytest.raises(TypeError):
            freeze_rules({'row': {'value': value}}, _freeze)
