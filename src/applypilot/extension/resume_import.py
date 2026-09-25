"""Résumé -> draft profile import (Copilot v3, section A).

Pipeline: text extraction -> deterministic regex pass -> one LLM call for
the messy structured bits (work_history[], education[], current title,
total years) -> merge over the existing profile -> return a DRAFT.

Non-negotiables (see docs/superpowers/specs/2026-09-25-copilot-v3-product-
design.md section A):

- This module NEVER writes profile.json. It returns an ImportResult whose
  ``draft_profile`` the caller (server.py's POST /profile/import-resume)
  hands back for the operator to review and save explicitly.
- Canary fields (work authorisation, sponsorship, salary/compensation,
  EEO) are never inferred here. ``strip_canary_fields`` removes them from
  whatever the LLM contributes, defensively, even if a misbehaving model
  volunteers them despite being told not to. Whatever the *existing*
  profile already has in those sections is left completely alone --
  stripping applies only to the résumé's own contribution, before it is
  merged over the existing profile.
- Every failure mode (unsupported type, oversized file, corrupt PDF,
  empty text, no LLM configured, malformed LLM JSON) degrades to a clear
  ResumeImportError or a `warnings` entry -- never a raw exception/500.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB -- a résumé is a few hundred KB at most

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain; charset=utf-8",
}

# Résumé filename inside the profile directory, keyed by extension. Always
# derived from the validated extension -- never from the client-supplied
# filename -- so this module can never be tricked into writing outside the
# profile directory (no path traversal surface: no directory separators,
# no client-controlled basename).
RESUME_BASENAME = "resume"
TEXT_RENDERING_NAME = "resume.txt"

# Profile sections a résumé must never populate, no matter what the LLM
# returns. Mirrors canary.py's exact scope: work authorisation/sponsorship,
# salary/compensation expectation, and EEO voluntary disclosure. A résumé
# does not reliably state any of these, and guessing them causes real-world
# harm (see resolve.py tier-1 canary invariant).
CANARY_TOP_LEVEL_KEYS = {"work_authorization", "compensation", "eeo_voluntary"}

# The one secret path anywhere in the profile (mirrors
# applypilot.extension.resolve.SECRET_PROFILE_PATHS) -- belt-and-braces,
# since nothing in this module ever produces a password, but the merge
# helper is generic enough that a future field addition should not be able
# to smuggle one through.
SECRET_PERSONAL_KEYS = {"password"}


class ResumeImportError(Exception):
    """A user-facing, safe-to-display import failure. Never a stack trace."""


class IdentityMismatch(ResumeImportError):
    """The résumé names a different person than the active profile.

    Uploading a résumé writes resume.pdf/resume.txt into the ACTIVE
    profile's directory and drafts that person's profile from it. Uploading
    someone else's résumé therefore replaced one person's identity with
    another's — on a single-profile install, the very files the autonomous
    pipeline applies with. Raised before anything is written."""

    def __init__(self, resume_name: str, profile_name: str):
        self.resume_name = resume_name
        self.profile_name = profile_name
        super().__init__(
            f"This résumé is for {resume_name}, but the active profile is {profile_name}. "
            f"Uploading it would replace {profile_name}'s résumé and details with "
            f"{resume_name}'s. Create or switch to a profile for {resume_name} instead — "
            "or confirm that you want to replace this profile's identity.")


# ---------------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------------


def validate_upload(filename: str, data: bytes) -> str:
    """Validate size and extension before anything else is read/parsed.

    Returns the lowercase extension (one of ALLOWED_EXTENSIONS) or raises
    ResumeImportError. The extension is derived only to pick a parser and a
    storage filename -- the client-supplied basename is discarded.
    """
    if not data:
        raise ResumeImportError("Uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ResumeImportError(
            f"Résumé file is too large ({len(data) // 1024} KB) -- max "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ResumeImportError(
            f"Unsupported file type {ext or '(no extension)'!r} -- upload a .pdf, .docx, or .txt résumé."
        )
    return ext


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def _extract_text_pdf(data: bytes) -> str:
    import io

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover -- pypdf is a hard dependency here
        raise ResumeImportError("PDF support is unavailable (pypdf is not installed).") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:
                raise ResumeImportError(
                    "This PDF is password-protected -- remove the password and re-upload."
                )
        pages = [page.extract_text() or "" for page in reader.pages]
    except ResumeImportError:
        raise
    except Exception as exc:
        raise ResumeImportError(
            "Could not read this PDF -- it may be corrupted or in an unsupported format."
        ) from exc
    return "\n".join(pages)


def _extract_text_docx(data: bytes) -> str:
    import io

    try:
        import docx  # python-docx -- lazily imported, NOT a hard dependency
    except ImportError as exc:
        raise ResumeImportError(
            "DOCX support needs the 'python-docx' package, which is not installed. "
            "Install it, or upload a .pdf or .txt résumé instead."
        ) from exc

    try:
        document = docx.Document(io.BytesIO(data))
        paragraphs = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text:
                        paragraphs.append(cell.text)
    except Exception as exc:
        raise ResumeImportError(
            "Could not read this .docx file -- it may be corrupted or not a real Word document."
        ) from exc
    return "\n".join(paragraphs)


def _extract_text_txt(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_text(data: bytes, ext: str) -> str:
    """Dispatch to the right extractor for `ext` (as returned by validate_upload)."""
    if ext == ".pdf":
        text = _extract_text_pdf(data)
    elif ext == ".docx":
        text = _extract_text_docx(data)
    elif ext == ".txt":
        text = _extract_text_txt(data)
    else:  # pragma: no cover -- validate_upload already enforces this
        raise ResumeImportError(f"Unsupported file type: {ext!r}")

    if not text or not text.strip():
        raise ResumeImportError(
            "Could not extract any text from this résumé -- it may be empty, "
            "scanned/image-only, or corrupted."
        )
    return text


# ---------------------------------------------------------------------------
# Deterministic pass -- high-confidence regex, no model
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<!\d)(\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)"
)
_LINKEDIN_RE = re.compile(r"(https?://)?(www\.)?linkedin\.com/[A-Za-z0-9/_\-.%]+", re.I)
_GITHUB_RE = re.compile(r"(https?://)?(www\.)?github\.com/[A-Za-z0-9/_\-.%]+", re.I)
_URL_RE = re.compile(r"https?://[^\s,;()<>\"']+", re.I)


def _clean_url(url: str) -> str:
    url = url.rstrip(").,;")
    if not url.lower().startswith("http"):
        url = "https://" + url
    return url


def _guess_name(text: str) -> str:
    """Best-effort: the first non-empty line that doesn't look like contact
    info. Reliable for the large majority of résumés (name-first header),
    exactly as the design doc calls for -- not meant to be exhaustive."""
    for line in text.splitlines()[:8]:
        candidate = line.strip()
        if not candidate:
            continue
        if _EMAIL_RE.search(candidate) or _URL_RE.search(candidate):
            continue
        if _PHONE_RE.search(candidate):
            continue
        if len(candidate) > 60:
            continue
        return candidate
    return ""


_NAME_TOKEN_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|['’.-])*$")
# Words that make a first line a heading or a job title, not a name.
_NOT_NAME_WORDS = {
    "resume", "résumé", "curriculum", "vitae", "cv", "portfolio", "profile", "summary",
    "senior", "junior", "lead", "principal", "staff", "intern", "engineer", "developer",
    "designer", "manager", "analyst", "scientist", "consultant", "specialist", "director",
    "architect", "administrator", "product", "data", "software", "ux", "ui", "research",
}


def _name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[\s,]+", (name or "").lower().replace(".", " ")) if t]


def looks_like_person_name(name: str) -> bool:
    """2-5 alphabetic tokens — enough to trust _guess_name's first line as a
    name rather than a heading like "RESUME" or "Product Designer Portfolio"
    (a guard that fires on a heading would block legitimate uploads)."""
    toks = (name or "").split()
    return (2 <= len(toks) <= 5 and all(_NAME_TOKEN_RE.match(t) for t in toks)
            and not any(t.lower().strip(".") in _NOT_NAME_WORDS for t in toks))


def same_person(a: str, b: str) -> bool:
    """Lenient: same first name and same (or initial-compatible) last name,
    or one name's tokens contained in the other's (middle names)."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return True  # nothing to compare — never block on missing data
    if set(ta) <= set(tb) or set(tb) <= set(ta):
        return True
    if ta[0] != tb[0]:
        return False
    la, lb = ta[-1], tb[-1]
    return la == lb or (len(la) == 1 and lb.startswith(la)) or (len(lb) == 1 and la.startswith(lb))


def deterministic_extract(text: str) -> dict[str, str]:
    """High-confidence contact-detail extraction. Returns a dotted-path ->
    value dict containing only the paths it actually found (never a path
    with an empty/absent value), so the caller can use `dict.keys()` as
    the provenance list for this pass."""
    out: dict[str, str] = {}

    name = _guess_name(text)
    if name:
        out["personal.full_name"] = name

    email_m = _EMAIL_RE.search(text)
    if email_m:
        out["personal.email"] = email_m.group(0)

    phone_m = _PHONE_RE.search(text)
    if phone_m:
        out["personal.phone"] = phone_m.group(0).strip()

    linkedin_m = _LINKEDIN_RE.search(text)
    if linkedin_m:
        out["personal.linkedin_url"] = _clean_url(linkedin_m.group(0))

    github_m = _GITHUB_RE.search(text)
    if github_m:
        out["personal.github_url"] = _clean_url(github_m.group(0))

    # Portfolio/website: the first URL that isn't LinkedIn or GitHub.
    for url_m in _URL_RE.finditer(text):
        url = url_m.group(0)
        if _LINKEDIN_RE.search(url) or _GITHUB_RE.search(url):
            continue
        out["personal.portfolio_url"] = _clean_url(url)
        break

    return out


# ---------------------------------------------------------------------------
# LLM pass -- work_history[], education[], current title, total years
# ---------------------------------------------------------------------------

_ALLOWED_WORK_KEYS = ("title", "company", "location", "start", "end", "current", "description")
_ALLOWED_EDU_KEYS = ("school", "degree", "field", "start", "end", "gpa")

_LLM_SYSTEM_PROMPT = """You extract structured career facts from a résumé's raw text. \
Output ONLY a single JSON object -- no prose, no markdown code fences, no commentary.

Extract ONLY facts explicitly present in the text. If something is unknown, use "" \
(or an empty list). Never invent an employer, school, date, or accomplishment.

For each work_history entry's "description", preserve the résumé's own bullet points \
VERBATIM -- do not paraphrase, summarize, or merge them into a paragraph. Format it as \
one bullet per line, each line starting with "- ", exactly matching the accomplishments \
and responsibilities as written in the résumé, in the same order. If the résumé \
describes a role in plain sentences with no bullet markers, split it into one short \
bullet per sentence instead of one long paragraph -- but never invent or add a bullet \
whose content is not already present in the résumé text.

For each education entry's "gpa", include it ONLY when the résumé explicitly states a \
GPA for that specific degree, written exactly as it appears (e.g. "3.8" or "3.8/4.0" or \
"8.9/10") -- never invent, estimate, or convert a GPA to a different scale. If a GPA is \
present but you cannot tell which of two or more degrees it belongs to, leave "gpa" as \
"" on every entry rather than guessing.

Do NOT include anything about work authorization, visa/sponsorship status, salary or \
compensation expectations, or EEO/demographic information (gender, race, ethnicity, \
veteran status, disability status) -- even if the résumé happens to mention them. \
Leave those out of the JSON entirely.

Match this exact shape:
{
  "work_history": [
    {"title": "", "company": "", "location": "", "start": "MM/YYYY", "end": "MM/YYYY", \
"current": false, "description": "- first bullet\\n- second bullet"}
  ],
  "education": [
    {"school": "", "degree": "", "field": "", "start": "MM/YYYY", "end": "MM/YYYY", "gpa": ""}
  ],
  "current_title": "",
  "total_years_experience": ""
}

Dates must be "MM/YYYY" or "" (never a bare year, never a full date). "current" is a \
JSON boolean; when true, "end" should be "". List work_history and education in \
reverse-chronological order, as the résumé presents them."""

_MAX_RESUME_CHARS_FOR_LLM = 16000


def _build_llm_messages(text: str) -> list[dict]:
    body = text[:_MAX_RESUME_CHARS_FOR_LLM]
    return [
        {"role": "system", "content": _LLM_SYSTEM_PROMPT},
        {"role": "user", "content": f"RESUME TEXT:\n\n{body}"},
    ]


def _default_llm_fn(text: str) -> str:
    """Real (non-test) LLM call. Same established shape as
    apply/answer_cache.py's _default_llm_fn: lazy import of the shared
    client, one call, plain text back.

    Uses ``llm_util.get_llm_client()`` rather than ``applypilot.llm.get_client()``
    directly, so an operator with no LLM provider env var set but the
    Claude Code CLI installed still gets a working résumé import -- see
    llm_util's module docstring. Everything else (provider order when an
    env var IS set, fail-soft-to-warning behaviour in llm_extract() above)
    is unchanged."""
    from applypilot.extension.llm_util import get_llm_client

    messages = _build_llm_messages(text)
    return get_llm_client().chat(messages, max_tokens=3000, temperature=0.0)


def _extract_json_object(raw: str) -> dict | None:
    """Best-effort recovery of a JSON object from LLM output that may be
    wrapped in markdown fences or preceded/followed by stray prose. Returns
    None (never raises) when nothing parseable can be found."""
    if not raw:
        return None
    candidate = raw.strip()
    # Strip a ```json ... ``` or ``` ... ``` fence if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", candidate, re.S | re.I)
    if fence:
        candidate = fence.group(1).strip()
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    # Fall back to the outermost {...} span, for output with leading/
    # trailing commentary the model added despite instructions.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _as_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1"}
    return False


# Any common bullet marker (hyphen, asterisk, the usual unicode bullet
# glyphs, or a numbered "1." / "1)") at the start of a line, plus the
# whitespace after it -- stripped and replaced with a uniform "- " so every
# bullet in the stored description looks the same regardless of how the
# résumé (or the model echoing it) originally marked it.
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*•‣◦⁃∙·]|[0-9]{1,2}[.)])\s+")
# A sentence boundary: punctuation followed by whitespace and a capital
# letter/digit/opening paren -- used only as a last resort, to split a
# single-paragraph description (no line breaks at all) into one bullet per
# sentence, never to invent content.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])")


def _normalize_description(raw: str) -> str:
    """Normalise a work_history description into newline-separated "- "
    bullet lines -- résumés are written in bullets, and ATS "Role
    Description" boxes are filled with bullets, never a single paragraph.

    - Text that already has one item per line (its own bullet marker or
      not) stays one bullet per line, verbatim apart from the marker being
      replaced with a uniform "- " -- order and wording untouched.
    - A single blob with no line breaks (the common shape of a raw LLM
      response despite the prompt) is split on sentence boundaries so each
      sentence becomes its own bullet. Nothing is invented: the words are
      exactly what was given, just re-segmented.
    """
    text = (raw or "").strip()
    if not text:
        return ""

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 1:
        blob = _BULLET_PREFIX_RE.sub("", lines[0] if lines else text).strip()
        lines = [s.strip() for s in _SENTENCE_SPLIT_RE.split(blob) if s.strip()] or (
            [blob] if blob else []
        )

    bullets = [f"- {cleaned}" for ln in lines if (cleaned := _BULLET_PREFIX_RE.sub("", ln).strip())]
    return "\n".join(bullets)


# ---------------------------------------------------------------------------
# GPA -- a deterministic, high-precision regex pass (no model needed), plus
# strict validation of whatever the LLM proposes per education entry. A GPA
# is a factual claim about the applicant's academic record, so a value this
# module cannot confidently place on the right degree is dropped entirely
# (with a warning) rather than risk stating someone else's GPA on their
# behalf -- see the module docstring's canary-adjacent non-negotiables.
# ---------------------------------------------------------------------------

_NUM = r"\d{1,2}(?:\.\d{1,3})?"
_GPA_LABEL = r"(?:cumulative\s+gpa|cgpa|gpa)"
# "GPA: 3.8/4.0", "GPA 3.8", "CGPA 8.9/10", "Cumulative GPA: 3.85"
_GPA_LABEL_FIRST_RE = re.compile(
    rf"\b{_GPA_LABEL}\b\s*[:\-]?\s*({_NUM})(?:\s*/\s*({_NUM}))?", re.I
)
# "3.8/4.0 GPA"
_GPA_VALUE_FIRST_RE = re.compile(rf"({_NUM})\s*/\s*({_NUM})\s*\b{_GPA_LABEL}\b", re.I)

# Scales this module will actually honour when the résumé states one
# explicitly. Default (no denominator given) is always 4.0 -- a bare "GPA
# 8.9" is not a plausible 4.0-scale value and is rejected, never guessed
# into a different scale.
_VALID_GPA_SCALES = {4.0, 5.0, 10.0}
_DEFAULT_GPA_SCALE = 4.0


def _find_gpa_mentions(text: str) -> list[tuple[int, str]]:
    """Every GPA-shaped mention in `text`, one per line (first match only),
    as ``(line_index, raw_value)`` -- ``raw_value`` is the number and, if
    stated, its scale (e.g. "3.8" or "3.8/4.0"), never the label. Purely
    mechanical: a candidate here may still fail `_validate_gpa`."""
    mentions: list[tuple[int, str]] = []
    for i, line in enumerate((text or "").splitlines()):
        m = _GPA_LABEL_FIRST_RE.search(line)
        if m:
            value, scale = m.group(1), m.group(2)
            mentions.append((i, f"{value}/{scale}" if scale else value))
            continue
        m = _GPA_VALUE_FIRST_RE.search(line)
        if m:
            mentions.append((i, f"{m.group(1)}/{m.group(2)}"))
    return mentions


def _validate_gpa(raw: str) -> str | None:
    """Strictly validate a candidate GPA string -- from the deterministic
    pass above or volunteered by the LLM -- and return it normalised
    (résumé's own formatting preserved: "3.8" or "3.8/4.0"), or None if it
    is not a plausible GPA. A scale is only ever what the résumé stated
    explicitly (4.0/5.0/10.0); with none given, 4.0 is assumed and the
    value must fit it. Never converts between scales."""
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.match(rf"^({_NUM})\s*/\s*({_NUM})$", raw)
    if m:
        value_s, scale_s = m.group(1), m.group(2)
        value, scale = float(value_s), float(scale_s)
        if scale not in _VALID_GPA_SCALES or value < 0 or value > scale:
            return None
        return f"{value_s}/{scale_s}"
    m = re.match(rf"^({_NUM})$", raw)
    if m:
        value_s = m.group(1)
        value = float(value_s)
        if value < 0 or value > _DEFAULT_GPA_SCALE:
            return None
        return value_s
    return None


def _education_block_starts(lines: list[str], entries: list[dict]) -> list[int | None]:
    """For each education entry (in résumé order), the index of the first
    line that names it (by school, else degree), or None if no confident
    match was found. Searched strictly in order, each entry's search
    starting after the previous entry's match, so two similarly-worded
    entries never collapse onto the same line."""
    starts: list[int | None] = []
    search_from = 0
    for entry in entries:
        candidates = [c.strip() for c in (entry.get("school"), entry.get("degree")) if c and c.strip()]
        found = None
        for cand in candidates:
            cand_lower = cand.lower()
            for i in range(search_from, len(lines)):
                if cand_lower in lines[i].lower():
                    found = i
                    break
            if found is not None:
                break
        starts.append(found)
        if found is not None:
            search_from = found + 1
    return starts


def _attach_gpa_to_education(text: str, entries: list[dict]) -> list[str]:
    """Attach a validated GPA to the education entry it appears beside,
    using the résumé's own text (the deterministic pass takes priority over
    anything the LLM guessed per-entry -- see module docstring). Mutates
    `entries` in place, setting "gpa" only where the attachment is
    unambiguous; a mention that cannot be pinned to exactly one entry is
    left unattached and reported as a warning, never guessed.

    Returns the list of warnings (0 or more, deduplicated).
    """
    warnings: list[str] = []
    if not entries:
        return warnings
    lines = (text or "").splitlines()
    mentions = _find_gpa_mentions(text)
    if not mentions:
        return warnings

    if len(entries) == 1:
        # Only one degree on the résumé -- no ambiguity possible regardless
        # of where on the page the GPA happens to sit.
        for _line_idx, raw in mentions:
            valid = _validate_gpa(raw)
            if valid:
                entries[0]["gpa"] = valid
                break
        return warnings

    starts = _education_block_starts(lines, entries)

    def _block_end(i: int) -> int:
        for j in range(i + 1, len(starts)):
            if starts[j] is not None:
                return starts[j]
        return len(lines)

    for line_idx, raw in mentions:
        valid = _validate_gpa(raw)
        if not valid:
            continue
        matches = [
            i for i, start in enumerate(starts)
            if start is not None and start <= line_idx < _block_end(i)
        ]
        if len(matches) == 1:
            entries[matches[0]]["gpa"] = valid
        else:
            warnings.append(
                "Found a GPA on the résumé but couldn't tell which degree it belongs "
                "to -- add it manually rather than risk it on the wrong one."
            )
    return list(dict.fromkeys(warnings))


def _validate_entry(raw_entry, allowed_keys: tuple[str, ...]) -> dict | None:
    """Keep only the allow-listed keys of one work_history/education entry,
    coercing every value to a safe plain type. Non-dict entries are dropped
    (returns None) rather than raising -- one bad item from a hostile/buggy
    LLM response must not sink the whole list."""
    if not isinstance(raw_entry, dict):
        return None
    out: dict = {}
    for key in allowed_keys:
        if key == "current":
            out[key] = _as_bool(raw_entry.get(key))
        elif key == "description":
            out[key] = _normalize_description(_as_str(raw_entry.get(key)))
        elif key == "gpa":
            # Strict validation -- an implausible or malformed value from
            # the LLM is dropped (stored as "", same as "not stated") rather
            # than surfaced as a fact about the applicant's record.
            out[key] = _validate_gpa(_as_str(raw_entry.get(key))) or ""
        else:
            out[key] = _as_str(raw_entry.get(key))
    # Drop entries that carry no identifying information at all.
    if allowed_keys is _ALLOWED_WORK_KEYS and not (out["title"] or out["company"]):
        return None
    if allowed_keys is _ALLOWED_EDU_KEYS and not (out["school"] or out["degree"]):
        return None
    return out


def parse_llm_json(raw: str) -> tuple[dict, list[str]]:
    """Parse and hard-validate the LLM's response.

    Returns (fields, warnings). `fields` contains only recognised keys
    (work_history, education, current_title, total_years_experience) --
    this is itself an allow-list, so anything else the model volunteers
    (canary fields among them) is dropped here before it ever reaches the
    merge step. Never raises: malformed/hostile/empty output degrades to
    ({}, [warning]).
    """
    warnings: list[str] = []
    parsed = _extract_json_object(raw)
    if parsed is None:
        return {}, ["The AI extraction returned no valid JSON -- work history, education, "
                     "title and years of experience need manual entry."]

    fields: dict = {}

    work_raw = parsed.get("work_history")
    if isinstance(work_raw, list):
        entries = [e for e in (_validate_entry(x, _ALLOWED_WORK_KEYS) for x in work_raw) if e]
        if entries:
            fields["work_history"] = entries
    elif "work_history" in parsed:
        warnings.append("AI response had a malformed work_history -- skipped, fill in manually.")

    edu_raw = parsed.get("education")
    if isinstance(edu_raw, list):
        entries = [e for e in (_validate_entry(x, _ALLOWED_EDU_KEYS) for x in edu_raw) if e]
        if entries:
            fields["education"] = entries
    elif "education" in parsed:
        warnings.append("AI response had a malformed education list -- skipped, fill in manually.")

    current_title = _as_str(parsed.get("current_title"))
    if current_title:
        fields["current_title"] = current_title

    total_years = _as_str(parsed.get("total_years_experience"))
    if total_years:
        fields["total_years_experience"] = total_years

    # Every other key the model returned (including, deliberately, any
    # canary-shaped one) is simply never looked at -- `fields` only ever
    # contains the four keys handled above.
    return fields, warnings


def llm_extract(text: str, llm_fn: Callable[[str], str] | None = None) -> tuple[dict, list[str]]:
    """Run the one-time LLM pass. Never raises: any failure (no provider
    configured, network error, timeout, ...) degrades to ({}, [warning])
    so the deterministic fields are still returned."""
    fn = llm_fn or _default_llm_fn
    try:
        raw = fn(text)
    except Exception as exc:  # noqa: BLE001 -- degrade, never crash the import
        return {}, [
            f"AI extraction is unavailable ({exc}) -- work history, education, title "
            "and years of experience need manual entry."
        ]
    return parse_llm_json(raw)


# ---------------------------------------------------------------------------
# Canary stripping + merge
# ---------------------------------------------------------------------------


def strip_canary_fields(d: dict) -> dict:
    """Remove every canary-owned top-level section and the secret password
    field from `d`, in place, and return it. Defensive: called on whatever
    the résumé pass contributes, even though that contribution should never
    contain these keys in the first place (parse_llm_json already allow-
    lists), specifically so a future bug in the allow-list still cannot let
    one through."""
    for key in CANARY_TOP_LEVEL_KEYS:
        d.pop(key, None)
    personal = d.get("personal")
    if isinstance(personal, dict):
        for key in SECRET_PERSONAL_KEYS:
            personal.pop(key, None)
    return d


def _dotted_set(d: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _deep_merge(base: dict, overlay: dict) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def build_draft_profile(
    existing_profile: dict, deterministic: dict[str, str], llm_fields: dict
) -> dict:
    """Merge the résumé's contribution over a copy of `existing_profile`.

    Anything the résumé does not mention is left exactly as it was in
    `existing_profile` -- including any canary section the operator has
    already filled in by hand, which this function never touches because
    the *contribution* being merged in never contains those keys
    (strip_canary_fields is applied to the contribution, not the result).
    """
    draft = copy.deepcopy(existing_profile) if isinstance(existing_profile, dict) else {}

    contribution: dict = {}
    for path, value in deterministic.items():
        _dotted_set(contribution, path, value)

    if "work_history" in llm_fields:
        contribution["work_history"] = llm_fields["work_history"]
    if "education" in llm_fields:
        contribution["education"] = llm_fields["education"]
    if llm_fields.get("current_title"):
        _dotted_set(contribution, "experience.current_job_title", llm_fields["current_title"])
    if llm_fields.get("total_years_experience"):
        _dotted_set(
            contribution, "experience.years_of_experience_total", llm_fields["total_years_experience"]
        )

    contribution = strip_canary_fields(contribution)
    _deep_merge(draft, contribution)
    return draft


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    draft_profile: dict
    provenance: dict[str, str]  # dotted path (or "work_history"/"education") -> "deterministic"|"llm"
    warnings: list[str] = field(default_factory=list)
    saved_filename: str = ""      # e.g. "resume.pdf" -- relative to the profile dir
    content_type: str = ""


def import_resume(
    *,
    filename: str,
    data: bytes,
    existing_profile: dict,
    profile_dir: Path,
    llm_fn: Callable[[str], str] | None = None,
    allow_identity_change: bool = False,
) -> ImportResult:
    """Full résumé-import pipeline. Never writes profile.json -- the caller
    (server.py) is responsible for returning ``draft_profile`` to the
    operator for review, and only POST /profile (a separate, explicit
    endpoint) ever persists a profile.

    Raises ResumeImportError for anything the operator needs to fix
    (unsupported type, oversized file, corrupt/unreadable file, no
    extractable text). Any LLM failure is soft -- recorded in
    ``warnings``, never raised.
    """
    ext = validate_upload(filename, data)
    text = extract_text(data, ext)

    if not allow_identity_change:
        resume_name = _guess_name(text)
        profile_name = str(((existing_profile or {}).get("personal") or {}).get("full_name") or "").strip()
        if (profile_name and looks_like_person_name(resume_name)
                and not same_person(resume_name, profile_name)):
            raise IdentityMismatch(resume_name, profile_name)

    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    saved_filename = f"{RESUME_BASENAME}{ext}"
    (profile_dir / saved_filename).write_bytes(data)
    # The rest of the pipeline expects a resume.txt rendering to exist
    # regardless of the uploaded format. When the upload IS resume.txt this
    # is a harmless repeat of the same write.
    (profile_dir / TEXT_RENDERING_NAME).write_text(text, encoding="utf-8")

    deterministic = deterministic_extract(text)
    provenance: dict[str, str] = {path: "deterministic" for path in deterministic}

    llm_fields, warnings = llm_extract(text, llm_fn=llm_fn)
    if "work_history" in llm_fields:
        provenance["work_history"] = "llm"
    if "education" in llm_fields:
        provenance["education"] = "llm"
        # Deterministic GPA pass takes priority over whatever the LLM
        # guessed per-entry -- see the module docstring. Runs only once
        # this résumé's own education entries exist to attach onto; a
        # profile with no education contribution at all has nowhere to
        # confidently place a GPA anyway.
        warnings.extend(_attach_gpa_to_education(text, llm_fields["education"]))
    if llm_fields.get("current_title"):
        provenance["experience.current_job_title"] = "llm"
    if llm_fields.get("total_years_experience"):
        provenance["experience.years_of_experience_total"] = "llm"

    draft_profile = build_draft_profile(existing_profile, deterministic, llm_fields)

    return ImportResult(
        draft_profile=draft_profile,
        provenance=provenance,
        warnings=warnings,
        saved_filename=saved_filename,
        content_type=CONTENT_TYPES.get(ext, "application/octet-stream"),
    )


# ---------------------------------------------------------------------------
# Serving the stored résumé back (GET /resume, GET /resume/info)
# ---------------------------------------------------------------------------

# Priority order when more than one resume.* file exists in the profile
# directory (e.g. a .docx upload plus the resume.txt rendering written
# alongside it) -- prefer the operator's original file over the derived
# text rendering.
_SERVE_PRIORITY = (".pdf", ".docx", ".txt")


def find_stored_resume(profile_dir: Path) -> Path | None:
    """Return the path to the stored résumé file to serve, or None if
    nothing has been uploaded for this profile yet."""
    profile_dir = Path(profile_dir)
    for ext in _SERVE_PRIORITY:
        candidate = profile_dir / f"{RESUME_BASENAME}{ext}"
        if candidate.is_file():
            return candidate
    return None


def content_type_for(path: Path) -> str:
    return CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")
