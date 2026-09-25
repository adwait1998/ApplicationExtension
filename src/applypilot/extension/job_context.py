"""Which job is this application for?

Title, company and description of the posting behind the page being filled,
for the things that need it (a cover letter today). Tried in order:

1. the operator's own jobs DB (read-only) — the pipeline already scraped
   the full description of every job it discovered;
2. the ATS's public job API for the posting in the URL (Greenhouse,
   Lever, Ashby, Workday) — plain GETs of public data;
3. the page's own visible text, sent by the extension — the last resort,
   capped, and marked as such.

Nothing here writes anywhere, and no application data is sent: the only
outbound requests are GETs of the public posting itself.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

MAX_DESCRIPTION_CHARS = 8000
_UA = "Mozilla/5.0 (ApplyPilot Copilot; local)"


def _fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _strip_html(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup or "")
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def parse_ats_url(url: str) -> dict | None:
    """{"ats", "slug", "job_id", ...} for a recognisable posting URL, else None."""
    try:
        u = urllib.parse.urlparse(url or "")
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    query = urllib.parse.parse_qs(u.query)
    if host.endswith("greenhouse.io"):
        if parts[:2] == ["embed", "job_app"]:
            slug = (query.get("for") or [""])[0]
            job_id = (query.get("token") or query.get("gh_jid") or [""])[0]
            if slug and job_id.isdigit():
                return {"ats": "greenhouse", "slug": slug, "job_id": job_id}
            return None
        if len(parts) >= 3 and parts[1] == "jobs" and parts[2].isdigit():
            return {"ats": "greenhouse", "slug": parts[0], "job_id": parts[2]}
        return None
    if host == "jobs.lever.co" and len(parts) >= 2:
        return {"ats": "lever", "slug": parts[0], "job_id": parts[1]}
    if host == "jobs.ashbyhq.com" and len(parts) >= 2:
        return {"ats": "ashby", "slug": parts[0], "job_id": parts[1]}
    if host.endswith("myworkdayjobs.com") and "job" in parts:
        i = parts.index("job")
        site_parts = parts[:i]
        if site_parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", site_parts[0]):
            site_parts = site_parts[1:]            # locale prefix: /en-US/
        rest = [p for p in parts[i + 1:] if p not in ("apply", "applyManually", "autofillWithResume")]
        if site_parts and rest:
            return {"ats": "workday", "slug": host.split(".")[0], "site": site_parts[-1],
                    "host": host, "job_path": "/".join(rest[:2])}
    return None


def job_from_api(url: str, fetch: Callable[[str], object] = _fetch_json) -> dict | None:
    p = parse_ats_url(url)
    if not p:
        return None
    try:
        if p["ats"] == "greenhouse":
            d = fetch(f"https://boards-api.greenhouse.io/v1/boards/{p['slug']}/jobs/{p['job_id']}")
            return {"title": d.get("title", ""), "company": d.get("company_name") or p["slug"],
                    "description": _strip_html(html_mod.unescape(d.get("content", ""))),
                    "source": "greenhouse api"}
        if p["ats"] == "lever":
            d = fetch(f"https://api.lever.co/v0/postings/{p['slug']}/{p['job_id']}")
            lists = "\n".join(f"{x.get('text', '')}\n{_strip_html(x.get('content', ''))}"
                              for x in d.get("lists", []) or [])
            desc = "\n\n".join(x for x in (d.get("descriptionPlain", ""), lists, d.get("additionalPlain", "")) if x)
            return {"title": d.get("text", ""), "company": p["slug"], "description": desc,
                    "source": "lever api"}
        if p["ats"] == "ashby":
            d = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{p['slug']}")
            for job in d.get("jobs", []) or []:
                if str(job.get("id", "")).lower() == p["job_id"].lower():
                    desc = job.get("descriptionPlain") or _strip_html(job.get("descriptionHtml", ""))
                    return {"title": job.get("title", ""), "company": p["slug"], "description": desc,
                            "source": "ashby api"}
            return None
        if p["ats"] == "workday":
            d = fetch(f"https://{p['host']}/wday/cxs/{p['slug']}/{p['site']}/job/{p['job_path']}")
            info = d.get("jobPostingInfo", {}) or {}
            org = (d.get("hiringOrganization", {}) or {}).get("name") or p["slug"]
            return {"title": info.get("title", ""), "company": org,
                    "description": _strip_html(info.get("jobDescription", "")), "source": "workday api"}
    except Exception:
        return None
    return None


def _norm_url(url: str) -> str:
    u = (url or "").strip().lower().split("#", 1)[0]
    u = re.sub(r"/(apply|application)/?(\?.*)?$", "", u)
    u = re.sub(r"\?.*$", "", u) if "gh_jid=" not in u else u
    return u.rstrip("/")


def job_from_db(url: str, db_path: str | Path | None) -> dict | None:
    if not db_path or not Path(db_path).exists():
        return None
    target = _norm_url(url)
    if not target:
        return None
    try:
        con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select url, application_url, title, site, coalesce(full_description, description, '') "
                "from jobs where application_url like ? or url like ?",
                (f"%{target.split('//', 1)[-1][:120]}%", f"%{target.split('//', 1)[-1][:120]}%"),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return None
    for job_url, app_url, title, site, desc in rows:
        if target in (_norm_url(job_url), _norm_url(app_url)) and desc:
            return {"title": title or "", "company": site or "", "description": desc, "source": "your jobs db"}
    return None


def job_context(urls: list[str], page_text: str = "", db_path: str | Path | None = None,
                fetch: Callable[[str], object] = _fetch_json) -> dict | None:
    """The posting's {title, company, description, source}, or None."""
    for url in urls:
        job = job_from_db(url, db_path)
        if job:
            break
    else:
        job = None
        for url in urls:
            job = job_from_api(url, fetch)
            if job and job.get("description"):
                break
    if not job and page_text and len(page_text.strip()) > 200:
        job = {"title": "", "company": "", "description": page_text.strip(), "source": "page text"}
    if job:
        job["description"] = (job.get("description") or "")[:MAX_DESCRIPTION_CHARS]
    return job
