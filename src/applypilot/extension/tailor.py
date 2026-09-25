"""A résumé tailored to the job on the page, for the applicant to review and
attach — the extension's front door to the pipeline's own tailoring
(scoring/tailor.py: structured JSON from the model, header and contact
details assembled by code, a programmatic validator, and an LLM judge that
checks the tailored text against the original for fabrication).

Only a version the judge passed is offered for attaching; a judge warning
is shown with it; anything that failed validation is refused with the
reasons. The base résumé is never modified. Files live in the active
profile's own directory under tailored/.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Callable

TAILORED_DIRNAME = "tailored"
OFFERABLE = ("approved", "approved_with_judge_warning")


class TailorError(Exception):
    """User-facing, safe to display."""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "job"


def tailored_id(job: dict, url: str) -> str:
    digest = hashlib.sha256((url or json.dumps(job, sort_keys=True)).encode("utf-8")).hexdigest()[:8]
    return f"{_slug(job.get('company', ''))}-{_slug(job.get('title', ''))}-{digest}"


def _default_pdf(text_path: Path) -> Path:
    from applypilot.scoring.pdf import convert_to_pdf

    return convert_to_pdf(text_path)


def tailor_for_page(profile: dict, job: dict | None, resume_text: str, url: str, out_dir: Path, *,
                    client, tailor_fn: Callable | None = None,
                    pdf_fn: Callable[[Path], Path] = _default_pdf) -> dict:
    if not job or not (job.get("description") or "").strip():
        raise TailorError("couldn't find this job's description to tailor against")
    if not (resume_text or "").strip():
        raise TailorError("no résumé text on file — upload your résumé in Settings first")
    if tailor_fn is None:
        from applypilot.scoring.tailor import tailor_resume as tailor_fn

    job_dict = {"title": job.get("title", ""), "site": job.get("company", ""), "location": "",
                "full_description": job["description"]}
    text, report = tailor_fn(resume_text, job_dict, profile, max_retries=2, validation_mode="normal",
                             client=client)
    status = report.get("status", "")
    validator = report.get("validator") or {}
    judge = report.get("judge") or {}
    if status not in OFFERABLE or not (text or "").strip():
        errors = "; ".join((validator.get("errors") or [])[:3]) or judge.get("issues") or status
        raise TailorError(f"the tailored version didn't pass the fabrication checks ({errors}) — "
                          "use your regular résumé for this one")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tid = tailored_id(job, url)
    text_path = out_dir / f"{tid}.txt"
    text_path.write_text(text, encoding="utf-8")
    pdf_path = Path(pdf_fn(text_path))
    meta = {"id": tid, "url": url, "title": job.get("title", ""), "company": job.get("company", ""),
            "status": status, "judge": {"verdict": judge.get("verdict", ""), "issues": judge.get("issues", "")},
            "warnings": list(validator.get("warnings") or []), "created": time.time(),
            "pdf": pdf_path.name}
    (out_dir / f"{tid}.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return {**meta, "text": text}


def find_tailored(out_dir: Path, tid: str) -> tuple[dict, Path] | None:
    if not re.fullmatch(r"[a-z0-9-]{1,100}", tid or ""):
        return None
    meta_path = Path(out_dir) / f"{tid}.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    pdf = Path(out_dir) / meta.get("pdf", "")
    return (meta, pdf) if pdf.is_file() else None
