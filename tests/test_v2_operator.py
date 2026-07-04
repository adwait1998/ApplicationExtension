import json

import pytest

from applypilot.apply.v2 import ir
from applypilot.apply.v2 import operator as op


class _FakeClient:
    """Call-site-injected fake Operator LLM (mirrors test_answer_cache._LLM):
    captures messages, returns a scripted JSON string per call."""
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def chat(self, messages, temperature=0.0, max_tokens=4096, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self._replies.pop(0)


def _req():
    return op.FieldResolutionRequest(
        job_context="Senior Product Designer at Acme",
        fields=[
            op.FieldSpec(field_fp="fp1", question_text="Why do you want to work here?",
                         widget_kind="textarea", options=None, char_limit=500),
            op.FieldSpec(field_fp="fp2", question_text="Preferred work style?",
                         widget_kind="react_select",
                         options=["Remote", "Hybrid", "Onsite"], char_limit=None),
        ],
    )


def test_resolve_fields_text_and_index_answer():
    reply = json.dumps({"answers": [
        {"field_fp": "fp1", "text": "Your design culture resonates with me."},
        {"field_fp": "fp2", "option_index": 0},
    ]})
    client = _FakeClient([reply])
    operator = op.LLMOperator(client=client)
    ans = operator.resolve_fields(_req())
    assert ans.by_fp["fp1"].text == "Your design culture resonates with me."
    assert ans.by_fp["fp2"].option_index == 0        # index, not free text
    assert ans.by_fp["fp2"].text is None


def test_free_typed_option_value_is_rejected_at_schema_layer():
    # A free-typed option value for an enumerated field must be a type error,
    # not silently accepted (invariant 6 — kills the react-select-desync class).
    bad = json.dumps({"answers": [
        {"field_fp": "fp1", "text": "ok"},
        {"field_fp": "fp2", "text": "Fully Remote Forever"},   # not an index!
    ]})
    good = json.dumps({"answers": [
        {"field_fp": "fp1", "text": "ok"},
        {"field_fp": "fp2", "option_index": 1},
    ]})
    client = _FakeClient([bad, good])     # first invalid -> ONE retry -> valid
    operator = op.LLMOperator(client=client)
    ans = operator.resolve_fields(_req())
    assert len(client.calls) == 2                     # exactly one retry
    assert ans.by_fp["fp2"].option_index == 1


def test_out_of_range_index_rejected_then_retry():
    bad = json.dumps({"answers": [{"field_fp": "fp1", "text": "ok"},
                                  {"field_fp": "fp2", "option_index": 9}]})  # OOR
    good = json.dumps({"answers": [{"field_fp": "fp1", "text": "ok"},
                                   {"field_fp": "fp2", "option_index": 2}]})
    operator = op.LLMOperator(client=_FakeClient([bad, good]))
    ans = operator.resolve_fields(_req())
    assert ans.by_fp["fp2"].option_index == 2


def test_two_invalid_replies_parks_field_not_crash():
    junk = "not json at all"
    operator = op.LLMOperator(client=_FakeClient([junk, junk]))
    ans = operator.resolve_fields(_req())
    # After ONE retry still invalid -> cannot_answer, never an exception.
    assert ans.by_fp["fp1"].cannot_answer is True
    assert ans.by_fp["fp2"].cannot_answer is True


def test_char_limit_clamped():
    long = "x" * 999
    reply = json.dumps({"answers": [{"field_fp": "fp1", "text": long},
                                    {"field_fp": "fp2", "option_index": 0}]})
    operator = op.LLMOperator(client=_FakeClient([reply]))
    ans = operator.resolve_fields(_req())
    assert len(ans.by_fp["fp1"].text) <= 500          # clamped to char_limit


def test_response_format_requested_when_supported(monkeypatch):
    # The operator asks the client for JSON object mode when the client accepts it.
    client = _FakeClient([json.dumps({"answers": [
        {"field_fp": "fp1", "text": "ok"}, {"field_fp": "fp2", "option_index": 0}]})])
    op.LLMOperator(client=client).resolve_fields(_req())
    assert client.calls[0]["kwargs"].get("response_format") == {"type": "json_object"}


def test_llm_chat_compat_accepts_response_format(monkeypatch):
    from applypilot import llm
    captured = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "{}"}}]}

    c = llm.LLMClient.__new__(llm.LLMClient)   # bypass __init__ network
    c.model = "gpt-x"; c.api_key = "k"; c.base_url = "http://x"; c._is_gemini = False
    c._client = type("C", (), {"post": lambda self, url, json, headers: (captured.update(payload=json) or _Resp())})()
    out = c._chat_compat([{"role": "user", "content": "hi"}], 0.0, 100,
                         response_format={"type": "json_object"})
    assert out == "{}"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    # backward compat: omitting it must NOT add the key
    c._chat_compat([{"role": "user", "content": "hi"}], 0.0, 100)
    assert "response_format" not in captured["payload"]


def test_metered_client_forwards_response_format_to_inner():
    # The REAL path is get_client() -> MeteredClient(inner). Assert MeteredClient
    # does NOT drop response_format (regression guard: its chat() had no **kwargs).
    from applypilot.spend_ledger import MeteredClient, SpendLedger

    captured = {}

    class _Inner:
        def chat(self, messages, temperature=0.0, max_tokens=4096, response_format=None):
            captured["response_format"] = response_format
            return "{}"

    mc_client = MeteredClient(_Inner(), SpendLedger.__new__(SpendLedger), model="m")
    # metering may need a no-op record; if SpendLedger needs init, build it via the
    # normal ctor + a temp path as other spend_ledger tests do.
    op.LLMOperator(client=mc_client).resolve_fields(_req())
    assert captured["response_format"] == {"type": "json_object"}   # reached the inner client
