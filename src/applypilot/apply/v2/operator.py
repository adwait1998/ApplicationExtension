"""The Operator interface (spec §9) — the only AI in the apply loop.

JSON-in -> schema-validated JSON-out; enumerated answers by INDEX (free-typed
option values are a type error, invariant 6); one retry; no browser access.
Built over llm.get_client() so provider swap needs zero engine changes
(acceptance #4). Spend is metered because get_client()/MeteredClient wraps it.

Phase 3 implements resolve_fields fully; score/label_controls are declared for
transport-agnosticism and stubbed (see plan Open Decisions)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class FieldSpec:
    field_fp: str
    question_text: str
    widget_kind: str
    options: list[str] | None            # None for free-text; list for enumerated
    char_limit: int | None = None


@dataclass
class FieldResolutionRequest:
    job_context: str
    fields: list[FieldSpec] = field(default_factory=list)


@dataclass
class FieldAnswer:
    field_fp: str
    text: str | None = None
    option_index: int | None = None
    cannot_answer: bool = False


@dataclass
class FieldAnswers:
    by_fp: dict[str, FieldAnswer] = field(default_factory=dict)


class Operator(Protocol):
    def score(self, job, rubric, anchors) -> Any: ...                 # §7.3 (Phase 4)
    def resolve_fields(self, request: FieldResolutionRequest) -> FieldAnswers: ...  # §6.5
    def label_controls(self, snapshot) -> Any: ...                    # §6.8 degraded


_SYSTEM = (
    "You fill job-application fields. Return ONLY JSON: "
    '{"answers":[{"field_fp": str, "text": str} | {"field_fp": str, "option_index": int}]}. '
    "For enumerated fields you MUST return option_index (0-based into the given options); "
    "NEVER free-type an option value. For free-text fields return text. "
    "If you cannot answer a field, return {\"field_fp\": str, \"cannot_answer\": true}."
)


class LLMOperator:
    """Default Operator over the llm.py router."""

    def __init__(self, client=None):
        if client is None:
            from applypilot.llm import get_client
            client = get_client()
        self._client = client

    def resolve_fields(self, request: FieldResolutionRequest) -> FieldAnswers:
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": self._render(request)},
        ]
        raw = self._json_call(messages)
        parsed = self._validate(raw, request)
        if parsed is None:
            raw = self._json_call(messages)          # ONE retry (spec §6.5)
            parsed = self._validate(raw, request)
        if parsed is None:
            # park-don't-guess: every requested field -> cannot_answer
            return FieldAnswers(by_fp={f.field_fp: FieldAnswer(f.field_fp, cannot_answer=True)
                                       for f in request.fields})
        return parsed

    def _json_call(self, messages: list[dict]) -> str:
        try:
            return self._client.chat(messages, temperature=0.0, max_tokens=1024,
                                     response_format={"type": "json_object"})
        except TypeError:
            # Client doesn't accept response_format (older signature) -> retry plain.
            return self._client.chat(messages, temperature=0.0, max_tokens=1024)

    @staticmethod
    def _render(request: FieldResolutionRequest) -> str:
        payload = {"job": request.job_context, "fields": [
            {"field_fp": f.field_fp, "question": f.question_text,
             "widget": f.widget_kind, "char_limit": f.char_limit,
             "options": f.options} for f in request.fields]}
        return json.dumps(payload)

    @staticmethod
    def _validate(raw: str, request: FieldResolutionRequest) -> FieldAnswers | None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("answers"), list):
            return None
        spec_by_fp = {f.field_fp: f for f in request.fields}
        out = FieldAnswers()
        for a in data["answers"]:
            if not isinstance(a, dict):
                return None
            fp = a.get("field_fp")
            spec = spec_by_fp.get(fp)
            if spec is None:
                return None
            if a.get("cannot_answer") is True:
                out.by_fp[fp] = FieldAnswer(fp, cannot_answer=True)
                continue
            if spec.options is not None:                 # enumerated -> index only
                idx = a.get("option_index")
                if not isinstance(idx, int) or not (0 <= idx < len(spec.options)):
                    return None                          # free text / OOR -> invalid
                out.by_fp[fp] = FieldAnswer(fp, option_index=idx)
            else:                                        # free-text -> text only
                txt = a.get("text")
                if not isinstance(txt, str):
                    return None
                if spec.char_limit:
                    txt = txt[:spec.char_limit]
                out.by_fp[fp] = FieldAnswer(fp, text=txt)
        # every requested field must be answered (else re-ask)
        if set(out.by_fp) != set(spec_by_fp):
            return None
        return out

    # Declared for transport-agnosticism; Phase 4 fills these in.
    def score(self, job, rubric, anchors):
        raise NotImplementedError("score is Phase 4 (compile-then-score matching)")

    def label_controls(self, snapshot):
        raise NotImplementedError("label_controls is the Phase-4 degraded tier")


class ClaudeCLIOperator(LLMOperator):
    """Quarantined provider preserving the Claude Code CLI (subscription, no API
    key) path — proves the Operator is transport-agnostic. Wraps ClaudeCodeClient
    (which ignores response_format; _json_call's TypeError fallback handles it)."""

    def __init__(self):
        from applypilot.llm import ClaudeCodeClient
        super().__init__(client=ClaudeCodeClient())
