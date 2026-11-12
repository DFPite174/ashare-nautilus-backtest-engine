"""Strategy order intent data and atomic callback-batch validation."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    quantity: int
    limit_price: Decimal | None = None
    intent_id: str | None = None
    earliest_execution: datetime | None = None


def validate_intents(intents, master, decision_time, seen_ids):
    """Return the materialized batch; reserve IDs only after every item passes."""
    if not isinstance(decision_time, datetime) or decision_time.tzinfo is None or decision_time.utcoffset() is None:
        raise ValueError("Explicit timezone required: decision_time")
    batch = list(intents)
    batch_ids = set()
    for intent in batch:
        if (not isinstance(intent, OrderIntent) or not isinstance(intent.symbol, str)
                or intent.symbol not in master or not isinstance(intent.side, str)
                or intent.side.upper() not in {"BUY", "SELL"}
                or not isinstance(intent.quantity, int) or isinstance(intent.quantity, bool)
                or intent.quantity <= 0):
            raise ValueError("Invalid strategy order intent")
        if intent.limit_price is not None:
            try:
                price = Decimal(str(intent.limit_price))
                valid = price.is_finite() and price > 0 and not price % master[intent.symbol].tick_size
            except (InvalidOperation, ValueError, TypeError):
                valid = False
            if not valid:
                raise ValueError("Invalid strategy limit price or price tick")
        if intent.intent_id is not None:
            if not isinstance(intent.intent_id, str) or not intent.intent_id.strip():
                raise ValueError("intent_id must be a nonempty string")
            if intent.intent_id in seen_ids or intent.intent_id in batch_ids:
                raise ValueError(f"Duplicate intent_id: {intent.intent_id}")
            batch_ids.add(intent.intent_id)
        earliest = intent.earliest_execution
        if earliest is not None:
            if not isinstance(earliest, datetime) or earliest.tzinfo is None or earliest.utcoffset() is None:
                raise ValueError("Explicit timezone required: earliest_execution")
            if earliest < decision_time:
                raise ValueError("earliest_execution precedes decision_time")
    seen_ids.update(batch_ids)
    return batch
