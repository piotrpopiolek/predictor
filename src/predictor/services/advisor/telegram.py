"""Telegram notices. The message never carries a token or a button."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.models.advisor import AdvisorTelegramOutbox

_MAX = 5
KIND_RECOMMENDATION = "recommendation"
KIND_WITHDRAW = "withdraw"
KIND_SUMMARY = "summary"
KIND_SANDBOX_LLM = "sandbox_llm"
KIND_DECISION_WAIT = "decision_wait"
OPERATOR_KINDS = (
    KIND_RECOMMENDATION,
    KIND_WITHDRAW,
    KIND_SUMMARY,
    KIND_DECISION_WAIT,
)


class TelegramError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def recommendation_text(
    *,
    coupon_id: int,
    lines: tuple[str, ...],
    stake: str,
    combined: str,
    expires: str,
    link: str,
) -> str:
    body = "\n".join(lines)
    return (
        "Rekomendacja kuponu\n"
        f"{body}\n"
        f"Kurs łączny {combined}\n"
        f"Sugerowana stawka {stake}\n"
        f"Wygasa {expires}\n"
        f"{link}"
    )


def sandbox_llm_text(
    *,
    coupon_id: int,
    lines: Sequence[str],
    stake: str,
    combined: str,
) -> str:
    body = "\n".join(lines)
    return (
        f"Sandbox-llm gra kupon {coupon_id}\n"
        f"{body}\n"
        f"Kurs łączny {combined}\n"
        f"Stawka {stake}"
    )


def waiting_decision_text(cycle_id: int) -> str:
    """Operator chat only. No coupon, no token, no button."""
    return f"Czekam na decyzję.\n/advisor/recommendations?cycle={cycle_id}"


def enqueue(session: AsyncSession, kind: str, text: str) -> None:
    session.add(
        AdvisorTelegramOutbox(
            kind=kind,
            payload={"text": text},
            attempts=0,
            status="pending",
        )
    )


async def deliver_pending(
    session: AsyncSession,
    *,
    token: str,
    chat_id: str,
    post: Callable[[str, str, str], Awaitable[None]],
    kinds: Collection[str] = OPERATOR_KINDS,
) -> int:
    """Send pending notices for the given kinds. Empty credentials skip the send."""
    if not token or not chat_id:
        return 0
    rows = list(
        (
            await session.scalars(
                select(AdvisorTelegramOutbox)
                .where(AdvisorTelegramOutbox.status == "pending")
                .where(AdvisorTelegramOutbox.attempts < _MAX)
                .where(AdvisorTelegramOutbox.kind.in_(tuple(kinds)))
                .order_by(AdvisorTelegramOutbox.id)
                .limit(5)
            )
        ).all()
    )
    sent = 0
    for row in rows:
        row.status = "sending"
        row.attempts += 1
    await session.flush()
    for row in rows:
        text = str(row.payload.get("text", ""))
        try:
            await post(token, chat_id, text)
        except TelegramError as exc:
            row.last_error = exc.code[:32]
            row.status = "abandoned" if row.attempts >= _MAX else "pending"
            continue
        row.status = "sent"
        row.last_error = None
        sent += 1
    return sent


async def post_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                url,
                json={"chat_id": chat_id, "text": text},
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise TelegramError("send_failed") from exc
