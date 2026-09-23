"""Tests for the optional Laya backend (applypilot.extension.laya_backend).

This suite runs on a machine WITHOUT laya/torch installed -- the main test
interpreter genuinely lacks them, so every test either exercises the real
"package not installed" path directly (no mocking needed, it is simply
true on this machine), or injects a fake router object exposing the same
``load()``/``predict()`` surface Laya's real ``Router`` does, so
classify()/warmup() logic is exercised at $0 without ever importing torch.

One additional integration test is skipped unless laya is genuinely
importable AND APPLYPILOT_LAYA=1 is set -- it must never fail the suite on
a machine without Laya (see test_real_laya_classifies_realistic_fields).
"""
from __future__ import annotations

import os
import sys
import types

import pytest

from applypilot.extension import laya_backend as lb
from applypilot.extension import matcher
from applypilot.extension.schema import FieldDescriptor


def _field(**kwargs) -> FieldDescriptor:
    base = dict(
        id="f0", selector="#x", tag="input", type="text", name="",
        autocomplete="", label="", placeholder="", required=False, options=[],
    )
    base.update(kwargs)
    return FieldDescriptor(**base)


@pytest.fixture(autouse=True)
def _reset_module_singleton(monkeypatch):
    # get_backend() caches its result at module scope so repeated /health
    # polls don't re-import anything -- every test must start clean
    # regardless of run order.
    monkeypatch.setattr(lb, "_backend_singleton", None)
    monkeypatch.setattr(lb, "_backend_checked", False)
    monkeypatch.delenv(lb._ENV_VAR, raising=False)
    yield


class FakeRouter:
    """Stands in for laya.Router: same load()/predict() surface, no torch."""

    def __init__(self, answer=None, raise_on_predict=None, raise_on_load=None):
        self.answer = answer
        self.raise_on_predict = raise_on_predict
        self.raise_on_load = raise_on_load
        self.load_calls: list[str] = []
        self.predict_calls: list[tuple] = []

    def load(self, name):
        self.load_calls.append(name)
        if self.raise_on_load:
            raise self.raise_on_load
        return object()

    def predict(self, state, questions, model=None):
        self.predict_calls.append((state, questions, model))
        if self.raise_on_predict:
            raise self.raise_on_predict
        return {
            "answers": {
                "profile_key": {
                    "type": "choice",
                    "choice": self.answer[0],
                    "confidence": self.answer[1],
                    "probabilities": {},
                }
            }
        }


def _backend_with_fake_router(**fake_kwargs) -> lb._LayaBackend:
    backend = lb._LayaBackend()
    backend._router = FakeRouter(**fake_kwargs)
    return backend


# ---------------------------------------------------------------------------
# env gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("val", ["", "0", "false", "False", "no", "NO", "off", "Off"])
def test_laya_disabled_for_falsy_env_values(monkeypatch, val):
    monkeypatch.setenv(lb._ENV_VAR, val)
    assert lb._laya_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "True", "yes", "on", "anything"])
def test_laya_enabled_for_truthy_env_values(monkeypatch, val):
    monkeypatch.setenv(lb._ENV_VAR, val)
    assert lb._laya_enabled() is True


def test_env_unset_means_disabled():
    assert lb._laya_enabled() is False


def test_get_backend_returns_none_when_disabled_by_default():
    assert lb.get_backend() is None
    # disabled path must not even attempt the check that would flip this.
    assert lb._backend_checked is False


def test_get_backend_returns_none_when_enabled_but_package_not_installed(monkeypatch):
    # true on this machine: laya/torch are genuinely not installed in the
    # interpreter running the test suite.
    monkeypatch.setenv(lb._ENV_VAR, "1")
    result = lb.get_backend()
    assert result is None
    # the negative result is cached so repeated /health polling doesn't
    # keep retrying a doomed import.
    assert lb._backend_checked is True
    assert lb.get_backend() is None


def test_get_backend_succeeds_with_a_fake_installed_laya_package(monkeypatch):
    # simulate "laya is installed" without needing real torch: inject a
    # fake module into sys.modules so `from laya import Router` inside
    # get_backend() resolves to our stand-in.
    fake_module = types.ModuleType("laya")
    fake_module.Router = FakeRouter
    monkeypatch.setitem(sys.modules, "laya", fake_module)
    monkeypatch.setenv(lb._ENV_VAR, "1")

    backend = lb.get_backend()
    assert backend is not None
    assert isinstance(backend, lb._LayaBackend)
    # constructing the backend must not itself build a router / load a model.
    assert backend._router is None


def test_get_backend_is_cached_across_calls(monkeypatch):
    fake_module = types.ModuleType("laya")
    fake_module.Router = FakeRouter
    monkeypatch.setitem(sys.modules, "laya", fake_module)
    monkeypatch.setenv(lb._ENV_VAR, "1")

    first = lb.get_backend()
    second = lb.get_backend()
    assert first is second


# ---------------------------------------------------------------------------
# laziness -- importing the module must not import torch/laya or build a model
# ---------------------------------------------------------------------------


def test_importing_the_module_does_not_import_laya_or_torch():
    assert "laya" not in sys.modules
    assert "torch" not in sys.modules


def test_constructing_backend_does_not_build_a_router():
    backend = lb._LayaBackend()
    assert backend._router is None
    assert backend._unusable is False


# ---------------------------------------------------------------------------
# classify() -- the actual contract
# ---------------------------------------------------------------------------


def test_classify_returns_key_and_confidence_on_a_normal_answer():
    backend = _backend_with_fake_router(answer=("personal.email", 0.93))
    result = backend.classify(_field(label="Email"), ["personal.email", "personal.phone"])
    assert result == ("personal.email", 0.93)


def test_classify_maps_none_answer_to_none():
    backend = _backend_with_fake_router(answer=("none", 0.99))
    result = backend.classify(_field(label="Cover letter"), ["personal.email", "personal.phone"])
    assert result is None


def test_classify_maps_out_of_range_answer_to_none():
    # a malformed/hallucinated choice that was never offered as a
    # candidate must never be handed back to the ladder.
    backend = _backend_with_fake_router(answer=("personal.password", 0.99))
    result = backend.classify(_field(label="Something"), ["personal.email", "personal.phone"])
    assert result is None


def test_classify_never_returns_a_key_outside_candidate_keys():
    candidates = ["personal.email", "personal.phone", "experience.current_company"]
    for bogus in ("none", "made_up_key", "personal.password", ""):
        backend = _backend_with_fake_router(answer=(bogus, 0.99))
        result = backend.classify(_field(label="X"), candidates)
        if result is not None:
            assert result[0] in candidates


def test_classify_swallows_predict_exception():
    backend = _backend_with_fake_router(raise_on_predict=RuntimeError("boom"))
    result = backend.classify(_field(label="Email"), ["personal.email"])
    assert result is None


def test_classify_swallows_malformed_response_shape():
    backend = lb._LayaBackend()

    class MalformedRouter:
        def predict(self, state, questions, model=None):
            return {"answers": {}}  # missing "profile_key" entirely

    backend._router = MalformedRouter()
    result = backend.classify(_field(label="Email"), ["personal.email"])
    assert result is None


def test_classify_returns_none_when_router_is_unusable():
    backend = lb._LayaBackend()
    backend._unusable = True
    result = backend.classify(_field(label="Email"), ["personal.email"])
    assert result is None


def test_classify_returns_none_for_empty_candidate_list_without_calling_router():
    backend = lb._LayaBackend()

    class ExplodingRouter:
        def predict(self, *a, **k):
            raise AssertionError("should never be called with zero candidates")

    backend._router = ExplodingRouter()
    result = backend.classify(_field(label="Email"), [])
    assert result is None


def test_classify_passes_english_model_explicitly():
    fake = FakeRouter(answer=("personal.email", 0.9))
    backend = lb._LayaBackend()
    backend._router = fake
    backend.classify(_field(label="Email"), ["personal.email"])
    assert len(fake.predict_calls) == 1
    _, _, model = fake.predict_calls[0]
    assert model == "english"


# ---------------------------------------------------------------------------
# warmup()
# ---------------------------------------------------------------------------


def test_warmup_loads_english_checkpoint_and_reports_success():
    fake = FakeRouter()
    backend = lb._LayaBackend()
    backend._router = fake
    assert backend.warmup() is True
    assert fake.load_calls == ["english"]


def test_warmup_failure_marks_backend_unusable():
    backend = lb._LayaBackend()
    backend._router = FakeRouter(raise_on_load=RuntimeError("oom"))
    assert backend.warmup() is False
    assert backend._unusable is True
    # subsequent classify() calls must not try to rebuild a broken router.
    assert backend.classify(_field(label="Email"), ["personal.email"]) is None


def test_warmup_returns_false_when_router_cannot_be_built():
    backend = lb._LayaBackend()
    backend._unusable = True
    assert backend.warmup() is False


# ---------------------------------------------------------------------------
# state / question construction
# ---------------------------------------------------------------------------


def test_state_for_includes_core_field_shape():
    field = _field(label="Email Address", name="email", type="email")
    state = lb._state_for(field)
    assert state["field_label"] == "Email Address"
    assert state["field_name"] == "email"
    assert state["field_type"] == "email"


def test_state_for_omits_empty_optional_attributes():
    field = _field(label="X")
    state = lb._state_for(field)
    assert "field_placeholder" not in state
    assert "field_autocomplete" not in state
    assert "options" not in state


def test_state_for_includes_options_when_present():
    field = _field(label="Referral source", options=["LinkedIn", "Indeed"])
    state = lb._state_for(field)
    assert state["options"] == ["LinkedIn", "Indeed"]


def test_questions_for_includes_every_candidate_plus_none():
    candidates = ["personal.email", "personal.phone"]
    questions = lb._questions_for(candidates)
    criteria = questions["profile_key"]["criteria"]
    assert set(criteria) == {"personal.email", "personal.phone", "none"}
    assert questions["profile_key"]["type"] == "choice"


def test_questions_for_never_exceeds_calibrated_range():
    # matcher already caps candidates at LAYA_MAX_CANDIDATES (9); +1 for
    # "none" must stay at or under Laya's ~10-option calibrated ceiling.
    candidates = matcher.CANDIDATE_KEYS[: matcher.LAYA_MAX_CANDIDATES]
    questions = lb._questions_for(candidates)
    assert len(questions["profile_key"]["criteria"]) <= 10


def test_describe_known_key_returns_meaningful_text():
    text = lb._describe("personal.email")
    assert "email" in text.lower()


def test_describe_unknown_key_falls_back_to_path_words():
    text = lb._describe("personal.middle_name")
    assert "middle" in text.lower()
    assert "name" in text.lower()


def test_describe_falls_back_to_synonyms_when_description_missing(monkeypatch):
    descriptions = dict(lb._KEY_DESCRIPTIONS)
    descriptions.pop("personal.phone")
    monkeypatch.setattr(lb, "_KEY_DESCRIPTIONS", descriptions)
    text = lb._describe("personal.phone")
    # falls back to path words + matcher's synonym vocabulary for the key.
    assert "phone" in text.lower()
    assert any(word in text.lower() for word in matcher._KEY_SYNONYMS["personal.phone"])


# ---------------------------------------------------------------------------
# integration with the real ladder -- fake backend plugged into resolve.py
# ---------------------------------------------------------------------------


def test_fake_backend_end_to_end_through_the_ladder():
    from applypilot.extension import resolve

    profile = {"personal": {"email": "nida@example.com"}}
    backend = _backend_with_fake_router(answer=("personal.email", 0.9))
    result = resolve.resolve_field(_field(label="Contact info"), profile, laya=backend)
    assert result.source == "laya"
    assert result.value == "nida@example.com"


# ---------------------------------------------------------------------------
# optional integration test against the REAL laya package -- must never
# fail the suite on a machine without it (which is every machine except
# the probe venv used to hand-verify this module).
# ---------------------------------------------------------------------------

_laya_importable = True
try:
    import importlib.util

    _laya_importable = importlib.util.find_spec("laya") is not None
except Exception:
    _laya_importable = False


@pytest.mark.skipif(
    not _laya_importable or os.environ.get("APPLYPILOT_LAYA") != "1",
    reason="requires a real laya install and APPLYPILOT_LAYA=1 (opt-in integration test)",
)
def test_real_laya_classifies_realistic_fields(monkeypatch):
    monkeypatch.setenv(lb._ENV_VAR, "1")
    backend = lb.get_backend()
    assert backend is not None
    result = backend.classify(
        _field(label="Email Address", name="email", type="email"),
        ["personal.email", "personal.phone", "personal.full_name"],
    )
    assert result is not None
    key, confidence = result
    assert key in ("personal.email", "personal.phone", "personal.full_name")
    assert 0.0 <= confidence <= 1.0
