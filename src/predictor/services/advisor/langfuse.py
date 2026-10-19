"""Export one model call to Langfuse Cloud (EU) over OTLP.

The legacy ingestion API sunsets on 2026-11-16. This posts OTLP/JSON to
``/api/public/otel/v1/traces`` with ``x-langfuse-ingestion-version: 4``.
A failed export is stored and retried. It never raises to the caller, and
it never updates the cycle row.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from predictor.models.advisor import AdvisorLangfuseOutbox
from predictor.schemas.advisor import AdvisorSettings

logger = logging.getLogger(__name__)

MAX_EXPORT_ATTEMPTS = 5
EU_HOST = "https://cloud.langfuse.com"
_REDACTED = "[redacted]"
_PATTERNS = (
    re.compile(
        r"\b(?:postgres(?:ql)?(?:\+[a-z0-9]+)?|mongodb(?:\+srv)?|redis|mysql)://\S+",
        re.I,
    ),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"']+:[^\s\"']+@[^\s\"']+", re.I),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    re.compile(r"\b(?:sk|pk)-lf-[A-Za-z0-9\-]+\b"),
)


class TraceExportError(Exception):
    """The export failed. ``code`` is safe to store: no URL and no secret."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TraceSink(Protocol):
    async def send(self, body: dict[str, Any]) -> None:
        """Post one OTLP body. Raise TraceExportError on failure."""


@dataclass(frozen=True, slots=True)
class TraceDraft:
    prompt_version: str
    prompt: str
    model_input: dict[str, Any]
    output: str | None
    provider: str
    model: str
    model_version: str
    mode: str
    status: str
    input_tokens: int
    output_tokens: int
    thoughts_tokens: int
    cost_pln: Decimal
    latency_ms: int | None
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class TraceExport:
    trace_id: str
    body: dict[str, Any]


def redact(value: Any, secrets: Sequence[str]) -> Any:
    """Copy ``value`` with secrets, DSNs, and bearer tokens removed."""
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [redact(item, secrets) for item in value]
    return value


def build_trace(draft: TraceDraft, secrets: Sequence[str]) -> TraceExport:
    """One generation span for a single model call."""
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    start = _aware(draft.started_at)
    end = _aware(draft.finished_at)
    if end < start:
        end = start
    output = "" if draft.output is None else draft.output
    attributes = [
        _string("langfuse.trace.name", "advisor.cycle"),
        _string("langfuse.observation.type", "generation"),
        _string("langfuse.version", draft.prompt_version),
        _string("gen_ai.system", draft.provider),
        _string("gen_ai.request.model", draft.model),
        _string("langfuse.observation.input", _redact_text(draft.prompt, secrets)),
        _string("langfuse.observation.output", _redact_text(output, secrets)),
        _string(
            "langfuse.observation.metadata.input",
            json.dumps(redact(draft.model_input, secrets), default=str),
        ),
        _string("langfuse.observation.metadata.prompt_version", draft.prompt_version),
        _string("langfuse.observation.metadata.model_version", draft.model_version),
        _string("langfuse.observation.metadata.mode", draft.mode),
        _string("langfuse.observation.metadata.validation_status", draft.status),
        _string("langfuse.observation.metadata.cost_pln", str(draft.cost_pln)),
        _int("gen_ai.usage.input_tokens", draft.input_tokens),
        _int("gen_ai.usage.output_tokens", draft.output_tokens),
        _int("langfuse.observation.metadata.thoughts_tokens", draft.thoughts_tokens),
    ]
    if draft.latency_ms is not None:
        attributes.append(
            _int("langfuse.observation.metadata.latency_ms", draft.latency_ms)
        )
    body: dict[str, Any] = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [_string("service.name", "predictor-advisor")]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "predictor.advisor"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": "advisor.cycle",
                                "kind": 3,
                                "startTimeUnixNano": _nano(start),
                                "endTimeUnixNano": _nano(end),
                                "attributes": attributes,
                                "status": (
                                    {"code": 2, "message": draft.status}
                                    if draft.status == "provider_error"
                                    else {"code": 1}
                                ),
                            }
                        ],
                    }
                ],
            }
        ]
    }
    return TraceExport(trace_id, body)


def sink_from_settings(settings: AdvisorSettings) -> LangfuseSink | None:
    """None when either key is empty, so a decision still runs."""
    public = settings.langfuse_public_key.get_secret_value().strip()
    secret = settings.langfuse_secret_key.get_secret_value().strip()
    if not public or not secret:
        return None
    return LangfuseSink(
        public_key=public,
        secret_key=secret,
        host=settings.langfuse_host,
        timeout=settings.langfuse_timeout_seconds,
    )


class LangfuseSink:
    """POST one OTLP body to Langfuse Cloud. The default host is the EU region."""

    def __init__(
        self,
        *,
        public_key: str,
        secret_key: str,
        host: str = EU_HOST,
        timeout: float = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._public = public_key
        self._secret = secret_key
        self._url = f"{host.rstrip('/')}/api/public/otel/v1/traces"
        self._timeout = timeout
        self._transport = transport

    async def send(self, body: dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    self._url,
                    json=body,
                    auth=(self._public, self._secret),
                    headers={"x-langfuse-ingestion-version": "4"},
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise TraceExportError("timeout") from exc
        except httpx.HTTPStatusError as exc:
            raise TraceExportError(f"http_{exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise TraceExportError("network") from exc


async def deliver_trace(session: AsyncSession, sink: TraceSink, cycle_id: int) -> None:
    """Send the pending row for one cycle. Swallow every failure."""
    try:
        await _deliver(session, sink, cycle_id)
    except Exception as exc:
        logger.warning("langfuse export failed: %s", type(exc).__name__)


async def retry_pending(
    session: AsyncSession, sink: TraceSink, *, limit: int = 8
) -> int:
    """Try pending rows again. Returns how many were queued for this pass."""
    if session.in_transaction():
        return 0
    async with session.begin():
        cycle_ids = list(
            await session.scalars(
                select(AdvisorLangfuseOutbox.cycle_id)
                .where(
                    AdvisorLangfuseOutbox.status == "pending",
                    AdvisorLangfuseOutbox.attempts < MAX_EXPORT_ATTEMPTS,
                )
                .order_by(AdvisorLangfuseOutbox.id)
                .limit(limit)
            )
        )
    for cycle_id in cycle_ids:
        await deliver_trace(session, sink, cycle_id)
    return len(cycle_ids)


def _redact_text(text: str, secrets: Sequence[str]) -> str:
    cleaned = text
    needles = sorted(
        {item for item in secrets if len(item) >= 8}, key=len, reverse=True
    )
    for needle in needles:
        cleaned = cleaned.replace(needle, _REDACTED)
    for pattern in _PATTERNS:
        cleaned = pattern.sub(_REDACTED, cleaned)
    return cleaned


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def _nano(moment: datetime) -> str:
    return str(int(moment.timestamp() * 1_000_000_000))


def _string(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def _int(key: str, value: int) -> dict[str, Any]:
    return {"key": key, "value": {"intValue": str(value)}}


async def _deliver(session: AsyncSession, sink: TraceSink, cycle_id: int) -> None:
    if session.in_transaction():
        return
    async with session.begin():
        claimed = await session.execute(
            update(AdvisorLangfuseOutbox)
            .where(
                AdvisorLangfuseOutbox.cycle_id == cycle_id,
                AdvisorLangfuseOutbox.status == "pending",
                AdvisorLangfuseOutbox.attempts < MAX_EXPORT_ATTEMPTS,
            )
            .values(
                status="sending",
                attempts=AdvisorLangfuseOutbox.attempts + 1,
            )
            .returning(
                AdvisorLangfuseOutbox.payload,
                AdvisorLangfuseOutbox.attempts,
            )
        )
        row = claimed.first()
        if row is None:
            return
        payload = json.loads(json.dumps(row.payload))
        attempts = row.attempts
    code = await _send(sink, payload)
    status = "sent" if code is None else "pending"
    if code is not None and attempts >= MAX_EXPORT_ATTEMPTS:
        status = "abandoned"
    async with session.begin():
        await session.execute(
            update(AdvisorLangfuseOutbox)
            .where(
                AdvisorLangfuseOutbox.cycle_id == cycle_id,
                AdvisorLangfuseOutbox.status == "sending",
            )
            .values(
                status=status,
                last_error=None if code is None else code[:128],
            )
        )


async def _send(sink: TraceSink, payload: dict[str, Any]) -> str | None:
    try:
        await sink.send(payload)
    except TraceExportError as exc:
        return exc.code[:128]
    except Exception:
        return "export_failed"
    return None
