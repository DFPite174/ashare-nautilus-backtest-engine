"""Detached daily-close strategy requests, bound to validated market inputs."""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from types import MappingProxyType
from zoneinfo import ZoneInfo

from engine.intents import OrderIntent, validate_intents
from engine.prepared import PreparedBacktest
from engine.reproducibility import digest


@dataclass(frozen=True)
class TimedOrderIntent:
    decision_time: datetime
    intent: OrderIntent


@dataclass(frozen=True, slots=True, init=False)
class PreparedOrderTape:
    entries: tuple[TimedOrderIntent, ...]
    by_day: Mapping[date, tuple[TimedOrderIntent, ...]]
    market_input_sha256: str
    sha256: str

    def __new__(cls, *args, **kwargs):
        raise TypeError('Use prepare_order_tape() to create validated requests')


def _timestamp(value, name):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f'Explicit timezone required: {name}')
    if type(value.tzinfo) not in (timezone, ZoneInfo):
        raise TypeError('Order tape requires immutable timezone or ZoneInfo')
    return value.astimezone(ZoneInfo('Asia/Shanghai'))


def prepare_order_tape(prepared, entries):
    """Validate the complete tape before execution, preserving same-time order."""
    if type(prepared) is not PreparedBacktest:
        raise TypeError('Order tape requires official PreparedBacktest inputs')
    prepared.assert_compatible()
    if not prepared.daily_only:
        raise ValueError('Order tape requires daily-close market inputs')
    copied = []
    for entry in entries:
        if type(entry) is not TimedOrderIntent or type(entry.intent) is not OrderIntent:
            raise TypeError('Order tape requires official TimedOrderIntent and OrderIntent values')
        decision = _timestamp(entry.decision_time, 'decision_time')
        if decision.date() not in prepared.sessions or decision.time() != time(15):
            raise ValueError('decision_time must equal Shanghai 15:00 on a prepared session')
        intent = entry.intent
        if (type(intent.symbol) is not str or type(intent.side) is not str
                or type(intent.quantity) is not int
                or (intent.intent_id is not None and type(intent.intent_id) is not str)):
            raise ValueError('Invalid strategy order intent')
        price = intent.limit_price
        if price is not None and type(price) not in (Decimal, str, int, float):
            raise TypeError('Order tape price must be Decimal, string, integer or float')
        earliest = None if intent.earliest_execution is None else _timestamp(intent.earliest_execution, 'earliest_execution')
        # The shared validator rejects malformed prices before Decimal conversion.
        validate_intents([intent], prepared.master, decision, set())
        copied.append(TimedOrderIntent(decision, OrderIntent(intent.symbol, intent.side,
            intent.quantity, None if price is None else Decimal(str(price)), intent.intent_id, earliest)))
    ordered = tuple(sorted(copied, key=lambda e: e.decision_time))
    seen_ids = set()
    by_day = {}
    for entry in ordered:
        validate_intents([entry.intent], prepared.master, entry.decision_time, seen_ids)
        by_day.setdefault(entry.decision_time.date(), []).append(entry)
    tape = object.__new__(PreparedOrderTape)
    for key, value in dict(entries=ordered,
            by_day=MappingProxyType({day: tuple(values) for day, values in by_day.items()}),
            market_input_sha256=prepared.input_sha256,
            sha256=digest({'market_input_sha256': prepared.input_sha256, 'entries': ordered})).items():
        object.__setattr__(tape, key, value)
    return tape
