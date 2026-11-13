"""Validated, detached market inputs for repeated same-process research runs."""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
import hashlib
import inspect
from types import MappingProxyType
from zoneinfo import ZoneInfo
from collections import defaultdict

from engine.reproducibility import digest


@lru_cache(maxsize=8)
def _validation_version(validator):
    return hashlib.sha256(inspect.getsource(validator).encode()).hexdigest()


def _freeze(value):
    if type(value) is datetime and value.tzinfo is not None and type(value.tzinfo) not in (timezone, ZoneInfo):
        raise TypeError("Prepared datetime requires immutable timezone or ZoneInfo")
    if isinstance(value, Mapping):
        return MappingProxyType({_freeze(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if type(value) in (str, int, float, bool, Decimal, date, datetime, type(None)):
        return value
    raise TypeError(f"Unsupported prepared input value: {type(value).__name__}")


@dataclass(frozen=True, slots=True, init=False)
class PreparedBacktest:
    instruments: tuple
    bars: tuple
    actions: tuple
    sessions: tuple
    rules: Mapping
    master: Mapping
    input_sha256: str
    validation_version: str
    bars_by_day: Mapping
    daily_only: bool

    def __new__(cls, *args, **kwargs):
        raise TypeError("Use prepare_backtest() to create validated inputs")

    def assert_compatible(self):
        from engine.backtest import _validate
        if self.validation_version != _validation_version(_validate):
            raise ValueError("Market validator changed; prepare inputs again")


def prepare_backtest(instruments, bars, rules, *, corporate_actions=(), trading_days=None):
    """Detach inputs, validate market coverage once, and cache their content hash."""
    from engine.backtest import BacktestBar, _validate, TZ, at
    from contracts import Instrument, ConvertibleBondInstrument, CorporateAction

    instruments = tuple(instruments)
    if any(type(i) not in (Instrument, ConvertibleBondInstrument) for i in instruments):
        raise TypeError("Prepared inputs require official instrument models")
    instruments = tuple(type(i).model_validate(i.model_dump()) for i in instruments)
    actions = tuple(corporate_actions)
    if any(type(a) is not CorporateAction for a in actions):
        raise TypeError("Prepared inputs require official corporate action models")
    actions = tuple(CorporateAction.model_validate(a.model_dump()) for a in actions)
    bars = tuple(bars)
    if any(type(b) is not BacktestBar for b in bars):
        raise TypeError("Prepared inputs require official BacktestBar values")
    for b in bars:
        for value in (b.symbol, b.start, b.end, b.open, b.high, b.low, b.close, b.volume):
            _freeze(value)
    frozen_rules = _freeze(rules)
    if trading_days is not None:
        trading_days = tuple(_freeze(day) for day in trading_days)
    master, sessions = _validate(instruments, bars, frozen_rules, actions, Decimal(1), 1, trading_days)
    sessions = tuple(sessions)
    by_day=defaultdict(list);daily_only=True
    for bar in bars:
        day=bar.end.astimezone(TZ).date();by_day[day].append(bar)
        if bar.start!=at(day,9,30) or bar.end!=at(day,15):daily_only=False
    bars_by_day=MappingProxyType({day:tuple(by_day[day]) for day in sessions})
    input_sha256 = digest({'instruments': sorted(instruments, key=lambda i: i.symbol),
        'bars': sorted(bars, key=lambda b: (b.start, b.symbol)), 'rules': frozen_rules,
        'actions': sorted(actions, key=lambda a: (a.symbol, a.ex_date)), 'calendar': sessions})
    prepared = object.__new__(PreparedBacktest)
    for key, value in dict(instruments=instruments, bars=bars, actions=actions, sessions=sessions,
                           rules=frozen_rules, master=MappingProxyType(master),
                           input_sha256=input_sha256, validation_version=_validation_version(_validate),
                           bars_by_day=bars_by_day,daily_only=daily_only).items():
        object.__setattr__(prepared, key, value)
    return prepared
