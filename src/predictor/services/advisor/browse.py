"""Read-only browse of advisor_* tables. Status process only; no writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from predictor.models.advisor import (
    AdvisorCounterfactual,
    AdvisorCoupon,
    AdvisorCouponEvent,
    AdvisorCycle,
    AdvisorDecision,
    AdvisorLangfuseOutbox,
    AdvisorLeg,
    AdvisorSettlementEvent,
    AdvisorStrategy,
    AdvisorStrategyEvent,
    AdvisorStrategyVersion,
    AdvisorTelegramOutbox,
)
from predictor.models.catalog import League
from predictor.models.fixtures import Fixture, Team

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100


class BrowseError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    model: type[Any]
    list_columns: tuple[str, ...]
    detail_extra: tuple[str, ...]
    filters: frozenset[str]


TABLES: dict[str, TableSpec] = {
    "advisor_strategies": TableSpec(
        name="advisor_strategies",
        model=AdvisorStrategy,
        list_columns=("id", "name", "status", "kind", "created_at"),
        detail_extra=(),
        filters=frozenset({"status", "kind"}),
    ),
    "advisor_strategy_versions": TableSpec(
        name="advisor_strategy_versions",
        model=AdvisorStrategyVersion,
        list_columns=(
            "id",
            "strategy_id",
            "version_no",
            "prompt_version",
            "min_combined_odd",
            "max_legs",
            "max_minute_regulation",
            "max_minute_extra",
            "wait_minutes",
            "created_at",
        ),
        detail_extra=("candidate_rules", "notes"),
        filters=frozenset({"strategy_id"}),
    ),
    "advisor_strategy_events": TableSpec(
        name="advisor_strategy_events",
        model=AdvisorStrategyEvent,
        list_columns=(
            "id",
            "strategy_id",
            "previous_status",
            "new_status",
            "source",
            "created_at",
        ),
        detail_extra=("note",),
        filters=frozenset({"strategy_id", "source"}),
    ),
    "advisor_cycles": TableSpec(
        name="advisor_cycles",
        model=AdvisorCycle,
        list_columns=(
            "id",
            "strategy_version_id",
            "mode",
            "validation_status",
            "provider",
            "model",
            "cost_pln",
            "latency_ms",
            "started_at",
        ),
        detail_extra=(
            "finished_at",
            "model_version",
            "prompt_version",
            "input_tokens",
            "output_tokens",
            "thoughts_tokens",
            "prompt_text",
            "input_json",
            "raw_output",
            "langfuse_trace_id",
            "otel_trace_id",
        ),
        filters=frozenset({"mode", "validation_status", "strategy_version_id"}),
    ),
    "advisor_decisions": TableSpec(
        name="advisor_decisions",
        model=AdvisorDecision,
        list_columns=(
            "id",
            "cycle_id",
            "strategy_id",
            "fixture_id",
            "decision",
            "period",
            "n",
            "odd_at_eval",
            "goals_home",
            "goals_away",
            "ordinal",
            "candidate_rule",
            "created_at",
        ),
        detail_extra=(
            "reasons",
            "rationale",
            "estimated_probability",
            "confidence",
        ),
        filters=frozenset({"strategy_id", "fixture_id", "decision", "cycle_id"}),
    ),
    "advisor_counterfactuals": TableSpec(
        name="advisor_counterfactuals",
        model=AdvisorCounterfactual,
        list_columns=("id", "decision_id", "goal_scored", "odd", "recorded_at"),
        detail_extra=(),
        filters=frozenset({"decision_id"}),
    ),
    "advisor_coupons": TableSpec(
        name="advisor_coupons",
        model=AdvisorCoupon,
        list_columns=(
            "id",
            "mode",
            "status",
            "strategy_version_id",
            "cycle_id",
            "stake",
            "combined_odd",
            "proposed_combined_odd",
            "payout_keep",
            "placed_at",
            "created_at",
        ),
        detail_extra=("proposed_stake",),
        filters=frozenset({"mode", "status", "strategy_version_id", "cycle_id"}),
    ),
    "advisor_legs": TableSpec(
        name="advisor_legs",
        model=AdvisorLeg,
        list_columns=(
            "id",
            "coupon_id",
            "strategy_id",
            "fixture_id",
            "period",
            "n",
            "odd",
            "proposed_odd",
            "goals_home",
            "goals_away",
            "elapsed",
            "status",
            "mode",
            "created_at",
        ),
        detail_extra=(),
        filters=frozenset({"coupon_id", "fixture_id", "strategy_id", "status", "mode"}),
    ),
    "advisor_coupon_events": TableSpec(
        name="advisor_coupon_events",
        model=AdvisorCouponEvent,
        list_columns=(
            "id",
            "coupon_id",
            "previous_status",
            "new_status",
            "source",
            "created_at",
        ),
        detail_extra=("note",),
        filters=frozenset({"coupon_id", "source"}),
    ),
    "advisor_settlement_events": TableSpec(
        name="advisor_settlement_events",
        model=AdvisorSettlementEvent,
        list_columns=(
            "id",
            "coupon_id",
            "leg_id",
            "previous_status",
            "new_status",
            "source",
            "created_at",
        ),
        detail_extra=(),
        filters=frozenset({"coupon_id", "source"}),
    ),
    "advisor_langfuse_outbox": TableSpec(
        name="advisor_langfuse_outbox",
        model=AdvisorLangfuseOutbox,
        list_columns=(
            "id",
            "cycle_id",
            "status",
            "attempts",
            "last_error",
            "created_at",
        ),
        detail_extra=("payload", "trace_id"),
        filters=frozenset({"status", "cycle_id"}),
    ),
    "advisor_telegram_outbox": TableSpec(
        name="advisor_telegram_outbox",
        model=AdvisorTelegramOutbox,
        list_columns=("id", "kind", "status", "attempts", "last_error", "created_at"),
        detail_extra=("payload",),
        filters=frozenset({"status", "kind"}),
    ),
}

TABLE_NAMES: tuple[str, ...] = tuple(TABLES)


def clamp_limit(raw: int | None) -> int:
    if raw is None:
        return _DEFAULT_LIMIT
    if raw < 1:
        raise BrowseError("bad_limit")
    return min(raw, _MAX_LIMIT)


async def table_counts(session: AsyncSession) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, spec in TABLES.items():
        count = int(
            await session.scalar(select(func.count()).select_from(spec.model)) or 0
        )
        out.append({"table": name, "rows": count})
    return out


async def list_rows(
    session: AsyncSession,
    table: str,
    *,
    filters: dict[str, str],
    before_id: int | None,
    limit: int | None,
) -> dict[str, Any]:
    spec = _spec(table)
    lim = clamp_limit(limit)
    where = _filters(spec, filters)
    stmt = select(spec.model)
    for clause in where:
        stmt = stmt.where(clause)
    if before_id is not None:
        stmt = stmt.where(spec.model.id < before_id)
    stmt = stmt.order_by(spec.model.id.desc()).limit(lim)
    models = list((await session.scalars(stmt)).all())
    rows = [_plain(spec, row, detail=False) for row in models]
    await _enrich(session, table, rows)
    next_before = int(rows[-1]["id"]) if rows else None
    return {
        "table": table,
        "limit": lim,
        "before_id": before_id,
        "next_before_id": next_before if len(rows) == lim else None,
        "filters": {key: filters[key] for key in sorted(filters)},
        "rows": rows,
    }


async def get_row(session: AsyncSession, table: str, row_id: int) -> dict[str, Any]:
    spec = _spec(table)
    row = await session.get(spec.model, row_id)
    if row is None:
        raise BrowseError("missing")
    payload = _plain(spec, row, detail=True)
    await _enrich(session, table, [payload])
    return {"table": table, "row": payload}


def _spec(table: str) -> TableSpec:
    try:
        return TABLES[table]
    except KeyError:
        raise BrowseError("unknown_table") from None


def _filters(spec: TableSpec, filters: dict[str, str]) -> list[Any]:
    clauses: list[Any] = []
    for key, raw in filters.items():
        if key not in spec.filters:
            raise BrowseError("bad_filter")
        column = getattr(spec.model, key)
        clauses.append(column == _cast(column, raw))
    return clauses


def _cast(column: Any, raw: str) -> Any:
    python_type = getattr(column.type, "python_type", str)
    if python_type is int:
        try:
            return int(raw)
        except ValueError:
            raise BrowseError("bad_filter") from None
    if python_type is bool:
        lowered = raw.casefold()
        if lowered in {"1", "true", "t", "yes"}:
            return True
        if lowered in {"0", "false", "f", "no"}:
            return False
        raise BrowseError("bad_filter")
    return raw


def _plain(spec: TableSpec, row: Any, *, detail: bool) -> dict[str, Any]:
    names = spec.list_columns + (spec.detail_extra if detail else ())
    return {name: _jsonable(getattr(row, name)) for name in names}


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


async def _enrich(
    session: AsyncSession, table: str, rows: list[dict[str, Any]]
) -> None:
    if not rows:
        return
    if table in {"advisor_strategy_versions", "advisor_strategy_events"}:
        await _attach_strategy_names(session, rows, "strategy_id")
    elif table == "advisor_cycles":
        await _attach_version_strategy(session, rows)
        await _attach_cycle_matches(session, rows)
    elif table == "advisor_decisions":
        await _attach_strategy_names(session, rows, "strategy_id")
        await _attach_matches(session, rows, "fixture_id")
        await _attach_cycles(session, rows, "cycle_id")
    elif table == "advisor_counterfactuals":
        await _attach_decision_context(session, rows)
    elif table == "advisor_coupons":
        await _attach_version_strategy(session, rows)
        await _attach_cycles(session, rows, "cycle_id")
        await _attach_coupon_legs(session, rows)
        for row in rows:
            if row.get("mode") == "recommendation":
                row["recommendation_path"] = f"/advisor/recommendations/{row['id']}"
    elif table == "advisor_legs":
        await _attach_strategy_names(session, rows, "strategy_id")
        await _attach_matches(session, rows, "fixture_id")
        await _attach_coupon_summary(session, rows)
    elif table == "advisor_coupon_events":
        await _attach_coupon_context(session, rows)
        await _attach_coupon_legs_for_events(session, rows)
    elif table == "advisor_settlement_events":
        await _attach_coupon_context(session, rows)
        await _attach_settlement_legs(session, rows)
    elif table == "advisor_langfuse_outbox":
        await _attach_langfuse_cycles(session, rows)


async def _attach_strategy_names(
    session: AsyncSession, rows: list[dict[str, Any]], key: str
) -> None:
    ids = _ints(rows, key)
    if not ids:
        return
    found = {
        int(row.id): row.name
        for row in (
            await session.scalars(
                select(AdvisorStrategy).where(AdvisorStrategy.id.in_(ids))
            )
        ).all()
    }
    for row in rows:
        sid = row.get(key)
        row["strategy_name"] = found.get(int(sid)) if sid is not None else None


async def _attach_version_strategy(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> None:
    ids = _ints(rows, "strategy_version_id")
    if not ids:
        return
    result = await session.execute(
        select(
            AdvisorStrategyVersion.id,
            AdvisorStrategyVersion.version_no,
            AdvisorStrategyVersion.strategy_id,
            AdvisorStrategy.name,
        )
        .join(
            AdvisorStrategy,
            AdvisorStrategy.id == AdvisorStrategyVersion.strategy_id,
        )
        .where(AdvisorStrategyVersion.id.in_(ids))
    )
    found = {
        int(vid): {
            "version_no": int(version_no),
            "strategy_id": int(strategy_id),
            "strategy_name": name,
        }
        for vid, version_no, strategy_id, name in result.all()
    }
    for row in rows:
        vid = row.get("strategy_version_id")
        info = found.get(int(vid)) if vid is not None else None
        if info is None:
            row["version_no"] = None
            row["strategy_id"] = None
            row["strategy_name"] = None
        else:
            row.update(info)


async def _attach_matches(
    session: AsyncSession, rows: list[dict[str, Any]], key: str
) -> None:
    ids = _ints(rows, key)
    labels = await _match_labels(session, ids)
    for row in rows:
        fid = row.get(key)
        row["match"] = labels.get(int(fid)) if fid is not None else None


async def _attach_cycles(
    session: AsyncSession, rows: list[dict[str, Any]], key: str
) -> None:
    ids = _ints(rows, key)
    if not ids:
        for row in rows:
            row["cycle"] = None
        return
    found = {
        int(row.id): {
            "id": int(row.id),
            "mode": row.mode,
            "validation_status": row.validation_status,
            "started_at": _jsonable(row.started_at),
        }
        for row in (
            await session.scalars(select(AdvisorCycle).where(AdvisorCycle.id.in_(ids)))
        ).all()
    }
    for row in rows:
        cid = row.get(key)
        row["cycle"] = found.get(int(cid)) if cid is not None else None


async def _attach_cycle_matches(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> None:
    cycle_ids = _ints(rows, "id")
    if not cycle_ids:
        return
    result = await session.execute(
        select(AdvisorDecision.cycle_id, AdvisorDecision.fixture_id)
        .where(AdvisorDecision.cycle_id.in_(cycle_ids))
        .order_by(AdvisorDecision.id)
    )
    by_cycle: dict[int, list[int]] = {}
    fixture_ids: set[int] = set()
    for cycle_id, fixture_id in result.all():
        cid = int(cycle_id)
        fid = int(fixture_id)
        bucket = by_cycle.setdefault(cid, [])
        if fid not in bucket:
            bucket.append(fid)
        fixture_ids.add(fid)
    labels = await _match_labels(session, fixture_ids)
    for row in rows:
        fids = by_cycle.get(int(row["id"]), [])
        row["matches"] = [labels[fid] for fid in fids if fid in labels]


async def _attach_decision_context(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> None:
    ids = _ints(rows, "decision_id")
    if not ids:
        return
    decisions = {
        int(row.id): row
        for row in (
            await session.scalars(
                select(AdvisorDecision).where(AdvisorDecision.id.in_(ids))
            )
        ).all()
    }
    strategy_ids = {int(d.strategy_id) for d in decisions.values()}
    fixture_ids = {int(d.fixture_id) for d in decisions.values()}
    names = await _strategy_map(session, strategy_ids)
    matches = await _match_labels(session, fixture_ids)
    for row in rows:
        decision = decisions.get(int(row["decision_id"]))
        if decision is None:
            row["decision"] = None
            row["strategy_name"] = None
            row["match"] = None
            continue
        row["decision"] = {
            "id": int(decision.id),
            "decision": decision.decision,
            "period": decision.period,
            "n": decision.n,
            "goals_home": decision.goals_home,
            "goals_away": decision.goals_away,
            "odd_at_eval": _jsonable(decision.odd_at_eval),
            "strategy_id": int(decision.strategy_id),
            "fixture_id": int(decision.fixture_id),
        }
        row["strategy_name"] = names.get(int(decision.strategy_id))
        row["match"] = matches.get(int(decision.fixture_id))


async def _attach_coupon_legs(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> None:
    coupon_ids = _ints(rows, "id")
    by_coupon = await _legs_by_coupon(session, coupon_ids)
    for row in rows:
        row["legs"] = by_coupon.get(int(row["id"]), [])


async def _attach_coupon_legs_for_events(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> None:
    coupon_ids = _ints(rows, "coupon_id")
    by_coupon = await _legs_by_coupon(session, coupon_ids)
    for row in rows:
        cid = row.get("coupon_id")
        row["legs"] = by_coupon.get(int(cid), []) if cid is not None else []


async def _attach_coupon_summary(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> None:
    ids = _ints(rows, "coupon_id")
    if not ids:
        return
    found = {
        int(row.id): {
            "status": row.status,
            "mode": row.mode,
            "stake": _jsonable(row.stake),
        }
        for row in (
            await session.scalars(
                select(AdvisorCoupon).where(AdvisorCoupon.id.in_(ids))
            )
        ).all()
    }
    for row in rows:
        cid = row.get("coupon_id")
        row["coupon"] = found.get(int(cid)) if cid is not None else None


async def _attach_coupon_context(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> None:
    ids = _ints(rows, "coupon_id")
    if not ids:
        return
    result = await session.execute(
        select(
            AdvisorCoupon.id,
            AdvisorCoupon.status,
            AdvisorCoupon.mode,
            AdvisorCoupon.stake,
            AdvisorCoupon.combined_odd,
            AdvisorStrategyVersion.version_no,
            AdvisorStrategy.name,
        )
        .join(
            AdvisorStrategyVersion,
            AdvisorStrategyVersion.id == AdvisorCoupon.strategy_version_id,
        )
        .join(
            AdvisorStrategy,
            AdvisorStrategy.id == AdvisorStrategyVersion.strategy_id,
        )
        .where(AdvisorCoupon.id.in_(ids))
    )
    found = {
        int(cid): {
            "status": status,
            "mode": mode,
            "stake": _jsonable(stake),
            "combined_odd": _jsonable(combined),
            "version_no": int(version_no),
            "strategy_name": name,
        }
        for cid, status, mode, stake, combined, version_no, name in result.all()
    }
    for row in rows:
        cid = row.get("coupon_id")
        info = found.get(int(cid)) if cid is not None else None
        row["coupon"] = info
        if info is not None:
            row["strategy_name"] = info["strategy_name"]
            row["version_no"] = info["version_no"]


async def _attach_settlement_legs(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> None:
    ids = _ints(rows, "leg_id")
    if not ids:
        for row in rows:
            row["leg"] = None
        return
    legs = {
        int(row.id): row
        for row in (
            await session.scalars(select(AdvisorLeg).where(AdvisorLeg.id.in_(ids)))
        ).all()
    }
    fixture_ids = {int(leg.fixture_id) for leg in legs.values()}
    matches = await _match_labels(session, fixture_ids)
    for row in rows:
        lid = row.get("leg_id")
        leg = legs.get(int(lid)) if lid is not None else None
        if leg is None:
            row["leg"] = None
            continue
        row["leg"] = {
            "id": int(leg.id),
            "period": leg.period,
            "n": leg.n,
            "odd": _jsonable(leg.odd),
            "status": leg.status,
            "fixture_id": int(leg.fixture_id),
            "match": matches.get(int(leg.fixture_id)),
        }


async def _attach_langfuse_cycles(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> None:
    ids = _ints(rows, "cycle_id")
    if not ids:
        return
    result = await session.execute(
        select(
            AdvisorCycle.id,
            AdvisorCycle.mode,
            AdvisorCycle.validation_status,
            AdvisorCycle.model,
            AdvisorCycle.started_at,
            AdvisorStrategyVersion.version_no,
            AdvisorStrategy.name,
        )
        .join(
            AdvisorStrategyVersion,
            AdvisorStrategyVersion.id == AdvisorCycle.strategy_version_id,
        )
        .join(
            AdvisorStrategy,
            AdvisorStrategy.id == AdvisorStrategyVersion.strategy_id,
        )
        .where(AdvisorCycle.id.in_(ids))
    )
    found = {
        int(cid): {
            "id": int(cid),
            "mode": mode,
            "validation_status": validation,
            "model": model,
            "started_at": _jsonable(started),
            "version_no": int(version_no),
            "strategy_name": name,
        }
        for cid, mode, validation, model, started, version_no, name in result.all()
    }
    for row in rows:
        cid = row.get("cycle_id")
        info = found.get(int(cid)) if cid is not None else None
        row["cycle"] = info
        if info is not None:
            row["strategy_name"] = info["strategy_name"]
            row["version_no"] = info["version_no"]


async def _legs_by_coupon(
    session: AsyncSession, coupon_ids: set[int]
) -> dict[int, list[dict[str, Any]]]:
    if not coupon_ids:
        return {}
    legs = list(
        (
            await session.scalars(
                select(AdvisorLeg)
                .where(AdvisorLeg.coupon_id.in_(coupon_ids))
                .order_by(AdvisorLeg.id)
            )
        ).all()
    )
    matches = await _match_labels(session, {int(leg.fixture_id) for leg in legs})
    out: dict[int, list[dict[str, Any]]] = {}
    for leg in legs:
        out.setdefault(int(leg.coupon_id), []).append(
            {
                "id": int(leg.id),
                "fixture_id": int(leg.fixture_id),
                "period": leg.period,
                "n": leg.n,
                "odd": _jsonable(leg.odd),
                "status": leg.status,
                "match": matches.get(int(leg.fixture_id)),
            }
        )
    return out


async def _strategy_map(session: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    return {
        int(row.id): row.name
        for row in (
            await session.scalars(
                select(AdvisorStrategy).where(AdvisorStrategy.id.in_(ids))
            )
        ).all()
    }


async def _match_labels(
    session: AsyncSession, fixture_ids: set[int]
) -> dict[int, dict[str, Any]]:
    if not fixture_ids:
        return {}
    home = aliased(Team)
    away = aliased(Team)
    result = await session.execute(
        select(
            Fixture.id,
            home.name,
            away.name,
            League.name,
            Fixture.date,
            Fixture.status_short,
            Fixture.goals_home,
            Fixture.goals_away,
        )
        .join(home, home.id == Fixture.home_team_id)
        .join(away, away.id == Fixture.away_team_id)
        .outerjoin(League, League.id == Fixture.league_id)
        .where(Fixture.id.in_(fixture_ids))
    )
    out: dict[int, dict[str, Any]] = {}
    for fid, home_name, away_name, league, kickoff, status, gh, ga in result.all():
        score = "–"
        if gh is not None and ga is not None:
            score = f"{gh}–{ga}"
        when = None
        if isinstance(kickoff, datetime):
            when = kickoff.isoformat()
        out[int(fid)] = {
            "fixture_id": int(fid),
            "home": home_name or "?",
            "away": away_name or "?",
            "league": league,
            "date": when,
            "status": status,
            "score": score,
            "label": f"{home_name or '?'} – {away_name or '?'}",
            "path": f"/live/match/{int(fid)}",
        }
    return out


def _ints(rows: list[dict[str, Any]], key: str) -> set[int]:
    found: set[int] = set()
    for row in rows:
        value = row.get(key)
        if value is not None:
            found.add(int(value))
    return found
