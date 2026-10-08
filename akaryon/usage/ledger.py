from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select, text

from akaryon.database.models import UsageCostRecord, UsageReservationRecord
from akaryon.database.session import session_scope


def month_start(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month_start(now: datetime | None = None) -> datetime:
    start = month_start(now)
    return start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)


class UsageLedger:
    """Persist priced provider usage used by monthly estimated-cost limits."""

    @staticmethod
    def _serialize_budget_updates(session) -> None:
        """Take a database transaction lock shared by every server process."""
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            # Acquire SQLite's reserved write lock before reading current spend.
            session.execute(text("BEGIN IMMEDIATE"))
        elif dialect == "postgresql":
            # A transaction-scoped advisory lock serializes spend and reserve updates.
            session.execute(text("SELECT pg_advisory_xact_lock(74100920261004)"))
        else:
            raise RuntimeError("Monthly spend caps require SQLite or PostgreSQL locking support")

    def __init__(self, session_factory, model_prices: dict[str, dict[str, float]]) -> None:
        self.session_factory = session_factory
        self.model_prices = model_prices

    def record(self, task_id: str, session_id: str, provider: str, model: str,
               input_tokens: int, output_tokens: int, occurred_at: datetime | None = None,
               reservation_id: str | None = None) -> float:
        rates = self.model_prices.get(f"{provider}:{model}")
        if not rates:
            raise ValueError("Monthly cost accounting requires a price for this provider and model")
        cost = (input_tokens * rates["input_usd_per_million"] +
                output_tokens * rates["output_usd_per_million"]) / 1_000_000
        with session_scope(self.session_factory) as session:
            self._serialize_budget_updates(session)
            session.add(UsageCostRecord(task_id=task_id, session_id=session_id, provider=provider,
                                        model=model, input_tokens=input_tokens, output_tokens=output_tokens,
                                        estimated_cost_usd=cost,
                                        occurred_at=occurred_at or datetime.now(timezone.utc)))
            if reservation_id:
                session.execute(delete(UsageReservationRecord).where(UsageReservationRecord.id == reservation_id))
        return cost

    def reserve(self, task_id: str, session_id: str, provider: str, model: str,
                input_tokens: int, requested_output_tokens: int, monthly_limit: float,
                extra_cost_usd: float = 0.0) -> tuple[int, str]:
        """Atomically reserve this call's estimated maximum against the monthly cap."""
        rates = self.model_prices.get(f"{provider}:{model}")
        if not rates:
            raise ValueError("Monthly cost accounting requires a price for this provider and model")
        if extra_cost_usd < 0:
            raise ValueError("An additional cost reservation cannot be negative")
        input_rate, output_rate = rates["input_usd_per_million"], rates["output_usd_per_million"]
        with session_scope(self.session_factory) as session:
            self._serialize_budget_updates(session)
            now = datetime.now(timezone.utc)
            start, end = month_start(now), next_month_start(now)
            committed = session.scalar(select(func.coalesce(func.sum(UsageCostRecord.estimated_cost_usd), 0.0))
                                       .where(UsageCostRecord.occurred_at >= start,
                                              UsageCostRecord.occurred_at < end)) or 0.0
            pending = session.scalar(select(func.coalesce(func.sum(UsageReservationRecord.estimated_cost_usd), 0.0))
                                     .where(UsageReservationRecord.reserved_at >= start,
                                            UsageReservationRecord.reserved_at < end)) or 0.0
            remaining = monthly_limit - float(committed) - float(pending)
            input_cost = input_tokens * input_rate / 1_000_000
            available = remaining - input_cost - extra_cost_usd
            if available <= 0:
                raise ValueError("Estimated monthly cost cap would be exceeded by the input and image reservation")
            output_tokens = requested_output_tokens
            if output_rate > 0:
                output_tokens = min(output_tokens, int(available * 1_000_000 // output_rate))
            if output_tokens < 1:
                raise ValueError("Estimated monthly cost cap does not allow another output token")
            cost = input_cost + output_tokens * output_rate / 1_000_000 + extra_cost_usd
            reservation_id = str(uuid4())
            session.add(UsageReservationRecord(
                id=reservation_id, task_id=task_id, session_id=session_id,
                provider=provider, model=model, input_tokens=input_tokens,
                output_tokens=output_tokens, estimated_cost_usd=cost, reserved_at=now,
            ))
        return output_tokens, reservation_id

    def reserve_fixed_cost(self, task_id: str, session_id: str, provider: str, model: str,
                           estimated_cost_usd: float, monthly_limit: float) -> str:
        """Reserve an operator-configured amount for APIs without reliable token usage."""
        if estimated_cost_usd <= 0:
            raise ValueError("A fixed-cost reservation must be greater than zero")
        with session_scope(self.session_factory) as session:
            self._serialize_budget_updates(session)
            now = datetime.now(timezone.utc)
            start, end = month_start(now), next_month_start(now)
            committed = session.scalar(select(func.coalesce(func.sum(UsageCostRecord.estimated_cost_usd), 0.0))
                                       .where(UsageCostRecord.occurred_at >= start,
                                              UsageCostRecord.occurred_at < end)) or 0.0
            pending = session.scalar(select(func.coalesce(func.sum(UsageReservationRecord.estimated_cost_usd), 0.0))
                                     .where(UsageReservationRecord.reserved_at >= start,
                                            UsageReservationRecord.reserved_at < end)) or 0.0
            if float(committed) + float(pending) + estimated_cost_usd > monthly_limit:
                raise ValueError("Estimated monthly cost cap would be exceeded by image generation")
            reservation_id = str(uuid4())
            session.add(UsageReservationRecord(
                id=reservation_id, task_id=task_id, session_id=session_id,
                provider=provider, model=model, input_tokens=0, output_tokens=0,
                estimated_cost_usd=estimated_cost_usd, reserved_at=now,
            ))
        return reservation_id

    def settle_estimate(self, reservation_id: str) -> None:
        """Charge the conservative reservation if provider usage is unavailable."""
        with session_scope(self.session_factory) as session:
            self._serialize_budget_updates(session)
            reservation = session.get(UsageReservationRecord, reservation_id)
            if reservation is None:
                return
            session.add(UsageCostRecord(
                task_id=reservation.task_id, session_id=reservation.session_id,
                provider=reservation.provider, model=reservation.model,
                input_tokens=reservation.input_tokens, output_tokens=reservation.output_tokens,
                estimated_cost_usd=reservation.estimated_cost_usd,
                occurred_at=reservation.reserved_at,
            ))
            session.delete(reservation)

    def month_to_date(self, now: datetime | None = None) -> float:
        with session_scope(self.session_factory) as session:
            total = session.scalar(select(func.coalesce(func.sum(UsageCostRecord.estimated_cost_usd), 0.0))
                                   .where(UsageCostRecord.occurred_at >= month_start(now),
                                          UsageCostRecord.occurred_at < next_month_start(now)))
            pending = session.scalar(select(func.coalesce(func.sum(UsageReservationRecord.estimated_cost_usd), 0.0))
                                     .where(UsageReservationRecord.reserved_at >= month_start(now),
                                            UsageReservationRecord.reserved_at < next_month_start(now)))
        return float(total or 0.0) + float(pending or 0.0)
