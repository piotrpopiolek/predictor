"""Pure cases for this layer. They do not open Postgres."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from predictor.constants import SANDBOX_BANKROLL
from predictor.models.advisor import (
    AdvisorStrategyVersion,
)
from predictor.services.advisor.recommend import (
    RecommendationSetup,
    hour_allowed,
    inactive_reason,
    league_allowed,
    max_drawdown,
    promotion_block,
    withdraw_due,
)
from predictor.services.advisor.summary import (
    parse_proposal,
    render_summary_prompt,
)
from predictor.services.advisor.telegram import recommendation_text
from predictor.ui.advisor import render_recommendation_cards

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)

_PROPOSAL = (
    '{"min_combined_odd":"1.700","max_legs":2,'
    '"max_minute_regulation":80,"max_minute_extra":110,'
    '"wait_minutes":10,"notes":"węższy próg"}'
)


def _setup(**overrides: object) -> RecommendationSetup:
    base = dict(
        stake=Decimal("20"),
        loss_limit=Decimal("100"),
        hours=(8, 22),
        league_ids=frozenset({39}),
        coupon_limit=10,
        ttl_seconds=180,
        withdraw_yield=Decimal("0"),
    )
    base.update(overrides)
    return RecommendationSetup(**base)


def test_recommendation_stays_inactive_without_operator_settings() -> None:
    bare = _setup(stake=None, hours=None, league_ids=frozenset())
    assert (
        inactive_reason(bare, NOW, coupons_today=0, loss_today=Decimal("0")) == "stake"
    )
    ready = _setup()
    assert inactive_reason(ready, NOW, coupons_today=0, loss_today=Decimal("0")) is None
    open_leagues = _setup(league_ids=frozenset())
    assert (
        inactive_reason(open_leagues, NOW, coupons_today=0, loss_today=Decimal("0"))
        is None
    )
    assert league_allowed(39, open_leagues, untracked=frozenset({874})) is True
    assert league_allowed(874, open_leagues, untracked=frozenset({874})) is False
    assert league_allowed(39, ready, untracked=frozenset()) is True
    assert league_allowed(140, ready, untracked=frozenset()) is False
    assert inactive_reason(ready, NOW, coupons_today=10, loss_today=Decimal("0")) == (
        "coupon_limit"
    )
    assert inactive_reason(ready, NOW, coupons_today=0, loss_today=Decimal("100")) == (
        "loss_limit_hit"
    )
    assert hour_allowed(23, (22, 6)) is True
    assert hour_allowed(12, (22, 6)) is False


def test_promotion_and_withdraw_use_the_sandbox_book() -> None:
    assert (
        promotion_block(
            settled=199,
            yield_net=Decimal("0.1"),
            baseline_yield=Decimal("0"),
            drawdown=Decimal("0.1"),
        )
        == "sample"
    )
    assert (
        promotion_block(
            settled=200,
            yield_net=Decimal("0.2"),
            baseline_yield=Decimal("0.1"),
            drawdown=Decimal("0.2"),
        )
        is None
    )
    assert withdraw_due(Decimal("-0.01"), Decimal("0")) is True
    assert withdraw_due(None, Decimal("0")) is False
    assert max_drawdown([-(SANDBOX_BANKROLL / 4)]) == Decimal("0.2500")


def test_telegram_text_has_no_token() -> None:
    text = recommendation_text(
        coupon_id=4,
        lines=("A – B, 70', 0:1, Over 1.5 @ 1.70",),
        stake="20",
        combined="1.70",
        expires="2026-10-19T12:03:00+00:00",
        link="http://127.0.0.1:8000/advisor/recommendations/4",
    )
    assert "bot" not in text.casefold()
    assert "1.70" in text
    card = render_recommendation_cards(
        [
            {
                "id": 1,
                "status": "proposed",
                "stake": "20.00",
                "combined": "1.700",
                "remaining": 90,
                "below": True,
                "legs": [
                    {
                        "fixture_id": 9,
                        "score": "0:1",
                        "line": "1.5",
                        "odd": "1.700",
                        "proposed": "1.700",
                    }
                ],
            }
        ]
    )
    assert "poniżej progu" in card
    assert 'value="secret"' not in card


def test_summary_proposal_rejects_extra_fields() -> None:
    proposal = parse_proposal(_PROPOSAL)
    assert proposal.max_legs == 2


def test_summary_prompt_points_at_the_schema() -> None:
    version = AdvisorStrategyVersion(
        strategy_id=1,
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
    prompt = render_summary_prompt("sandbox-llm", version)
    assert "schematem odpowiedzi" in prompt
    assert "Nie dodawaj pól" in prompt
    assert "Pola:" not in prompt
    assert "1.600" in prompt
