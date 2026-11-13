"""Read-only advisor table browser: filters, joins, and HTML escaping."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete

from predictor.models.advisor import (
    AdvisorCycle,
    AdvisorDecision,
    AdvisorStrategy,
    AdvisorStrategyVersion,
)
from predictor.models.catalog import League
from predictor.models.fixtures import Fixture, Team
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.settings import Settings
from predictor.services.advisor.browse import (
    BrowseError,
    clamp_limit,
    get_row,
    list_rows,
    table_counts,
)
from predictor.ui.advisor_browse import render_browse_row

LEAGUE = 882_101
HOME = 882_102
AWAY = 882_103
FIXTURE = 882_104
NAME = "advisor-browse-test"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def test_unknown_table_is_not_found() -> None:
    with pytest.raises(BrowseError) as raised:
        clamp_limit(0)
    assert raised.value.code == "bad_limit"
    assert clamp_limit(10000) == 100
    assert clamp_limit(None) == 50


def test_browse_module_has_no_writes() -> None:
    text = Path("src/predictor/services/advisor/browse.py").read_text(encoding="utf-8")
    for banned in (
        "session.add",
        "session.delete",
        "session.execute(delete",
        "session.execute(update",
        "session.execute(insert",
        ".commit(",
        "on_conflict",
    ):
        assert banned not in text


@pytest.mark.asyncio
async def test_list_rejects_unknown_table_and_bad_filter() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            with pytest.raises(BrowseError) as unknown:
                await list_rows(
                    session,
                    "advisor_missing",
                    filters={},
                    before_id=None,
                    limit=10,
                )
            assert unknown.value.code == "unknown_table"
            with pytest.raises(BrowseError) as bad:
                await list_rows(
                    session,
                    "advisor_decisions",
                    filters={"hack": "1"},
                    before_id=None,
                    limit=10,
                )
            assert bad.value.code == "bad_filter"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_decision_list_joins_match_and_detail_escapes() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    strategy_id = 0
    version_id = 0
    decision_id = 0
    try:
        async with factory() as session, session.begin():
            version_id, strategy_id = await _seed(session)
            cycle = AdvisorCycle(
                strategy_version_id=version_id,
                mode="sandbox",
                provider="google",
                model="gemini",
                model_version="1",
                prompt_version="p1",
                prompt_text="prompt",
                input_json={"ok": True},
                raw_output="{}",
                validation_status="ok",
                cost_pln=Decimal("0.0100"),
                started_at=NOW,
            )
            session.add(cycle)
            await session.flush()
            decision = AdvisorDecision(
                cycle_id=cycle.id,
                strategy_id=strategy_id,
                fixture_id=FIXTURE,
                period="regulation",
                goals_home=0,
                goals_away=1,
                ordinal=1,
                candidate_rule="favorite_losing",
                decision="bet",
                reasons=["edge"],
                rationale="safe <script>alert(1)</script>",
                odd_at_eval=Decimal("1.850"),
                n=1,
                created_at=NOW,
            )
            session.add(decision)
            await session.flush()
            decision_id = int(decision.id)

        async with factory() as session:
            counts = await table_counts(session)
            names = {item["table"] for item in counts}
            assert "advisor_decisions" in names
            listed = await list_rows(
                session,
                "advisor_decisions",
                filters={"strategy_id": str(strategy_id)},
                before_id=None,
                limit=10000,
            )
            assert listed["limit"] == 100
            assert listed["rows"]
            row = listed["rows"][0]
            assert row["strategy_name"] == NAME
            assert row["match"] is not None
            assert row["match"]["home"] == "BrowseHome"
            assert row["match"]["away"] == "BrowseAway"
            assert "BrowseHome" in row["match"]["label"]
            assert row["cycle"]["mode"] == "sandbox"

            detail = await get_row(session, "advisor_decisions", decision_id)
            html = render_browse_row(table="advisor_decisions", row=detail["row"])
            assert "BrowseHome" in html
            assert "<script>" not in html
            assert "&lt;script&gt;" in html

            missing = None
            try:
                await get_row(session, "advisor_decisions", decision_id + 9_999_999)
            except BrowseError as exc:
                missing = exc.code
            assert missing == "missing"
    finally:
        async with factory() as session, session.begin():
            await _cleanup(session, strategy_id, version_id)
        await engine.dispose()


async def _seed(session) -> tuple[int, int]:
    session.add(League(id=LEAGUE, name="Browse League"))
    session.add(Team(id=HOME, name="BrowseHome"))
    session.add(Team(id=AWAY, name="BrowseAway"))
    await session.flush()
    session.add(
        Fixture(
            id=FIXTURE,
            league_id=LEAGUE,
            season=2026,
            home_team_id=HOME,
            away_team_id=AWAY,
            goals_home=0,
            goals_away=1,
            status_short="2H",
            date=NOW,
        )
    )
    strategy = AdvisorStrategy(name=NAME, kind="llm", status="sandbox")
    session.add(strategy)
    await session.flush()
    version = AdvisorStrategyVersion(
        strategy_id=strategy.id,
        version_no=1,
        prompt_version="p1",
        min_combined_odd=Decimal("1.600"),
        max_legs=3,
        max_minute_regulation=80,
        max_minute_extra=110,
        wait_minutes=10,
        candidate_rules=["favorite_losing"],
    )
    session.add(version)
    await session.flush()
    return int(version.id), int(strategy.id)


async def _cleanup(session, strategy_id: int, version_id: int) -> None:
    if strategy_id:
        await session.execute(
            delete(AdvisorDecision).where(AdvisorDecision.strategy_id == strategy_id)
        )
    if version_id:
        await session.execute(
            delete(AdvisorCycle).where(AdvisorCycle.strategy_version_id == version_id)
        )
        await session.execute(
            delete(AdvisorStrategyVersion).where(
                AdvisorStrategyVersion.id == version_id
            )
        )
    if strategy_id:
        await session.execute(
            delete(AdvisorStrategy).where(AdvisorStrategy.id == strategy_id)
        )
    await session.execute(delete(Fixture).where(Fixture.id == FIXTURE))
    await session.execute(delete(Team).where(Team.id.in_((HOME, AWAY))))
    await session.execute(delete(League).where(League.id == LEAGUE))
