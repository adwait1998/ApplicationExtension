"""Wire shapes shared with the Chrome extension's content script.

Mirrors the JSON in docs/superpowers/specs/2026-09-23-copilot-extension-design.md
exactly, so ``FillPlan.to_dict()`` is the literal response body of POST
/resolve. Plain dataclasses (not pydantic) — the pydantic request models
that parse the incoming JSON live in server.py and convert into these.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class FieldDescriptor:
    """One form field as scanned by the content script.

    ``id`` is opaque and assigned by the content script; the service never
    needs to know anything about DOM structure beyond what is described
    here (selector is round-tripped back to the extension, never used
    server-side).
    """

    id: str
    selector: str = ""
    tag: str = ""
    type: str = ""
    name: str = ""
    autocomplete: str = ""
    label: str = ""
    placeholder: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)
    # Repeating-section context (Workday "Work Experience 2", etc.), sent by
    # the scanner. Both optional and defaulted so every existing caller and
    # test keeps working unchanged. ``section`` is the nearest enclosing
    # heading/legend text; ``section_index`` is the 1-based position parsed
    # from it (or None when it could not be parsed / there is no repeating
    # section). Consumed by tier 3 (applypilot.extension.structured).
    section: str = ""
    section_index: int | None = None
    # The Workday widget driver the content script bound this field to
    # (e.g. "wd-dropdown", "wd-prompt", "wd-date-my", "wd-date-y"), or "" for
    # a plain native input/select. Purely informational to the service --
    # resolution is still driven by label/section as before; the JS side
    # uses `id` to know which driver to invoke for a fill. Optional and
    # defaulted so every existing caller/test keeps working unchanged.
    widget: str = ""


@dataclass
class FillResult:
    """A field the ladder decided to fill."""

    id: str
    value: str
    source: str  # "canary" | "deterministic" | "structured" | "laya" | "answer_bank" | "draft"
    profile_key: str
    confidence: float
    auto_fill: bool
    reason: str
    # Tier 6 only: this value is LLM-generated text, not a fact pulled from
    # the profile. Defaults False so every other tier (and every existing
    # caller/test) is unchanged. The extension renders draft=True fills
    # distinctly (blue, "review before submitting") -- see content.js /
    # popup.js, which check `fill.draft || fill.source === "draft"`.
    draft: bool = False
    # Multi-value fields only (currently just Skills): the individual values
    # a widget driver should add one at a time (e.g. Workday's "Type to Add
    # Skills" prompt, which needs one add-cycle per skill). `value` above is
    # still populated too (joined with ", ") so a plain text field still
    # gets something sensible. Empty for every other field -- see to_dict().
    values: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d.get("values"):
            d.pop("values", None)
        return d


@dataclass
class SkipResult:
    """A field the ladder left for the human, with why."""

    id: str
    source: str  # "canary" | "deterministic" | "structured" | "secret_guard" | "draft" | "unresolved"
    reason: str
    auto_fill: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FillPlan:
    """The full POST /resolve response."""

    fills: list[FillResult] = field(default_factory=list)
    skipped: list[SkipResult] = field(default_factory=list)
    tiers_available: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fills": [f.to_dict() for f in self.fills],
            "skipped": [s.to_dict() for s in self.skipped],
            "tiers_available": list(self.tiers_available),
        }
