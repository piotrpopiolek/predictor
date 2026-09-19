"""Unit tests for operator next-goal bet ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from predictor.services.live_board import LiveMatch, render_bets_html, render_live_html
from predictor.services.operator_bets import (
    STAKE_STEP,
    START_STAKE,
    BetError,
    build_history,
    current_stake,
    goal_counts_for_next_goal,
    has_counting_goal_since_snapshot,
    money,
    parse_odd,
    place_bet,
    resolve_auto_outcome,
    row_pnl,
    settle_bet_manual,
    settle_open_tickets,
)


def _bet(
    *,
    status: str = "open",
    odd: str = "1.700",
    stake: str = "21.00",
    bet_id: int = 1,
    payout_keep: str = "0.880",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=bet_id,
        fixture_id=100 + bet_id,
        odd=Decimal(odd),
        stake=Decimal(stake),
        status=status,
        placed_at=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
        settled_at=None,
        league="Test · League",
        home_name="Home FC",
        away_name="Away FC",
        goals_home=0,
        goals_away=0,
        payout_keep=Decimal(payout_keep),
    )


def test_stake_starts_at_21_and_grows_only_on_wins() -> None:
    assert START_STAKE == Decimal("21.00")
    assert STAKE_STEP == Decimal("0.10")
    assert money(START_STAKE + STAKE_STEP * 0) == Decimal("21.00")
    assert money(START_STAKE + STAKE_STEP * 1) == Decimal("21.10")
    assert money(START_STAKE + STAKE_STEP * 3) == Decimal("21.30")


def test_pnl_uses_088_payout_keep() -> None:
    won = _bet(status="won", odd="1.700", stake="21.00")
    assert row_pnl(won) == Decimal("10.42")
    lost = _bet(status="lost", stake="21.00")
    assert row_pnl(lost) == Decimal("-21.00")
    void = _bet(status="void", stake="21.00")
    assert row_pnl(void) == Decimal("0.00")
    open_bet = _bet(status="open", stake="21.00")
    assert row_pnl(open_bet) == Decimal("0.00")


def test_parse_odd_range_and_comma() -> None:
    assert parse_odd("1,65") == Decimal("1.650")
    with pytest.raises(BetError) as low:
        parse_odd("1.19")
    assert low.value.code == 400
    with pytest.raises(BetError) as high:
        parse_odd("6.01")
    assert high.value.code == 400


def test_resolve_auto_goal_will_happen() -> None:
    assert (
        resolve_auto_outcome(
            snap_home=1,
            snap_away=0,
            now_home=2,
            now_away=0,
            status_short="2H",
            elapsed=70,
        )
        == "won"
    )
    assert (
        resolve_auto_outcome(
            snap_home=1,
            snap_away=1,
            now_home=1,
            now_away=1,
            status_short="FT",
        )
        == "lost"
    )
    assert (
        resolve_auto_outcome(
            snap_home=0,
            snap_away=0,
            now_home=1,
            now_away=1,
            status_short="2H",
            elapsed=60,
        )
        == "won"
    )
    assert (
        resolve_auto_outcome(
            snap_home=0,
            snap_away=0,
            now_home=0,
            now_away=0,
            status_short="CANC",
        )
        is None
    )


def test_stoppage_goal_wins_next_goal_bet() -> None:
    assert goal_counts_for_next_goal(88, None) is True
    assert goal_counts_for_next_goal(90, None) is True
    assert goal_counts_for_next_goal(90, 4) is True
    assert goal_counts_for_next_goal(91, None) is True
    assert goal_counts_for_next_goal(94, None) is True
    assert goal_counts_for_next_goal(None, None) is False

    events = [
        SimpleNamespace(
            event_type="Goal",
            team_id=4317,
            detail="Normal Goal",
            minute=45,
            minute_extra=None,
        ),
        SimpleNamespace(
            event_type="Goal",
            team_id=313,
            detail="Normal Goal",
            minute=90,
            minute_extra=4,
        ),
    ]
    assert (
        has_counting_goal_since_snapshot(
            events, home_id=4317, away_id=313, snap_home=1, snap_away=0
        )
        is True
    )
    assert (
        resolve_auto_outcome(
            snap_home=1,
            snap_away=0,
            now_home=1,
            now_away=1,
            status_short="FT",
            counting_goal=True,
            has_goal_events=True,
        )
        == "won"
    )
    assert (
        resolve_auto_outcome(
            snap_home=1,
            snap_away=0,
            now_home=1,
            now_away=1,
            status_short="2H",
            elapsed=94,
            counting_goal=True,
            has_goal_events=True,
        )
        == "won"
    )


def test_build_history_saldo_and_hit_rates() -> None:
    bets = [
        _bet(bet_id=1, status="won", odd="1.700", stake="21.00"),
        _bet(bet_id=2, status="lost", stake="21.10"),
        _bet(bet_id=3, status="void", stake="21.10"),
        _bet(bet_id=4, status="open", stake="21.10"),
    ]
    rows, metrics = build_history(bets)
    assert metrics.wins == 1
    assert metrics.losses == 1
    assert metrics.voids == 1
    assert metrics.open_count == 1
    assert metrics.hit_rate == 50.0
    assert metrics.current_stake == Decimal("21.10")
    assert rows[1].saldo == Decimal("-10.68")
    assert rows[1].hit_overall == 50.0
    assert rows[1].hit_last10 == 50.0


def test_render_live_pauses_refresh_and_shows_bet_form() -> None:
    html = render_live_html(
        [],
        generated_at=datetime(2026, 9, 13, 20, 0, tzinfo=UTC),
        title="Następny gol",
        empty="Brak.",
        active_nav="next_goal",
        current_stake="21.00",
    )
    assert "http-equiv" not in html
    assert "formOpen" in html
    assert 'href="/live/bets"' in html
    assert "Zakłady" in html
    assert "Zagraj Brak" not in html
    assert "Zagraj następny gol" not in html


def test_render_live_card_bet_controls() -> None:
    match = LiveMatch(
        fixture_id=42,
        country="Japan",
        league="J2 League",
        round=None,
        home="Fujieda MYFC",
        away="Omiya Ardija",
        home_logo=None,
        away_logo=None,
        goals_home=1,
        goals_away=0,
        status_short="2H",
        status_long="Second Half",
        elapsed=48,
        extra=None,
    )
    html = render_live_html(
        [match],
        generated_at=datetime(2026, 9, 13, 20, 0, tzinfo=UTC),
        active_nav="next_goal",
        current_stake="21.00",
    )
    assert "Zagraj następny gol" in html
    assert "Zagraj Brak" not in html
    assert 'name="selection"' not in html
    assert "Gospodarze" not in html
    assert "padnie" in html
    assert "bez względu kto strzeli" in html
    assert 'name="odd"' in html
    assert "Stawka 21.00" in html
    assert 'action="/live/bets"' in html

    open_html = render_live_html(
        [match],
        generated_at=datetime(2026, 9, 13, 20, 0, tzinfo=UTC),
        active_nav="next_goal",
        open_bets={
            42: {
                "id": 7,
                "odd": "1.600",
                "stake": "21.00",
            }
        },
    )
    assert "Otwarty" in open_html
    assert "Gol @ 1.600" in open_html
    assert "Brak @" not in open_html
    assert 'action="/live/bets/7/settle"' in open_html
    assert "Zagraj następny gol" not in open_html
    assert "Zagraj Brak" not in open_html


def test_render_bets_history_metrics() -> None:
    payload = {
        "metrics": {
            "n": 2,
            "wins": 1,
            "losses": 1,
            "voids": 0,
            "open_count": 0,
            "hit_rate": 50.0,
            "saldo": "-10.68",
            "current_stake": "21.10",
            "avg_odd": "1.650",
            "staked": "42.10",
        },
        "bets": [
            {
                "id": 1,
                "fixture_id": 10,
                "placed_at": "2026-03-01T12:00:00+00:00",
                "league": "Test",
                "home": "A",
                "away": "B",
                "odd": "1.700",
                "stake": "21.00",
                "status": "won",
                "pnl": "10.42",
                "saldo": "10.42",
                "hit_overall": 100.0,
                "hit_last10": 100.0,
            }
        ],
    }
    html = render_bets_html(
        payload, generated_at=datetime(2026, 9, 13, 20, 0, tzinfo=UTC)
    )
    assert "Zakłady next goal" in html
    assert "Saldo" in html
    assert "Strona" not in html
    assert 'href="/live/bets" class="active"' in html
    assert "10,42" in html or "10.42" in html


def _match(fixture_id: int = 9001) -> LiveMatch:
    return LiveMatch(
        fixture_id=fixture_id,
        country="Test",
        league="League",
        round=None,
        home="Home FC",
        away="Away FC",
        home_logo=None,
        away_logo=None,
        goals_home=0,
        goals_away=0,
        status_short="1H",
        status_long="First Half",
        elapsed=20,
        extra=None,
    )


@pytest.mark.asyncio
async def test_current_stake_grows_only_on_wins() -> None:
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[0, 2, 2])
    assert await current_stake(session) == Decimal("21.00")
    assert await current_stake(session) == Decimal("21.20")
    assert await current_stake(session) == Decimal("21.20")


@pytest.mark.asyncio
async def test_place_rejects_second_open_on_same_fixture() -> None:
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=99)
    with pytest.raises(BetError) as exc:
        await place_bet(session, match=_match(42), odd=Decimal("1.700"))
    assert exc.value.code == 409
    assert exc.value.detail == "open_bet_exists"


@pytest.mark.asyncio
async def test_place_bet_uses_current_stake() -> None:
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[None, 1])
    session.add = MagicMock()
    session.flush = AsyncMock()

    bet = await place_bet(session, match=_match(55), odd=Decimal("2.100"))
    assert bet.fixture_id == 55
    assert bet.stake == Decimal("21.10")
    assert bet.status == "open"
    session.add.assert_called_once()
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_settle_manual_409_and_void() -> None:
    session = AsyncMock()
    settled = _bet(status="won", bet_id=3)
    session.get = AsyncMock(return_value=settled)
    with pytest.raises(BetError) as exc:
        await settle_bet_manual(session, 3, "lost")
    assert exc.value.code == 409

    open_bet = _bet(status="open", bet_id=4)
    session.get = AsyncMock(return_value=open_bet)
    session.flush = AsyncMock()
    out = await settle_bet_manual(session, 4, "void")
    assert out.status == "void"
    assert out.settled_at is not None

    session.get = AsyncMock(return_value=None)
    with pytest.raises(BetError) as missing:
        await settle_bet_manual(session, 999, "won")
    assert missing.value.code == 404


@pytest.mark.asyncio
async def test_settle_open_tickets_from_fixture_score() -> None:
    bet = _bet(status="open", bet_id=10)
    bet.fixture_id = 100
    bet.goals_home = 0
    bet.goals_away = 0

    open_result = MagicMock()
    open_result.all.return_value = [bet]
    fixture = SimpleNamespace(
        id=100,
        goals_home=1,
        goals_away=0,
        status_short="2H",
        home_team_id=1,
        away_team_id=2,
        elapsed_minutes=50,
    )
    fixture_result = MagicMock()
    fixture_result.all.return_value = [fixture]
    events_result = MagicMock()
    events_result.all.return_value = []

    session = AsyncMock()
    session.scalars = AsyncMock(
        side_effect=[open_result, fixture_result, events_result]
    )

    settled = await settle_open_tickets(session)
    assert settled == 1
    assert bet.status == "won"
    assert bet.settled_at is not None


@pytest.mark.asyncio
async def test_settle_open_loses_on_ft_without_goal() -> None:
    bet = _bet(status="open", bet_id=11)
    bet.fixture_id = 101
    bet.goals_home = 1
    bet.goals_away = 1

    open_result = MagicMock()
    open_result.all.return_value = [bet]
    fixture = SimpleNamespace(
        id=101,
        goals_home=1,
        goals_away=1,
        status_short="FT",
        home_team_id=1,
        away_team_id=2,
        elapsed_minutes=90,
    )
    fixture_result = MagicMock()
    fixture_result.all.return_value = [fixture]
    events_result = MagicMock()
    events_result.all.return_value = []

    session = AsyncMock()
    session.scalars = AsyncMock(
        side_effect=[open_result, fixture_result, events_result]
    )

    assert await settle_open_tickets(session) == 1
    assert bet.status == "lost"


def test_parse_odd_invalid() -> None:
    with pytest.raises(BetError) as exc:
        parse_odd("not-a-number")
    assert exc.value.code == 400
