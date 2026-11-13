"""Sandbox stake: baseline without a model, and one LLM strategy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from predictor.constants import PAYOUT_KEEP, SANDBOX_BANKROLL, stake_after_wins
from predictor.models.advisor import (
    AdvisorCoupon,
    AdvisorCycle,
    AdvisorDecision,
    AdvisorLangfuseOutbox,
    AdvisorLeg,
    AdvisorStrategy,
    AdvisorStrategyVersion,
    AdvisorTelegramOutbox,
)
from predictor.models.catalog import League
from predictor.models.fixtures import Fixture, Team
from predictor.postgres import make_async_engine, make_session_factory
from predictor.schemas.advisor import AdvisorSettings
from predictor.schemas.settings import Settings
from predictor.services.advisor.cycle import CycleCandidate, CycleRequest, run_cycle
from predictor.services.advisor.llm.protocol import LlmCompletion
from predictor.services.advisor.sandbox import (
    LegSnapshot,
    SandboxSlip,
    place_baseline,
    place_sandbox_coupons,
    release_funded_strategies,
)
from predictor.services.advisor.telegram import (
    deliver_pending,
)

NOW = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)

_TELEGRAM_TEST_KIND = "sandbox_llm_telegram_test"


@dataclass(frozen=True, slots=True)
class _World:
    strategy_id: int
    version_id: int
    league: int
    home: int
    away: int
    fixtures: tuple[int, ...]


class _Provider:
    provider_name = "google"
    model = "gemini-2.5-flash"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def decide(
        self, prompt: str, *, timeout: float, response_schema: object
    ) -> LlmCompletion:
        self.calls += 1
        return LlmCompletion(
            text=self.text,
            provider=self.provider_name,
            model=self.model,
            model_version="gemini-2.5-flash-001",
            input_tokens=10,
            output_tokens=10,
            thoughts_tokens=0,
        )


def _settings() -> AdvisorSettings:
    return AdvisorSettings(
        llm_api_key="secret",
        llm_price_input_pln_per_mtok=Decimal("0.50"),
        llm_price_output_pln_per_mtok=Decimal("1.25"),
    )


def _leg(
    fixture_id: int,
    odd: str,
    *,
    goals_home: int = 0,
    goals_away: int = 1,
    n: int = 1,
    ordinal: int = 2,
    rule: str = "favorite_losing",
) -> CycleCandidate:
    return CycleCandidate(
        fixture_id=fixture_id,
        period="regulation",
        goals_home=goals_home,
        goals_away=goals_away,
        ordinal=ordinal,
        candidate_rule=rule,
        odd=Decimal(odd),
        n=n,
    )


def _reply(
    rows: tuple[tuple[int, str], ...],
    coupons: tuple[tuple[int, ...], ...],
    *,
    probability: str = "0.75",
) -> str:
    # Default 0.75 clears single EV (odd 1.70 x 0.88).
    return json.dumps(
        {
            "candidates": [
                {
                    "fixture_id": fixture_id,
                    "decision": decision,
                    "estimated_probability": probability,
                    "confidence": "0.40",
                    "reasons": ["favorite_pressure"],
                    "rationale": "chasing the score",
                }
                for fixture_id, decision in rows
            ],
            "coupons": [{"fixture_ids": list(ids)} for ids in coupons],
        }
    )


@pytest.mark.asyncio
async def test_a_win_raises_the_next_sandbox_stake_by_ten_groszy() -> None:
    world = _World(0, 0, 880_481, 880_482, 880_483, (880_484, 880_485))
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                world = await _seed(
                    session, world, name="advisor-sandbox-step", kind="baseline"
                )
                await _open_coupon(
                    session,
                    world,
                    fixture_id=880_484,
                    stake=stake_after_wins(0),
                    odd=Decimal("1.700"),
                )
                coupon = await session.scalar(
                    select(AdvisorCoupon).where(
                        AdvisorCoupon.strategy_version_id == world.version_id
                    )
                )
                assert coupon is not None
                coupon.status = "won"
            placed = await place_baseline(
                session,
                strategy_id=world.strategy_id,
                strategy_version_id=world.version_id,
                candidates=(_leg(880_485, "1.80"),),
                clock=lambda: NOW,
            )
            assert len(placed.coupon_ids) == 1
            async with session.begin():
                nxt = await session.get(AdvisorCoupon, placed.coupon_ids[0])
            assert nxt is not None
            assert nxt.stake == stake_after_wins(1)
    finally:
        async with factory() as session, session.begin():
            await _wipe(session, (world,))
        await engine.dispose()


@pytest.mark.asyncio
async def test_baseline_bets_every_playable_candidate_without_a_model() -> None:
    low = _leg(880_404, "1.20")
    high = _leg(880_405, "1.70")
    world = _World(0, 0, 880_401, 880_402, 880_403, (880_404, 880_405))
    shared = _World(0, 0, 880_401, 880_402, 880_403, ())
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            empty = await place_baseline(
                session,
                strategy_id=0,
                strategy_version_id=0,
                candidates=(),
                clock=lambda: NOW,
            )
            assert empty.coupon_ids == ()
            assert empty.paused is False
            async with session.begin():
                world = await _seed(
                    session, world, name="advisor-sandbox-baseline", kind="baseline"
                )
            async with session.begin():
                with pytest.raises(RuntimeError, match="open transaction"):
                    await place_baseline(
                        session,
                        strategy_id=world.strategy_id,
                        strategy_version_id=world.version_id,
                        candidates=(low, high),
                        clock=lambda: NOW,
                    )
            with pytest.raises(ValueError, match="timezone"):
                await place_baseline(
                    session,
                    strategy_id=world.strategy_id,
                    strategy_version_id=world.version_id,
                    candidates=(low,),
                    clock=lambda: datetime(2026, 10, 18, 15, 0),
                )
            with pytest.raises(ValueError, match="unknown strategy"):
                await place_baseline(
                    session,
                    strategy_id=world.strategy_id + 9_000,
                    strategy_version_id=world.version_id,
                    candidates=(low,),
                    clock=lambda: NOW,
                )
            placed = await place_baseline(
                session,
                strategy_id=world.strategy_id,
                strategy_version_id=world.version_id,
                candidates=(low, high, low),
                clock=lambda: NOW,
            )
            assert placed.paused is False
            assert len(placed.coupon_ids) == 2
            async with session.begin():
                coupons = (
                    await session.scalars(
                        select(AdvisorCoupon)
                        .where(AdvisorCoupon.strategy_version_id == world.version_id)
                        .order_by(AdvisorCoupon.id)
                    )
                ).all()
                cycles = await session.scalar(
                    select(func.count())
                    .select_from(AdvisorCycle)
                    .where(AdvisorCycle.strategy_version_id == world.version_id)
                )
                decisions = await session.scalar(
                    select(func.count())
                    .select_from(AdvisorDecision)
                    .where(AdvisorDecision.strategy_id == world.strategy_id)
                )
            assert cycles == 0
            assert decisions == 0
            assert [coupon.combined_odd for coupon in coupons] == [
                Decimal("1.200"),
                Decimal("1.700"),
            ]
            for coupon in coupons:
                assert coupon.mode == "sandbox"
                assert coupon.status == "open"
                assert coupon.stake == stake_after_wins(0)
                assert coupon.payout_keep == PAYOUT_KEEP
                assert coupon.placed_at == NOW
                assert coupon.cycle_id is None
                assert coupon.proposed_combined_odd == coupon.combined_odd
            again = await place_baseline(
                session,
                strategy_id=world.strategy_id,
                strategy_version_id=world.version_id,
                candidates=(low, high),
                clock=lambda: NOW,
            )
            assert again.coupon_ids == ()
            assert again.paused is False
            async with session.begin():
                shared = await _seed(
                    session,
                    _World(0, 0, world.league, world.home, world.away, ()),
                    name="advisor-sandbox-shared",
                    kind="llm",
                    create_match=False,
                )
            request = CycleRequest(
                strategy_id=shared.strategy_id,
                strategy_version_id=shared.version_id,
                mode="sandbox",
                prompt_version="p1",
                prompt="score",
                input_json={},
                candidates=(high,),
                open_fixture_ids=frozenset(),
                min_odd=Decimal("1.6"),
                max_legs=3,
            )
            shared_outcome = await run_cycle(
                session,
                _Provider(_reply(((880_405, "bet"),), ((880_405,),))),
                _settings(),
                request,
                clock=lambda: NOW,
            )
            assert shared_outcome.status == "ok"
            assert len(shared_outcome.coupon_ids) == 1
            async with session.begin():
                legs = (
                    await session.scalars(
                        select(AdvisorLeg).where(AdvisorLeg.fixture_id == 880_405)
                    )
                ).all()
            assert {leg.strategy_id for leg in legs} == {
                world.strategy_id,
                shared.strategy_id,
            }
    finally:
        async with factory() as session, session.begin():
            await _wipe(session, (world, shared))
        await engine.dispose()


@pytest.mark.asyncio
async def test_baseline_pauses_when_the_stake_does_not_fit() -> None:
    world = _World(0, 0, 880_411, 880_412, 880_413, (880_414, 880_415, 880_416))
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                world = await _seed(
                    session, world, name="advisor-sandbox-pause", kind="baseline"
                )
                await _open_coupon(
                    session,
                    world,
                    fixture_id=880_414,
                    stake=SANDBOX_BANKROLL - stake_after_wins(0),
                    odd=Decimal("1.500"),
                )
            placed = await place_baseline(
                session,
                strategy_id=world.strategy_id,
                strategy_version_id=world.version_id,
                candidates=(_leg(880_415, "1.15"), _leg(880_416, "1.25")),
                clock=lambda: NOW,
            )
            assert placed.paused is True
            assert len(placed.coupon_ids) == 1
            blocked = await place_baseline(
                session,
                strategy_id=world.strategy_id,
                strategy_version_id=world.version_id,
                candidates=(_leg(880_416, "1.25"),),
                clock=lambda: NOW,
            )
            assert blocked.coupon_ids == ()
            assert blocked.paused is True
            async with session.begin():
                strategy = await session.get(AdvisorStrategy, world.strategy_id)
                kept = (
                    await session.scalars(
                        select(AdvisorLeg.fixture_id).where(
                            AdvisorLeg.strategy_id == world.strategy_id
                        )
                    )
                ).all()
            assert strategy is not None
            assert strategy.status == "paused"
            assert set(kept) == {880_414, 880_415}
    finally:
        async with factory() as session, session.begin():
            await _wipe(session, (world,))
        await engine.dispose()


@pytest.mark.asyncio
async def test_llm_coupon_is_written_with_the_decision() -> None:
    low = _leg(880_424, "1.20")
    first = _leg(880_425, "1.70")
    second = _leg(880_426, "1.40", goals_home=1, goals_away=1, n=2, ordinal=3)
    world = _World(0, 0, 880_421, 880_422, 880_423, (880_424, 880_425, 880_426))
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                world = await _seed(
                    session, world, name="advisor-sandbox-llm", kind="llm"
                )
            request = _request(world, (low,))
            refused = await run_cycle(
                session,
                _Provider(_reply(((880_424, "bet"),), ((880_424,),))),
                _settings(),
                request,
                clock=lambda: NOW,
            )
            assert refused.status == "below_threshold"
            assert refused.detail == "below_threshold"
            assert refused.coupon_ids == ()
            accepted = await run_cycle(
                session,
                _Provider(
                    _reply(
                        ((880_425, "bet"), (880_426, "bet")),
                        ((880_425, 880_426),),
                    )
                ),
                _settings(),
                _request(world, (first, second)),
                clock=lambda: NOW,
            )
            assert accepted.status == "ok"
            assert len(accepted.coupon_ids) == 1
            async with session.begin():
                coupon = await session.get(AdvisorCoupon, accepted.coupon_ids[0])
                legs = (
                    await session.scalars(
                        select(AdvisorLeg)
                        .where(AdvisorLeg.coupon_id == accepted.coupon_ids[0])
                        .order_by(AdvisorLeg.fixture_id)
                    )
                ).all()
                decisions = (
                    await session.scalars(
                        select(AdvisorDecision).where(
                            AdvisorDecision.cycle_id == accepted.cycle_id
                        )
                    )
                ).all()
            assert coupon is not None
            assert coupon.cycle_id == accepted.cycle_id
            assert coupon.mode == "sandbox"
            assert coupon.status == "open"
            assert coupon.stake == stake_after_wins(0)
            assert coupon.combined_odd == Decimal("2.380")
            assert coupon.proposed_combined_odd == Decimal("2.380")
            assert coupon.payout_keep == PAYOUT_KEEP
            assert coupon.placed_at == NOW
            assert [leg.fixture_id for leg in legs] == [880_425, 880_426]
            assert legs[0].odd == Decimal("1.700")
            assert legs[0].proposed_odd == Decimal("1.700")
            assert legs[0].n == 1
            assert legs[0].goals_home == 0
            assert legs[1].n == 2
            assert legs[1].goals_home == 1
            assert legs[1].goals_away == 1
            assert {leg.strategy_id for leg in legs} == {world.strategy_id}
            assert {item.decision for item in decisions} == {"bet"}
            repeat = await run_cycle(
                session,
                _Provider(
                    _reply(
                        ((880_425, "bet"), (880_426, "bet")),
                        ((880_425, 880_426),),
                    )
                ),
                _settings(),
                _request(world, (first, second)),
                clock=lambda: NOW,
            )
            assert repeat.status == "ok"
            assert repeat.coupon_ids == ()
            async with session.begin():
                coupon_count = await session.scalar(
                    select(func.count())
                    .select_from(AdvisorCoupon)
                    .where(AdvisorCoupon.strategy_version_id == world.version_id)
                )
            assert coupon_count == 1
    finally:
        async with factory() as session, session.begin():
            await _wipe(session, (world,))
        await engine.dispose()


@pytest.mark.asyncio
async def test_llm_places_until_the_stake_no_longer_fits() -> None:
    world = _World(0, 0, 880_441, 880_442, 880_443, (880_444, 880_445, 880_446))
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                world = await _seed(
                    session, world, name="advisor-sandbox-short", kind="llm"
                )
                await _open_coupon(
                    session,
                    world,
                    fixture_id=880_444,
                    stake=SANDBOX_BANKROLL - stake_after_wins(0),
                    odd=Decimal("1.500"),
                )
            # A7 allows a single only when the cycle has one playable. Place
            # the first, pause for stake, then confirm a second cycle cannot.
            outcome = await run_cycle(
                session,
                _Provider(_reply(((880_445, "bet"),), ((880_445,),))),
                _settings(),
                _request(world, (_leg(880_445, "1.70"),)),
                clock=lambda: NOW,
            )
            assert outcome.status == "ok"
            assert len(outcome.coupon_ids) == 1
            blocked = await run_cycle(
                session,
                _Provider(_reply(((880_446, "bet"),), ((880_446,),))),
                _settings(),
                _request(world, (_leg(880_446, "1.80"),)),
                clock=lambda: NOW,
            )
            assert blocked.status == "ok"
            assert blocked.coupon_ids == ()
            async with session.begin():
                strategy = await session.get(AdvisorStrategy, world.strategy_id)
                decisions = await session.scalar(
                    select(func.count())
                    .select_from(AdvisorDecision)
                    .where(
                        AdvisorDecision.cycle_id.in_(
                            (outcome.cycle_id, blocked.cycle_id)
                        )
                    )
                )
                played = (
                    await session.scalars(
                        select(AdvisorLeg.fixture_id).where(
                            AdvisorLeg.coupon_id == outcome.coupon_ids[0]
                        )
                    )
                ).all()
            assert strategy is not None
            assert strategy.status == "paused"
            assert decisions == 2
            assert list(played) == [880_445]
    finally:
        async with factory() as session, session.begin():
            await _wipe(session, (world,))
        await engine.dispose()


@pytest.mark.asyncio
async def test_paused_strategy_keeps_the_decision_and_skips_the_coupon() -> None:
    world = _World(0, 0, 880_431, 880_432, 880_433, (880_434,))
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                world = await _seed(
                    session, world, name="advisor-sandbox-guards", kind="llm"
                )
            with pytest.raises(ValueError, match="kind"):
                await place_baseline(
                    session,
                    strategy_id=world.strategy_id,
                    strategy_version_id=world.version_id,
                    candidates=(_leg(880_434, "1.70"),),
                    clock=lambda: NOW,
                )
            with pytest.raises(ValueError, match="belong"):
                await place_baseline(
                    session,
                    strategy_id=world.strategy_id,
                    strategy_version_id=world.version_id + 1_000_000,
                    candidates=(_leg(880_434, "1.70"),),
                    clock=lambda: NOW,
                )
            async with session.begin():
                strategy = await session.get(AdvisorStrategy, world.strategy_id)
                assert strategy is not None
                strategy.status = "paused"
            outcome = await run_cycle(
                session,
                _Provider(_reply(((880_434, "bet"),), ((880_434,),))),
                _settings(),
                _request(world, (_leg(880_434, "1.70"),)),
                clock=lambda: NOW,
            )
            assert outcome.called is True
            assert outcome.status == "ok"
            assert outcome.coupon_ids == ()
            async with session.begin():
                decisions = await session.scalar(
                    select(func.count())
                    .select_from(AdvisorDecision)
                    .where(AdvisorDecision.cycle_id == outcome.cycle_id)
                )
                coupons = await session.scalar(
                    select(func.count())
                    .select_from(AdvisorCoupon)
                    .where(AdvisorCoupon.strategy_version_id == world.version_id)
                )
                strategy = await session.get(AdvisorStrategy, world.strategy_id)
            assert decisions == 1
            assert coupons == 0
            assert strategy is not None
            assert strategy.status == "paused"
    finally:
        async with factory() as session, session.begin():
            await _wipe(session, (world,))
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_open_match_does_not_block_a_new_one() -> None:
    """Skip an open leg. Drop a coupon that mixes open+new. Place a new single alone."""
    baseline = _World(0, 0, 880_451, 880_452, 880_453, (880_454, 880_455))
    llm = _World(0, 0, 880_461, 880_462, 880_463, (880_464, 880_465, 880_466))
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                baseline = await _seed(
                    session, baseline, name="advisor-sandbox-mix", kind="baseline"
                )
                await _open_coupon(
                    session,
                    baseline,
                    fixture_id=880_454,
                    stake=stake_after_wins(0),
                    odd=Decimal("1.500"),
                )
            placed = await place_baseline(
                session,
                strategy_id=baseline.strategy_id,
                strategy_version_id=baseline.version_id,
                candidates=(_leg(880_454, "1.40"), _leg(880_455, "1.20")),
                clock=lambda: NOW,
            )
            assert placed.paused is False
            assert len(placed.coupon_ids) == 1
            async with session.begin():
                fresh = (
                    await session.scalars(
                        select(AdvisorLeg.fixture_id).where(
                            AdvisorLeg.coupon_id == placed.coupon_ids[0]
                        )
                    )
                ).all()
                held = await _fixture_ids(session, baseline.strategy_id)
            assert list(fresh) == [880_455]
            assert held == [880_454, 880_455]

            async with session.begin():
                llm = await _seed(
                    session, llm, name="advisor-sandbox-mix-llm", kind="llm"
                )
                await _open_coupon(
                    session,
                    llm,
                    fixture_id=880_464,
                    stake=stake_after_wins(0),
                    odd=Decimal("1.700"),
                )
            combined = await run_cycle(
                session,
                _Provider(
                    _reply(
                        ((880_464, "bet"), (880_465, "bet")),
                        ((880_464, 880_465),),
                    )
                ),
                _settings(),
                _request(llm, (_leg(880_464, "1.70"), _leg(880_465, "1.80"))),
                clock=lambda: NOW,
            )
            assert combined.status == "ok"
            assert combined.coupon_ids == ()
            async with session.begin():
                assert await _fixture_ids(session, llm.strategy_id) == [880_464]
            # Open 880_464 stays out of the candidate set (A7: a single needs
            # exactly one playable). The new match is placed alone.
            singles = await run_cycle(
                session,
                _Provider(_reply(((880_466, "bet"),), ((880_466,),))),
                _settings(),
                _request(llm, (_leg(880_466, "1.75"),)),
                clock=lambda: NOW,
            )
            assert singles.status == "ok"
            assert len(singles.coupon_ids) == 1
            async with session.begin():
                added = (
                    await session.scalars(
                        select(AdvisorLeg.fixture_id).where(
                            AdvisorLeg.coupon_id == singles.coupon_ids[0]
                        )
                    )
                ).all()
                held = await _fixture_ids(session, llm.strategy_id)
            assert list(added) == [880_466]
            assert held == [880_464, 880_466]
    finally:
        async with factory() as session, session.begin():
            await _wipe(session, (baseline, llm))
        await engine.dispose()


@pytest.mark.asyncio
async def test_sandbox_llm_coupon_enqueues_telegram_and_baseline_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Avoid clashing with the live sandbox-llm row and outbox on predictor_dev.
    name = "sandbox-llm-telegram-test"
    monkeypatch.setattr(
        "predictor.services.advisor.sandbox.SANDBOX_LLM_NAME",
        name,
    )
    monkeypatch.setattr(
        "predictor.services.advisor.sandbox.KIND_SANDBOX_LLM",
        _TELEGRAM_TEST_KIND,
    )
    llm = _World(0, 0, 9_889_001, 9_889_002, 9_889_003, (9_889_004, 9_889_005))
    baseline = _World(0, 0, 9_889_011, 9_889_012, 9_889_013, (9_889_014,))
    other = _World(0, 0, 9_889_021, 9_889_022, 9_889_023, (9_889_024,))
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    posts: list[tuple[str, str]] = []

    async def _post(token: str, chat_id: str, text: str) -> None:
        assert token == "secret-token"
        assert "bot" not in text.casefold()
        posts.append((chat_id, text))

    try:
        async with factory() as session, session.begin():
            await _wipe_telegram_sandbox_rows(session)
        async with factory() as session, session.begin():
            llm = await _seed(session, llm, name=name, kind="llm")
            baseline = await _seed(
                session, baseline, name="baseline-telegram", kind="baseline"
            )
            other = await _seed(session, other, name="other-llm-telegram", kind="llm")
            home = await session.get(Team, llm.home)
            away = await session.get(Team, llm.away)
            assert home is not None and away is not None
            home.name = "Mes Kerman"
            away.name = "Saipa"
            session.add(Team(id=9_889_007, name="Legia"))
            session.add(Team(id=9_889_006, name="Raków"))
            await session.flush()
            second = await session.get(Fixture, 9_889_005)
            assert second is not None
            second.home_team_id = 9_889_007
            second.away_team_id = 9_889_006
        async with factory() as session, session.begin():
            placed = await place_sandbox_coupons(
                session,
                strategy_id=llm.strategy_id,
                strategy_version_id=llm.version_id,
                slips=(
                    SandboxSlip(
                        (
                            LegSnapshot(
                                fixture_id=9_889_004,
                                period="regulation",
                                goals_home=0,
                                goals_away=1,
                                odd=Decimal("1.700"),
                                n=1,
                            ),
                            LegSnapshot(
                                fixture_id=9_889_005,
                                period="regulation",
                                goals_home=1,
                                goals_away=2,
                                odd=Decimal("1.550"),
                                n=3,
                            ),
                        )
                    ),
                ),
                cycle_id=None,
                placed_at=NOW,
                kind="llm",
            )
            assert len(placed.coupon_ids) == 1
            other_placed = await place_sandbox_coupons(
                session,
                strategy_id=other.strategy_id,
                strategy_version_id=other.version_id,
                slips=(
                    SandboxSlip(
                        (
                            LegSnapshot(
                                fixture_id=9_889_024,
                                period="regulation",
                                goals_home=0,
                                goals_away=1,
                                odd=Decimal("1.700"),
                                n=1,
                            ),
                        )
                    ),
                ),
                cycle_id=None,
                placed_at=NOW,
                kind="llm",
            )
            assert len(other_placed.coupon_ids) == 1
        async with factory() as session:
            baseline_placed = await place_baseline(
                session,
                strategy_id=baseline.strategy_id,
                strategy_version_id=baseline.version_id,
                candidates=(_leg(9_889_014, "1.70"),),
                clock=lambda: NOW,
            )
        assert len(baseline_placed.coupon_ids) == 1
        async with factory() as session, session.begin():
            notices = (
                await session.scalars(
                    select(AdvisorTelegramOutbox).where(
                        AdvisorTelegramOutbox.kind == _TELEGRAM_TEST_KIND
                    )
                )
            ).all()
            assert len(notices) == 1
            text = str(notices[0].payload["text"])
            assert f"Sandbox-llm gra kupon {placed.coupon_ids[0]}" in text
            assert name not in text  # body still says Sandbox-llm, not the DB name
            assert "Mes Kerman – Saipa, 0:1, Over 1.5 @ 1.700" in text
            assert "Legia – Raków, 1:2, Over 3.5 @ 1.550" in text
            assert "Kurs łączny 2.635" in text
            assert f"Stawka {stake_after_wins(0)}" in text
            empty = await deliver_pending(
                session,
                token="secret-token",
                chat_id="",
                post=_post,
                kinds=(_TELEGRAM_TEST_KIND,),
            )
            assert empty == 0
            assert notices[0].status == "pending"
            sent = await deliver_pending(
                session,
                token="secret-token",
                chat_id="llm-chat",
                post=_post,
                kinds=(_TELEGRAM_TEST_KIND,),
            )
            assert sent == 1
            assert posts == [("llm-chat", text)]
            assert notices[0].status == "sent"
    finally:
        async with factory() as session, session.begin():
            await _wipe_telegram_sandbox_rows(session)
        await engine.dispose()


async def _wipe_telegram_sandbox_rows(session) -> None:
    names = (
        "sandbox-llm-telegram-test",
        "baseline-telegram",
        "other-llm-telegram",
    )
    strategy_ids = select(AdvisorStrategy.id).where(AdvisorStrategy.name.in_(names))
    version_ids = select(AdvisorStrategyVersion.id).where(
        AdvisorStrategyVersion.strategy_id.in_(strategy_ids)
    )
    await session.execute(
        delete(AdvisorTelegramOutbox).where(
            AdvisorTelegramOutbox.kind == _TELEGRAM_TEST_KIND
        )
    )
    await session.execute(
        delete(AdvisorLeg).where(AdvisorLeg.strategy_id.in_(strategy_ids))
    )
    await session.execute(
        delete(AdvisorCoupon).where(AdvisorCoupon.strategy_version_id.in_(version_ids))
    )
    await session.execute(
        delete(AdvisorStrategyVersion).where(
            AdvisorStrategyVersion.strategy_id.in_(strategy_ids)
        )
    )
    await session.execute(
        delete(AdvisorStrategy).where(AdvisorStrategy.name.in_(names))
    )
    await session.execute(
        delete(Fixture).where(
            Fixture.id.in_((9_889_004, 9_889_005, 9_889_014, 9_889_024))
        )
    )
    await session.execute(
        delete(Team).where(
            Team.id.in_(
                (
                    9_889_002,
                    9_889_003,
                    9_889_006,
                    9_889_007,
                    9_889_012,
                    9_889_013,
                    9_889_022,
                    9_889_023,
                )
            )
        )
    )
    await session.execute(
        delete(League).where(League.id.in_((9_889_001, 9_889_011, 9_889_021)))
    )


def _request(world: _World, candidates: tuple[CycleCandidate, ...]) -> CycleRequest:
    return CycleRequest(
        strategy_id=world.strategy_id,
        strategy_version_id=world.version_id,
        mode="sandbox",
        prompt_version="p1",
        prompt="score",
        input_json={},
        candidates=candidates,
        open_fixture_ids=frozenset(),
        min_odd=Decimal("1.6"),
        max_legs=3,
    )


@pytest.mark.asyncio
async def test_paused_strategy_returns_when_the_book_covers_the_stake() -> None:
    engine = make_async_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            funded = AdvisorStrategy(
                name="advisor-release-funded", kind="llm", status="paused"
            )
            short = AdvisorStrategy(
                name="advisor-release-short", kind="baseline", status="paused"
            )
            session.add(funded)
            session.add(short)
            await session.flush()
            version = AdvisorStrategyVersion(
                strategy_id=short.id,
                version_no=1,
                prompt_version="p4",
                min_combined_odd=Decimal("1.600"),
                max_legs=3,
                max_minute_regulation=80,
                max_minute_extra=110,
                wait_minutes=10,
                candidate_rules=["favorite_losing"],
                notes="",
            )
            session.add(version)
            await session.flush()
            session.add(
                AdvisorCoupon(
                    mode="sandbox",
                    strategy_version_id=version.id,
                    status="open",
                    stake=SANDBOX_BANKROLL - stake_after_wins(0) + Decimal("1.00"),
                    combined_odd=Decimal("1.500"),
                    proposed_combined_odd=Decimal("1.500"),
                    payout_keep=PAYOUT_KEEP,
                    placed_at=NOW,
                )
            )
            await release_funded_strategies(session)
            assert funded.status == "sandbox"
            assert short.status == "paused"
            funded_id, short_id = funded.id, short.id
        async with factory() as session, session.begin():
            await session.execute(
                delete(AdvisorCoupon).where(
                    AdvisorCoupon.strategy_version_id.in_(
                        select(AdvisorStrategyVersion.id).where(
                            AdvisorStrategyVersion.strategy_id == short_id
                        )
                    )
                )
            )
            await session.execute(
                delete(AdvisorStrategyVersion).where(
                    AdvisorStrategyVersion.strategy_id == short_id
                )
            )
            await session.execute(
                delete(AdvisorStrategy).where(
                    AdvisorStrategy.id.in_((funded_id, short_id))
                )
            )
    finally:
        await engine.dispose()


async def _seed(
    session,
    world: _World,
    *,
    name: str,
    kind: str,
    create_match: bool = True,
) -> _World:
    if create_match:
        session.add(League(id=world.league, name=name))
        session.add(Team(id=world.home, name="Home"))
        session.add(Team(id=world.away, name="Away"))
        await session.flush()
        for fixture_id in world.fixtures:
            session.add(
                Fixture(
                    id=fixture_id,
                    league_id=world.league,
                    season=2026,
                    home_team_id=world.home,
                    away_team_id=world.away,
                )
            )
    strategy = AdvisorStrategy(name=name, kind=kind, status="sandbox")
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
        candidate_rules=["favorite_losing", "tie_deficit"],
    )
    session.add(version)
    await session.flush()
    return _World(
        strategy.id,
        version.id,
        world.league,
        world.home,
        world.away,
        world.fixtures,
    )


async def _open_coupon(
    session,
    world: _World,
    *,
    fixture_id: int,
    stake: Decimal,
    odd: Decimal,
) -> None:
    coupon = AdvisorCoupon(
        mode="sandbox",
        strategy_version_id=world.version_id,
        status="open",
        stake=stake,
        combined_odd=odd,
        proposed_combined_odd=odd,
        payout_keep=PAYOUT_KEEP,
        placed_at=NOW,
    )
    session.add(coupon)
    await session.flush()
    session.add(
        AdvisorLeg(
            coupon_id=coupon.id,
            strategy_id=world.strategy_id,
            fixture_id=fixture_id,
            mode="sandbox",
            period="regulation",
            n=1,
            odd=odd,
            proposed_odd=odd,
            goals_home=0,
            goals_away=1,
            status="open",
        )
    )


async def _fixture_ids(session, strategy_id: int) -> list[int]:
    rows = (
        await session.scalars(
            select(AdvisorLeg.fixture_id)
            .where(AdvisorLeg.strategy_id == strategy_id)
            .order_by(AdvisorLeg.fixture_id)
        )
    ).all()
    return list(rows)


async def _wipe(session, worlds: tuple[_World, ...]) -> None:
    leagues: set[int] = set()
    teams: set[int] = set()
    fixtures: set[int] = set()
    for world in worlds:
        if not world.strategy_id:
            continue
        leagues.add(world.league)
        teams.update((world.home, world.away))
        fixtures.update(world.fixtures)
        await session.execute(
            delete(AdvisorLeg).where(AdvisorLeg.strategy_id == world.strategy_id)
        )
        await session.execute(
            delete(AdvisorDecision).where(
                AdvisorDecision.strategy_id == world.strategy_id
            )
        )
        if world.version_id:
            await session.execute(
                delete(AdvisorCoupon).where(
                    AdvisorCoupon.strategy_version_id == world.version_id
                )
            )
            await session.execute(
                delete(AdvisorLangfuseOutbox).where(
                    AdvisorLangfuseOutbox.cycle_id.in_(
                        select(AdvisorCycle.id).where(
                            AdvisorCycle.strategy_version_id == world.version_id
                        )
                    )
                )
            )
            await session.execute(
                delete(AdvisorCycle).where(
                    AdvisorCycle.strategy_version_id == world.version_id
                )
            )
            await session.execute(
                delete(AdvisorStrategyVersion).where(
                    AdvisorStrategyVersion.id == world.version_id
                )
            )
        await session.execute(
            delete(AdvisorStrategy).where(AdvisorStrategy.id == world.strategy_id)
        )
    if fixtures:
        await session.execute(delete(Fixture).where(Fixture.id.in_(fixtures)))
    if teams:
        await session.execute(delete(Team).where(Team.id.in_(teams)))
    if leagues:
        await session.execute(delete(League).where(League.id.in_(leagues)))
