"""URL → canonical ATS reference. Single source of truth for ATS detection,
superseding prefill._detect_ats and skill_runner._infer_ats (kept as thin
delegators). Exact-host matching only — substring 'lever'/'ashby' matching
false-positives (e.g. 'cleverhealth')."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse, unquote

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,80}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_GH_JID_RE = re.compile(r"[?&]gh_jid=(\d+)", re.I)
_WD_HOST_RE = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.(?P<dc>wd\d+)\.myworkdayjobs\.com$")
_WD_REQ_RE = re.compile(r"_([A-Za-z0-9-]+)$")
_VANITY_STOP = {"greenhouse", "lever", "ashbyhq", "myworkdayjobs", "icims"}


@dataclass(frozen=True)
class AtsRef:
    ats: str
    token: str
    job_id: str | None
    confident: bool = True


def _clean_token(value: str | None) -> str | None:
    if not value:
        return None
    tok = unquote(value).strip().strip("/")
    if not tok or tok.lower() in {"jobs", "job", "embed", "departments", "applications"}:
        return None
    if not _TOKEN_RE.match(tok):
        return None
    return tok.lower()


def parse_ats_url(url: str) -> AtsRef | None:
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    host = parsed.netloc.lower()
    parts = [unquote(p) for p in parsed.path.split("/") if p]

    # Greenhouse — canonical hosts
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and parts:
        token = _clean_token(parts[0])
        job_id = None
        if "jobs" in parts:
            i = parts.index("jobs")
            if i + 1 < len(parts) and parts[i + 1].isdigit():
                job_id = parts[i + 1]
        if job_id is None:
            m = _GH_JID_RE.search(url)
            job_id = m.group(1) if m else None
        return AtsRef("greenhouse", token, job_id, confident=True) if token else None
    if host == "boards-api.greenhouse.io" and len(parts) >= 3 and parts[:2] == ["v1", "boards"]:
        token = _clean_token(parts[2])
        return AtsRef("greenhouse", token, None, confident=True) if token else None

    # Lever — exact host
    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and parts:
        token = _clean_token(parts[0])
        job_id = parts[1] if len(parts) >= 2 and _UUID_RE.match(parts[1]) else None
        return AtsRef("lever", token, job_id, confident=True) if token else None
    if host == "api.lever.co" and len(parts) >= 3 and parts[:2] == ["v0", "postings"]:
        token = _clean_token(parts[2])
        return AtsRef("lever", token, None, confident=True) if token else None

    # Ashby — exact host
    if host == "jobs.ashbyhq.com" and parts:
        token = _clean_token(parts[0])
        job_id = parts[1] if len(parts) >= 2 and _UUID_RE.match(parts[1]) else None
        return AtsRef("ashby", token, job_id, confident=True) if token else None
    if host == "api.ashbyhq.com" and len(parts) >= 3 and parts[:2] == ["posting-api", "job-board"]:
        token = _clean_token(parts[2])
        return AtsRef("ashby", token, None, confident=True) if token else None

    # Workday — tenant.wdN.myworkdayjobs.com
    wd = _WD_HOST_RE.match(host)
    if wd:
        job_id = None
        if parts:
            m = _WD_REQ_RE.search(parts[-1])
            job_id = m.group(1) if m else None
        return AtsRef("workday", wd.group("tenant"), job_id, confident=True)

    # Greenhouse — vanity domain (gh_jid present, token guessed from host)
    m = _GH_JID_RE.search(url)
    if m:
        job_id = m.group(1)
        company = None
        if host.startswith("careers."):
            company = host.split(".")[1]
        elif host.endswith(".careers"):
            company = host.rsplit(".", 1)[0]
        elif host.startswith("jobs."):
            company = host.split(".")[1]
        else:
            path = parsed.path.lower()
            if any(seg in path for seg in ("/careers", "/positions", "/jobs", "/job")):
                labels = [p for p in host.split(".") if p and p != "www"]
                if len(labels) >= 2 and labels[0] not in _VANITY_STOP:
                    company = labels[0]
        token = _clean_token(company)
        if token:
            return AtsRef("greenhouse", token, job_id, confident=False)

    return None


def _norm(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def identity_id(url: str, *, company: str | None = None,
                title: str | None = None, location: str | None = None) -> str:
    """Stable cross-source identity for a job posting.

    ATS-parseable -> 'ats:token:job_id' (or 'ats:token' when job_id unknown).
    Otherwise -> 'norm:<sha1 of normalized company|title|location>'.
    Keys money/reputation records (decisions, receipts, submission ledger,
    already-applied block) so aliases and reposts fold correctly."""
    ref = parse_ats_url(url)
    if ref and ref.token:
        return f"{ref.ats}:{ref.token}:{ref.job_id}" if ref.job_id else f"{ref.ats}:{ref.token}"
    payload = "|".join((_norm(company), _norm(title), _norm(location)))
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"norm:{digest}"
