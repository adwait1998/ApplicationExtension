"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Output 3 lines ONLY. No markdown, no analysis, no explanation, no thinking.

Rule: If the job's field differs from the candidate's primary career field, score is 1-2. Otherwise score by skill/seniority match (1=poor, 10=perfect).

CRITICAL — these are SEPARATE career fields, do NOT conflate them just because they share a word like "Product" or "Design":
- Product/UX/Interaction/Visual Designer = the candidate's field. Score on merit.
- Product Manager, Program Manager, Product Owner, Engineering Manager = a DIFFERENT field (product strategy/delivery, not design execution). Score 1-2 even though the title contains "Product".
- Researcher / Research Scientist / Data Scientist / ML Engineer = a DIFFERENT field. Score 1-2.
- Content Designer / UX Writer / Content Strategist = adjacent writing field, NOT visual/product design. Score at most 4 unless the JD is explicitly a hybrid product-design role.
- People-MANAGEMENT design roles (Design Manager, Product Design Manager, Director of Design, Head of Design, VP Design, Creative Director) = a DIFFERENT (management) job. The candidate is a 5-year individual contributor, NOT a manager. Score 1-2 even though the title contains "Design".
- Internship / Fellowship / Apprenticeship / New-Grad / Co-op / Student / Trainee roles = early-career, a DIFFERENT (entry-level) job. The candidate has 5 years' professional experience. Score 1-2 even though the title contains "Design".
- "Design" in non-UX contexts (chip design, protein design, instructional design) = different field. Score 1-2.

Examples (Product Design candidate):
- "Senior Software Engineer" → 1
- "VP Medical Affairs" → 1
- "Staff Product Manager, Dashboard" → 1   (Product MANAGER ≠ Product Designer)
- "Senior Program Manager" → 1
- "Research Scientist, Alignment" → 1
- "Data Scientist, Growth" → 1
- "Content Designer" → 4   (writing-adjacent, not visual/product design)
- "Senior Product Designer" → 9
- "Staff Interaction Designer" → 9
- "UX Researcher" → 7   (research within UX is in-field)
- "Product Design Manager" → 2   (people-management, NOT an IC designer role)
- "Director of Design" → 1   (management/exec, not IC)
- "Design Lead" → 7   (senior IC track — fit; "lead" is not "manager")
- "UX Design Intern" → 1   (early-career, candidate has 5 yrs experience)
- "Design Fellow, Summer 2026" → 1   (fellowship = entry-level program)

Output format (3 lines, NO other text):
SCORE: <number 1-10>
KEYWORDS: <comma-separated terms from JD>
REASONING: <one short sentence>"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Tolerant of common formatting variations from local models:
      - Markdown bold (**SCORE**: 9, **Score:** 9)
      - Slash format (SCORE: 9/10, Score: 9 / 10)
      - Mixed case (Score, SCORE, score)
      - Stripped <think>...</think> blocks from reasoning models
    """
    # Strip <think>...</think> blocks (Qwen3, DeepSeek-R1, etc.)
    cleaned = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()

    score = 0
    keywords = ""
    reasoning = cleaned[:400]

    # Find score: match `SCORE: 9`, `**Score**: 9/10`, `Score - 9`, etc.
    score_match = re.search(
        r"\*{0,2}\bscore\*{0,2}\s*[:\-]\s*\*{0,2}(\d{1,2})",
        cleaned,
        re.IGNORECASE,
    )
    if score_match:
        try:
            score = max(1, min(10, int(score_match.group(1))))
        except ValueError:
            score = 0

    kw_match = re.search(
        r"\*{0,2}\bkeywords?\*{0,2}\s*[:\-]\s*(.+?)(?:\n\n|\n\*|\n#|\Z)",
        cleaned,
        re.IGNORECASE | re.DOTALL,
    )
    if kw_match:
        keywords = kw_match.group(1).strip()[:500]

    rsn_match = re.search(
        r"\*{0,2}\breasoning\*{0,2}\s*[:\-]\s*(.+?)(?:\n\n|\n\*|\n#|\Z)",
        cleaned,
        re.IGNORECASE | re.DOTALL,
    )
    if rsn_match:
        reasoning = rsn_match.group(1).strip()[:500]

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


# Title-based pre-filter: auto-score 1 for jobs that are clearly in a
# different field than the candidate's target role. Pulled from the user's
# profile.json `experience.target_role`. This skips the LLM entirely for
# obvious mismatches, saving 60-90s per job on local models.
# Pre-filter keywords. "match" terms are SPECIFIC role identifiers — generic
# words like "design" alone are too ambiguous (e.g. "Protein Design" is a
# pharma role, not a design role).
_FIELD_KEYWORDS = {
    "designer": {
        "match": (
            "designer", "ux ", " ux", "ui ", " ui", "ui/ux", "ux/ui",
            "user experience", "user research", "user researcher",
            "interaction design", "product design", "visual design",
            "graphic design", "service design", "design lead",
            "design system",
            # NOTE: "design manager"/"design director"/"creative director"
            # were REMOVED from match (iter-11 — they auto-preserved
            # management titles). The candidate is a 5-yr IC designer, not
            # a people-manager; those are handled by the management
            # override below, not preserved here.
        ),
        "exclude_titles": (
            "engineer", "scientist", "developer", "physician", "nurse",
            "pharmacist", "biologist", "chemist", "manufacturing",
            "quality control", "regulatory", "clinical", "medical affairs",
            "drug", "vaccine", "protein", "biomarker", "therapeutic",
            "calibration", "mass spec", "immuno", "pharmacovigilance",
            "controller", "accountant", "auditor", "treasury", "tax ",
            "finance", "financial", "analyst", "actuary",
            "legal counsel", "paralegal", "attorney", "lawyer",
            "salesforce admin", "warehouse", "trade operations",
            "recruiter", "machinist", "technician", "operator", "driver",
            "mechanic", "electrician", "trader", "broker",
            "corporate development", "commercial", "supply chain",
            "procurement", "logistics", "compliance", "audit",
            "co-op,", "co op,", "intern,", "internship,",
            # Adjacent-but-distinct career tracks. A Product/UX *Designer* is
            # NOT a Product Manager / Program Manager / Researcher / Data
            # Scientist. These dominated the score-7 tier (55% of it) because
            # the LLM treated "Product Manager" ≈ "Product Designer" on the
            # shared word "Product". The has_match guard below still protects
            # legit design roles ("Product Design Manager", "Design Manager",
            # "Manager, Product Design") because they contain a match term.
            "product manager", "program manager", "engineering manager",
            "product management", "program management", "product owner",
            "data scientist", "research scientist", "applied scientist",
            "data analyst", "business analyst", "growth manager",
            "marketing manager", "account executive", "sales ",
            "solutions architect", "customer success",
            # "researcher"/"product lead"/"production" are safe to exclude
            # because the has_match guard preserves true design titles:
            # "UX Researcher"/"User Researcher" match via "ux "/"user
            # research"; "Design Lead" matches via "design lead". Only
            # off-track variants ("Researcher, Alignment", "Product Lead,
            # AI", "Production Lead") have no design match term → filtered.
            "researcher", "research engineer", "research lead",
            "product lead", "production", "gtm ", "go-to-market",
            "operations manager", "project manager", "consultant",
        ),
    },
    "engineer": {
        "match": ("engineer", "developer", "swe", "sre", "devops", "platform"),
        "exclude_titles": (
            "designer", "physician", "nurse", "pharmacist",
            "manufacturing engineer", "regulatory", "clinical",
            "medical affairs", "vaccine", "controller", "accountant",
            "salesperson", "recruiter", "marketing manager", "sales engineer",
        ),
    },
}


# People-management / over-leveled markers. The candidate is a 5-year IC
# Product/UX Designer — a Design Manager / Director of Design / Head of
# Design / VP Design / Creative Director is a DIFFERENT (management) job,
# not a fit, and applying wastes attempts + mismatches the recruiter.
# These override the design match (a title can be both "Product Design"
# AND "Manager"; for an IC target, management wins → reject).
_DESIGN_MGMT_MARKERS = (
    "manager", "director", "head of", "vice president", " vp ", "vp,",
    "vp of", "chief ", "people lead",
)

# Early-career / training roles. The candidate has 5 years of professional
# experience — an internship / fellowship / apprenticeship / new-grad /
# co-op / student role is a DIFFERENT (entry-level, often unpaid or
# stipend) job, not a fit, and applying wastes a live attempt. Word-
# boundary regex so "internal" / "international" do NOT false-positive.
_EARLY_CAREER_RE = re.compile(
    r"\b(intern|interns|internship|internships|fellow|fellows|fellowship"
    r"|fellowships|apprentice|apprenticeship|trainee|co-?op|new[\s-]?grad"
    r"|new[\s-]?graduate|early[\s-]?career|student)\b",
    re.IGNORECASE,
)


def _prefilter_score(target_role: str, title: str) -> dict | None:
    """Return a forced score=1 result if the title clearly mismatches target_role.

    Returns None if no clear mismatch (LLM should evaluate).
    """
    if not target_role or not title:
        return None
    role_lower = target_role.lower()
    title_lower = title.lower()

    # Find which field bucket applies based on target_role
    bucket = None
    bucket_key = None
    for key, cfg in _FIELD_KEYWORDS.items():
        if any(m in role_lower for m in cfg["match"]):
            bucket = cfg
            bucket_key = key
            break
    if not bucket:
        return None

    # Management-level override (IC designer target only): reject people-
    # management / exec design titles even when "design" is present.
    if bucket_key == "designer" and any(
            mk in title_lower for mk in _DESIGN_MGMT_MARKERS):
        return {
            "score": 1,
            "keywords": "",
            "reasoning": (f"Pre-filter: '{title}' is a people-management / "
                          f"over-leveled role; candidate is an IC designer."),
        }

    # Early-career override (any field): the candidate is a 5-year
    # professional, so intern/fellow/apprentice/new-grad/student roles are
    # off-target even when they say "design" — they waste live attempts.
    if _EARLY_CAREER_RE.search(title):
        return {
            "score": 1,
            "keywords": "",
            "reasoning": (f"Pre-filter: '{title}' is an early-career / "
                          f"internship-fellowship role; candidate has 5 "
                          f"years' professional experience."),
        }

    # If title contains an excluded keyword AND no matching keyword, auto-fail
    has_exclusion = any(x in title_lower for x in bucket["exclude_titles"])
    has_match = any(m in title_lower for m in bucket["match"])
    if has_exclusion and not has_match:
        return {
            "score": 1,
            "keywords": "",
            "reasoning": f"Pre-filter: title '{title}' is in a different field than target role '{target_role}'.",
        }
    return None


def score_job(resume_text: str, job: dict, target_role: str = "") -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.
        target_role: Candidate's target role from profile (used for pre-filter).

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    pre = _prefilter_score(target_role, job.get("title", ""))
    if pre is not None:
        return pre

    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:2000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=4096, temperature=0.2)
        return _parse_score_response(response)
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    profile = load_profile()
    target_role = profile.get("experience", {}).get("target_role", "") if profile else ""
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    for job in jobs:
        result = score_job(resume_text, job, target_role=target_role)
        result["url"] = job["url"]
        completed += 1

        if result["score"] == 0:
            errors += 1

        results.append(result)

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

    # Write scores to DB
    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (r["score"], f"{r['keywords']}\n{r['reasoning']}", now, r["url"]),
        )
    conn.commit()

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
