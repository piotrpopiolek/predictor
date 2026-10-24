"""One line under a match: missing LLM gates, or the model's decision."""

from __future__ import annotations

from html import escape

from predictor.services.advisor.llm_note import LlmGap, LlmNote

_GAPS = {
    LlmGap.NOT_CANDIDATE: "faworyt nie przegrywa i nie odrabia dwumeczu",
    LlmGap.NOT_SECOND_HALF: "status inny niż 2H",
    LlmGap.MINUTE: "minuta powyżej limitu",
    LlmGap.NO_LINE: "brak Over N+0.5",
    LlmGap.SUSPENDED: "Over N+0.5 zawieszony",
    LlmGap.STALE: "Over N+0.5 starszy niż 5 min",
    LlmGap.ODD_LOW: "Over N+0.5 poniżej 1,30",
    LlmGap.OPEN_COUPON: "mecz już na otwartym kuponie",
    LlmGap.REJECTED: "ten wynik został odrzucony",
    LlmGap.PENALTIES: "rzuty karne",
    LlmGap.NOT_IN_PLAY: "mecz nie jest w grze",
}
_DECISIONS = {
    "bet": "graj",
    "reject": "odrzuć",
    "wait": "czekaj",
    "wait_timeout": "koniec czekania",
}


def render_llm_note(note: LlmNote) -> str:
    parts: list[str] = []
    if note.gaps:
        missing = ", ".join(_gap_text(note, gap) for gap in note.gaps)
        parts.append(f"Do LLM brakuje: {missing}")
    decision = _decision_text(note)
    if decision:
        parts.append(decision)
    elif note.strategy_paused:
        parts.append("Strategia LLM jest wstrzymana — model nie jest wołany")
    else:
        parts.append("W puli LLM — brak decyzji dla tego wyniku")
    text = " ".join(parts)
    kind = "ready" if not note.gaps and not note.strategy_paused else "blocked"
    return f'<p class="llm-note {kind}">{escape(text)}</p>'


def _gap_text(note: LlmNote, gap: LlmGap) -> str:
    label = _GAPS[gap]
    if gap is LlmGap.MINUTE:
        return f"{label} {note.minute_limit}"
    priced = {LlmGap.NO_LINE, LlmGap.SUSPENDED, LlmGap.STALE, LlmGap.ODD_LOW}
    if note.line and gap in priced:
        return label.replace("N+0.5", note.line)
    return label


def _decision_text(note: LlmNote) -> str:
    item = note.decision
    if item is None:
        return ""
    name = _DECISIONS.get(item.decision, item.decision)
    score = f"{item.goals_home}:{item.goals_away}"
    prefix = "LLM" if item.same_state and not note.gaps else "Ostatnia decyzja LLM"
    reason = " ".join(item.rationale.split())
    if len(reason) > 140:
        reason = reason[:137].rstrip() + "…"
    body = f"{prefix}: {name} przy {score}"
    if reason:
        body = f"{body} — {reason}"
    return body
