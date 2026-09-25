"""Post-hoc fabrication check for drafted answers (tier 6).

The draft tier's grounding was, until this module, only a *prompt instruction*:
the system prompt tells the model to use nothing beyond `resume_facts`, and
nothing verified that it complied. A model that ignores the instruction could
invent an employer under the applicant's real name, and the blue DRAFT badge
plus the operator's attention were the only backstop.

That makes the safety property "the operator will actually read it", which is
not something code guarantees. This module adds the check that is guaranteed.

Scope, deliberately narrow: it looks ONLY for **first-person employment
claims** — "I worked at X", "my time at X", "during my tenure at X" — and
verifies X is somewhere in the applicant's real history. It does NOT police
company names in general, because mentioning the employer you are applying to
("I admire how Tessera approaches design") is normal, truthful, and desirable
in these answers.

That narrowness is the point. A check that fires on every capitalised word
would be turned off within a day; one that fires only on invented employment
is worth obeying. An unfilled box is strictly better than a fabricated
employment claim under a real person's name.
"""
from __future__ import annotations

import re

# "…at Acme", "…for Globex Inc", "…with Initech" — the connector words that
# precede an organisation in an employment claim.
_CONNECTOR = r"(?:at|for|with|by|joined|from)"

# First-person employment assertions. Each must place the APPLICANT inside the
# organisation; a bare mention of a company is not matched on purpose.
# An organisation name: capitalised, up to four words. Written explicitly as
# [A-Z] rather than relying on re.IGNORECASE, because case IS the signal that
# distinguishes "Stripe" from "stripe" — switching the whole pattern to
# case-insensitive would make every lowercase word look like a company.
_ORG = r"(?P<org>[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})"

# Sentence-initial words are matched via explicit character classes for the
# same reason: only the first letter's case is optional.
_CLAIM_PATTERNS: tuple[re.Pattern, ...] = (
    # "I worked at X", "I have interned for X"
    re.compile(rf"\bI\s+(?:have\s+)?(?:work(?:ed|ing)?|intern(?:ed|ing)?|"
               rf"serv(?:ed|ing)|led|managed|built|design(?:ed)?)\s+"
               rf"(?:\w+\s+){{0,3}}?{_CONNECTOR}\s+{_ORG}"),
    # "My tenure at X", "our years with X"
    re.compile(rf"\b(?:[Mm]y|[Oo]ur)\s+(?:time|tenure|role|position|job|years?|"
               rf"experience|work)\s+(?:\w+\s+){{0,2}}?{_CONNECTOR}\s+{_ORG}"),
    # "I was a designer at X"
    re.compile(rf"\bI\s+(?:was|am)\s+(?:a|an|the)?\s*(?:\w+\s+){{0,3}}?"
               rf"(?:at|for)\s+{_ORG}"),
    # "employed by X", "hired at X"
    re.compile(rf"\b(?:employed|hired)\s+(?:by|at)\s+{_ORG}"),
    # "I joined X", "I left X" — the organisation is the direct object, so
    # there is no connector word for the patterns above to hang on.
    re.compile(rf"\bI\s+(?:joined|left|founded|co-founded)\s+{_ORG}"),
    # Cover-letter shapes: "At X, I redesigned ...", "At X we shipped ..."
    re.compile(rf"(?:^|[.!?]\s+|\n\s*)[Aa]t\s+{_ORG},?\s+(?:I|we|my)\b"),
    # "As a designer at X, I ..."
    re.compile(rf"\b[Aa]s\s+(?:a|an|the)\s+(?:[\w-]+\s+){{0,4}}?(?:at|for|with)\s+{_ORG}"),
    # "X, where I led ..."
    re.compile(rf"{_ORG},\s+where\s+I\b"),
    # "I spent four years at X"
    re.compile(rf"\bI\s+spent\s+(?:\w+\s+){{0,4}}?(?:at|with)\s+{_ORG}"),
)

# Capitalised words that start a sentence or are ordinary English — never an
# organisation, and matching them would produce noise that discredits the check.
_STOPWORDS = {
    "i", "the", "a", "an", "my", "our", "we", "they", "it", "this", "that",
    "there", "here", "when", "while", "after", "before", "during", "since",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}


def _norm(s: str) -> str:
    """Compare loosely: 'Acme Corp.' and 'acme corp' are the same employer."""
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|gmbh|plc)\b", " ",
               (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def known_organisations(profile: dict, target_company: str = "") -> set[str]:
    """Every organisation this applicant may truthfully claim to have been in,
    plus the company being applied to (legitimate to name in an answer)."""
    p = profile or {}
    facts = p.get("resume_facts", {}) or {}
    names: list[str] = []
    names += list(facts.get("preserved_companies", []) or [])
    names += list(facts.get("preserved_projects", []) or [])
    school = facts.get("preserved_school", "")
    if school:
        names.append(school)
    exp = p.get("experience", {}) or {}
    if exp.get("current_company"):
        names.append(exp["current_company"])
    for row in (p.get("work_history", []) or []):
        if isinstance(row, dict) and row.get("company"):
            names.append(row["company"])
    for row in (p.get("education", []) or []):
        if isinstance(row, dict) and row.get("school"):
            names.append(row["school"])
    if target_company:
        names.append(target_company)
    return {n for n in (_norm(x) for x in names) if n}


def find_unsupported_claims(text: str, profile: dict,
                            target_company: str = "") -> list[str]:
    """Organisations the text claims the applicant was part of, but which do
    not appear anywhere in their real history. Empty list == nothing to flag."""
    if not text:
        return []
    known = known_organisations(profile, target_company)
    flagged: list[str] = []
    for pattern in _CLAIM_PATTERNS:
        for m in pattern.finditer(text):
            org = (m.group("org") or "").strip(" .,;:")
            n = _norm(org)
            if not n or n in _STOPWORDS or org.lower() in _STOPWORDS:
                continue
            # Substring either way: "Acme" should satisfy a claim about
            # "Acme Corporation" and vice versa.
            if any(n == k or n in k or k in n for k in known):
                continue
            if org not in flagged:
                flagged.append(org)
    return flagged


def is_grounded(text: str, profile: dict, target_company: str = "") -> bool:
    return not find_unsupported_claims(text, profile, target_company)
