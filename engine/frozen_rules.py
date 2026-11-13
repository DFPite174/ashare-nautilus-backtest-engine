"""Detached rule mappings with one shared field index per row schema."""
from collections.abc import ItemsView, Mapping
from datetime import date
from decimal import Decimal
from types import MappingProxyType


_SCALARS = (str, int, float, bool, Decimal, date, type(None))


class _RuleItems(ItemsView):
    def __iter__(self):
        return zip(self._mapping._schema[0], self._mapping._values)


class _FrozenRule(Mapping):
    __slots__ = ('_schema', '_values')

    def __init__(self, schema, values):
        object.__setattr__(self, '_schema', schema)
        object.__setattr__(self, '_values', values)

    def __setattr__(self, name, value):
        raise TypeError('Frozen rule is immutable')

    def __delattr__(self, name):
        raise TypeError('Frozen rule is immutable')

    def __getitem__(self, key):
        return self._values[self._schema[1][key]]

    def __iter__(self):
        return iter(self._schema[0])

    def __len__(self):
        return len(self._values)

    def items(self):
        return _RuleItems(self)


def freeze_rules(rules, freeze_scalar):
    """Freeze rules using the supplied recursive freezer for non-scalar values.

    Schema interning is local to this call, so unused preparations retain no cache.
    Exact scalar types alone bypass the freezer; datetime and subclasses must pass
    its validation, including checks for mutable timezone implementations.
    """
    if not isinstance(rules, Mapping):
        return freeze_scalar(rules)
    schemas = {}
    frozen = {}
    for key, row in rules.items():
        frozen_key = freeze_scalar(key)
        if (type(key) is tuple and type(frozen_key) is tuple and len(key) == len(frozen_key)
                and all(original is detached for original, detached in zip(key, frozen_key))):
            frozen_key = key
        if not isinstance(row, Mapping):
            frozen[frozen_key] = freeze_scalar(row)
            continue
        fields = tuple(k if type(k) in _SCALARS else freeze_scalar(k) for k in row)
        share_schema = all(type(k) is str for k in fields)
        schema = schemas.get(fields) if share_schema else None
        if schema is None:
            schema = (fields, MappingProxyType({k: i for i, k in enumerate(fields)}))
            if share_schema:
                schemas[fields] = schema
        values = tuple(v if type(v) in _SCALARS else freeze_scalar(v) for v in row.values())
        frozen[frozen_key] = _FrozenRule(schema, values)
    return MappingProxyType(frozen)
