"""Nightly canary parse (spec §6.8/§11): sample ~N live forms per supported ATS,
parse each through the front-end registry, report a per-ATS parse-rate + gaps.
Manual/cron-invoked, NO daemon (spec §14). Never submits — navigate + parse only.
Alarms on a parse-rate drop (DOM churn) before the queue feels it; gaps feed
same-day fixture promotion."""
from __future__ import annotations

from applypilot.apply.v2 import frontends

_STANDARD = {"email", "first_name", "last_name", "resume", "phone"}


def _parsed_ok(schema) -> bool:
    if schema is None:
        return False
    keys = {f.semantic_key for s in schema.steps for f in s.fields}
    return bool(keys & _STANDARD)


def score(results: list[dict]) -> dict:
    by_ats: dict[str, dict] = {}
    for r in results:
        d = by_ats.setdefault(r["ats"], {"n": 0, "parsed": 0, "gaps": 0})
        d["n"] += 1
        if r.get("error") is None and _parsed_ok(r.get("schema")):
            d["parsed"] += 1
        else:
            d["gaps"] += 1
    for d in by_ats.values():
        d["parse_rate"] = round(d["parsed"] / d["n"], 3) if d["n"] else 0.0
    return {"by_ats": by_ats}


def alarms(report: dict, *, min_parse_rate: float = 0.90) -> list[str]:
    return [ats for ats, d in report["by_ats"].items() if d["parse_rate"] < min_parse_rate]


def format_report(report: dict) -> str:
    lines = ["=" * 48, "  Canary parse — per-ATS parse rate", "=" * 48]
    for ats, d in sorted(report["by_ats"].items()):
        lines.append(f"  {ats:12s} {d['parsed']:>3}/{d['n']:<3} ({d['parse_rate']:.0%})  gaps={d['gaps']}")
    lines.append("=" * 48)
    return "\n".join(lines)


def sample_urls(conn, ats: str, *, limit: int = 20) -> list[str]:
    """Sample recent candidate URLs for `ats` from the jobs table (live runner)."""
    host = {"greenhouse": "greenhouse.io", "ashby": "jobs.ashbyhq.com", "lever": "jobs.lever.co"}[ats]
    base = ("SELECT application_url FROM jobs WHERE application_url LIKE ? "
            "AND application_url IS NOT NULL ")
    try:
        rows = conn.execute(
            base + "ORDER BY discovered_at DESC LIMIT ?",
            (f"%{host}%", limit)).fetchall()
    except Exception:                                # noqa: BLE001 — schema drift must not crash the canary
        rows = conn.execute(
            base + "ORDER BY rowid DESC LIMIT ?",
            (f"%{host}%", limit)).fetchall()
    return [r[0] for r in rows if r[0]]


def run_live(conn, *, atses=("greenhouse", "ashby", "lever"), limit: int = 20,
             headless: bool = True) -> dict:
    """Live runner: navigate + parse each sampled URL; collect (ats, schema, error).
    Uses a throwaway headless Chromium (out-of-band; never submits)."""
    from playwright.sync_api import sync_playwright
    from applypilot.apply.browser_stream import collect_browser_observation
    results = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=headless)
        for ats in atses:
            parse = frontends.parser_for(ats)
            if parse is None:
                continue
            for url in sample_urls(conn, ats, limit=limit):
                page = b.new_context().new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    obs = collect_browser_observation(page)
                    results.append({"ats": ats, "schema": parse(obs, company="canary", url=url),
                                    "error": None})
                except Exception as e:                   # noqa: BLE001 — a nav/parse failure IS a gap
                    results.append({"ats": ats, "schema": None, "error": str(e)})
                finally:
                    page.close()
        b.close()
    return score(results)
