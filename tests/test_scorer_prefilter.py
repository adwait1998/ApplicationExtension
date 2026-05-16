"""Regression: the title prefilter must reject adjacent-but-distinct career
tracks (Product Manager / Program Manager / Researcher / Data Scientist) for
a Product/UX Designer candidate, while preserving genuine design roles.

Context: 55% of the score-7 tier was Product/Program Managers because the
LLM treated "Product Manager" ≈ "Product Designer" on the shared word
"Product". The prefilter now hard-fails those before the LLM sees them.
The has_match guard must still protect design-management titles.
"""
from __future__ import annotations

import pytest

from applypilot.scoring.scorer import _prefilter_score

TARGET = "Product Designer"


@pytest.mark.parametrize("title", [
    "Staff Product Manager, Dashboard",
    "Senior Product Manager, ML Signals",
    "Senior Product Manager, Agent Context",
    "Program Manager - Technology Capital Builds",
    "GTM Growth Product Manager, Agentic Systems",
    "Group Product Manager",
    "Product Owner, Payments",
    "Research Scientist, Alignment Oversight",
    "Applied Scientist, Ranking",
    "Data Scientist, Growth",
    "Engineering Manager, Platform",
    "Business Analyst, Operations",
    "Account Executive, Enterprise",
    # AI/ML "Researcher" (not UX research), PM-track "Product Lead",
    # "Production Lead", project/ops management — all off-track for a
    # Product Designer. The has_match guard still protects UX Researcher /
    # User Researcher / Design Lead (see preserve test below).
    "Researcher, Alignment",
    "Researcher, Training",
    "Research Engineer, Safety",
    "Product Lead, AI",
    "Product Lead, Growth Marketing",
    "Production Lead",
    "Project Manager, Engineering",
    "GTM Strategy Manager",
    "Operations Manager, Studio",
    "Principal Consultant",
    # iter-11: people-management / over-leveled DESIGN roles — the
    # candidate is a 5-yr IC, not a manager. These waste applies.
    "Product Design Manager",
    "Design Manager",
    "Manager, Product Design",
    "Senior Product Design Manager, Payroll",
    "Director of Design",
    "Design Director",
    "Head of Design",
    "VP, Design",
    "VP of Product Design",
    "Creative Director",
    "Sr. Manager, UX Design, Prime Video",
    "Director, Experience Design",
    # iter-12: early-career / training roles — candidate has 5 yrs
    # professional experience. Internship/fellowship waste live attempts.
    "UX Design Intern",
    "Product Design Intern, Summer 2026",
    "Design Internship - Fall",
    "Design Fellow",
    "Design Fellowship Program",
    "UX Apprentice",
    "Product Design Apprenticeship",
    "New Grad Product Designer",
    "New-Graduate UX Designer",
    "Early Career Designer",
    "Early-Career Product Designer",
    "Product Design Co-op",
    "UX Design Coop, 2026",
    "Student Product Designer",
    "Design Trainee",
])
def test_prefilter_rejects_off_track_roles(title):
    r = _prefilter_score(TARGET, title)
    assert r is not None, f"{title!r} should be hard-filtered"
    assert r["score"] == 1


@pytest.mark.parametrize("title", [
    "Senior Product Designer",
    "Product Designer, AI Models",
    "Staff Interaction Designer",
    "Principal Product Designer",       # Principal = senior IC, not mgmt
    "UX Researcher",                    # research within UX is in-field
    "User Researcher, Design Systems",  # "researcher" excluded, "user research" guards it
    "Senior UX Designer",
    "Design Lead, Growth",             # "lead" = senior IC track, NOT manager
    "Lead Product Designer",
    # iter-12 false-positive guards: word-boundary regex must NOT treat
    # "internal" / "international" as "intern".
    "Internal Tools Product Designer",
    "International Product Designer",
    "Senior Internal Communications Designer",
])
def test_prefilter_preserves_design_roles(title):
    # None == not prefiltered → goes to the LLM for merit scoring
    assert _prefilter_score(TARGET, title) is None


def test_prefilter_noop_without_target_role():
    assert _prefilter_score("", "Staff Product Manager") is None
    assert _prefilter_score("Product Designer", "") is None
