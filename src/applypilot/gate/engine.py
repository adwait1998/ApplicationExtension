"""Compose the deterministic rules into one verdict per job. UNKNOWN never
becomes eligible. If ANY rule REJECTs -> ineligible (all evaluated rules are
still recorded). Run at ingest; re-runnable via gate_version bump."""
from __future__ import annotations

from datetime import datetime, timezone

from . import GATE_VERSION
from .rules import location_rule, seniority_rule, sponsorship_rule, automatability_rule
from applypilot.identity import identity_id, parse_ats_url


def gate_job(job: dict, profile: dict) -> dict:
    # Defensive coercion: raw discovery dicts can carry non-string values
    # (e.g. floats from API JSON / pandas NaN). One bad field must not raise.
    apply_url = str(job.get("application_url") or job.get("url") or "")
    title = str(job.get("title") or "")
    location = str(job.get("location") or "")
    desc = str(job.get("full_description") or job.get("description") or "")

    verdicts = {
        "automatability": automatability_rule(apply_url, workday_accounts=profile.get("workday_accounts", [])),
        "location": location_rule(location, profile.get("geo", {}), description=desc),
        "seniority": seniority_rule(title, profile.get("seniority", {})),
        "sponsorship": sponsorship_rule(desc, needs_sponsorship=profile.get("needs_sponsorship", False)),
    }
    reasons = [{"rule": name, "result": v.result, "code": v.code, "evidence": v.evidence}
               for name, v in verdicts.items()]

    if any(v.is_reject for v in verdicts.values()):
        result = "ineligible"
    elif any(v.result == "UNKNOWN" for v in verdicts.values()):
        result = "unknown"
    else:
        result = "eligible"

    automatability = {
        "manual_ats": "manual", "account_required": "account_required",
        "automatable_supported": "auto", "automatable_workday": "auto",
    }.get(verdicts["automatability"].code, "unknown")

    ref = parse_ats_url(apply_url)
    return {
        "identity_id": identity_id(apply_url, company=job.get("site"), title=title, location=location),
        "ats": ref.ats if ref else None,
        "board_token": ref.token if ref else None,
        "ats_job_id": ref.job_id if ref else None,
        "gate_result": result,
        "gate_reasons": reasons,          # caller json.dumps() before DB write
        "automatability": automatability,
        "gate_version": GATE_VERSION,
        "gated_at": datetime.now(timezone.utc).isoformat(),
    }
