"""Tests for applypilot.extension.resume_import and the résumé endpoints on
applypilot.extension.server (POST /profile/import-resume, GET /resume,
GET /resume/info).

$0, no network, no real files under E:\\applypilot-data: every disk-backed
test lives entirely under tmp_path, and every LLM call is injected (a
plain llm_fn for the module-level tests, a monkeypatched
applypilot.llm.get_client for the HTTP-level ones) -- this suite must
never invoke a real model or touch the network.
"""
from __future__ import annotations

import io
import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from applypilot.extension import resume_import  # noqa: E402
from applypilot.extension.server import create_app  # noqa: E402

# ---------------------------------------------------------------------------
# A tiny, real, extractable PDF -- built with pypdf itself (no reportlab/
# fpdf available in this environment), so PDF text extraction is exercised
# for real rather than mocked.
# ---------------------------------------------------------------------------


def _make_pdf_bytes(lines: list[str]) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)

    content_lines = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    first = True
    for line in lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        if first:
            content_lines.append(f"({escaped}) Tj")
            first = False
        else:
            content_lines.append(f"T* ({escaped}) Tj")
    content_lines.append("ET")
    content = "\n".join(content_lines).encode("latin-1")

    stream_obj = DecodedStreamObject()
    stream_obj.set_data(content)
    stream_ref = writer._add_object(stream_obj)

    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    font_ref = writer._add_object(font)

    resources = DictionaryObject()
    font_dict = DictionaryObject()
    font_dict[NameObject("/F1")] = font_ref
    resources[NameObject("/Font")] = font_dict

    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = stream_ref

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


RESUME_LINES = [
    "Jane Doe",
    "jane.doe@example.com | 555-123-4567",
    "linkedin.com/in/janedoe | github.com/janedoe",
    "Authorized to work in the US, no sponsorship required.",
]
RESUME_PDF_BYTES = _make_pdf_bytes(RESUME_LINES)
RESUME_TXT = "\n".join(RESUME_LINES)


VALID_LLM_JSON = json.dumps(
    {
        "work_history": [
            {
                "title": "Senior Product Designer",
                "company": "Acme",
                "location": "Seattle, WA",
                "start": "03/2022",
                "end": "",
                "current": True,
                "description": "Led design for the core product.",
            },
            {
                "title": "Product Designer",
                "company": "Globex",
                "location": "Portland, OR",
                "start": "06/2018",
                "end": "02/2022",
                "current": False,
                "description": "Owned onboarding flows.",
            },
        ],
        "education": [
            {
                "school": "State University",
                "degree": "Bachelor's Degree",
                "field": "Computer Science",
                "start": "08/2014",
                "end": "05/2018",
            }
        ],
        "current_title": "Senior Product Designer",
        "total_years_experience": "6",
    }
)


def _fake_llm_fn(_text: str) -> str:
    return VALID_LLM_JSON


# ---------------------------------------------------------------------------
# validate_upload: bounds + extension allowlist
# ---------------------------------------------------------------------------


def test_validate_upload_rejects_empty():
    with pytest.raises(resume_import.ResumeImportError):
        resume_import.validate_upload("resume.pdf", b"")


def test_validate_upload_rejects_oversized():
    data = b"x" * (resume_import.MAX_UPLOAD_BYTES + 1)
    with pytest.raises(resume_import.ResumeImportError):
        resume_import.validate_upload("resume.txt", data)


def test_validate_upload_rejects_unknown_extension():
    with pytest.raises(resume_import.ResumeImportError):
        resume_import.validate_upload("resume.exe", b"whatever")


def test_validate_upload_rejects_no_extension():
    with pytest.raises(resume_import.ResumeImportError):
        resume_import.validate_upload("resume", b"whatever")


@pytest.mark.parametrize("name,ext", [("resume.pdf", ".pdf"), ("Resume.DOCX", ".docx"), ("r.txt", ".txt")])
def test_validate_upload_accepts_known_extensions(name, ext):
    assert resume_import.validate_upload(name, b"some bytes") == ext


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def test_extract_text_txt_decodes_utf8():
    text = resume_import.extract_text("Jane Doe\njane@example.com".encode("utf-8"), ".txt")
    assert "Jane Doe" in text
    assert "jane@example.com" in text


def test_extract_text_txt_empty_raises():
    with pytest.raises(resume_import.ResumeImportError):
        resume_import.extract_text(b"   \n\n  ", ".txt")


def test_extract_text_pdf_extracts_real_text():
    text = resume_import.extract_text(RESUME_PDF_BYTES, ".pdf")
    assert "Jane Doe" in text
    assert "jane.doe@example.com" in text
    assert "555-123-4567" in text


def test_extract_text_pdf_corrupt_raises_clear_error_not_crash():
    with pytest.raises(resume_import.ResumeImportError):
        resume_import.extract_text(b"this is not a pdf at all", ".pdf")


def test_extract_text_docx_missing_dependency_degrades_with_clear_message():
    """python-docx is deliberately NOT installed in this environment (task
    spec: lazily imported, not a hard dependency). Importing it here must
    raise a clear, user-facing ResumeImportError, never an ImportError
    escaping to the caller."""
    with pytest.raises(resume_import.ResumeImportError, match="python-docx"):
        resume_import.extract_text(b"whatever bytes", ".docx")


# ---------------------------------------------------------------------------
# Deterministic pass: email, phone, LinkedIn/GitHub, name -- no model
# ---------------------------------------------------------------------------


def test_deterministic_extract_finds_contact_fields():
    det = resume_import.deterministic_extract(RESUME_TXT)
    assert det["personal.full_name"] == "Jane Doe"
    assert det["personal.email"] == "jane.doe@example.com"
    assert "555-123-4567" in det["personal.phone"]
    assert "linkedin.com/in/janedoe" in det["personal.linkedin_url"]
    assert "github.com/janedoe" in det["personal.github_url"]


def test_deterministic_extract_omits_absent_fields():
    det = resume_import.deterministic_extract("Just some plain text with no contact info at all.")
    assert "personal.email" not in det
    assert "personal.phone" not in det
    assert "personal.linkedin_url" not in det
    assert "personal.github_url" not in det


def test_deterministic_extract_portfolio_prefers_non_linkedin_github():
    text = "Jane Doe\nhttps://linkedin.com/in/janedoe\nhttps://janedoe.design\njane@example.com"
    det = resume_import.deterministic_extract(text)
    assert det["personal.portfolio_url"] == "https://janedoe.design"


# ---------------------------------------------------------------------------
# LLM JSON validation: strict parsing, malformed/hostile output never raises
# ---------------------------------------------------------------------------


def test_parse_llm_json_valid_round_trips():
    fields, warnings = resume_import.parse_llm_json(VALID_LLM_JSON)
    assert warnings == []
    assert len(fields["work_history"]) == 2
    assert fields["work_history"][0]["company"] == "Acme"
    assert fields["work_history"][0]["current"] is True
    assert fields["education"][0]["school"] == "State University"
    assert fields["current_title"] == "Senior Product Designer"
    assert fields["total_years_experience"] == "6"


def test_parse_llm_json_strips_markdown_fence():
    wrapped = f"```json\n{VALID_LLM_JSON}\n```"
    fields, warnings = resume_import.parse_llm_json(wrapped)
    assert warnings == []
    assert fields["current_title"] == "Senior Product Designer"


def test_parse_llm_json_recovers_object_from_surrounding_prose():
    wrapped = f"Sure, here is the extracted data:\n{VALID_LLM_JSON}\nLet me know if you need anything else!"
    fields, _warnings = resume_import.parse_llm_json(wrapped)
    assert fields["current_title"] == "Senior Product Designer"


@pytest.mark.parametrize(
    "garbage",
    [
        "I'm sorry, I can't help with that.",
        "",
        "```json\n{not valid json\n```",
        "[1, 2, 3]",
        "null",
    ],
)
def test_parse_llm_json_malformed_or_hostile_output_never_raises(garbage):
    fields, warnings = resume_import.parse_llm_json(garbage)
    assert fields == {}
    assert warnings, "expected a warning explaining manual entry is needed"


def test_parse_llm_json_drops_non_dict_work_history_entries():
    raw = json.dumps({"work_history": ["not a dict", 42, None], "education": []})
    fields, _warnings = resume_import.parse_llm_json(raw)
    assert "work_history" not in fields  # every entry was invalid -> nothing survives


def test_parse_llm_json_drops_entries_missing_identifying_info():
    raw = json.dumps({"work_history": [{"title": "", "company": "", "description": "no title or company"}]})
    fields, _warnings = resume_import.parse_llm_json(raw)
    assert "work_history" not in fields


def test_parse_llm_json_ignores_unknown_keys():
    raw = json.dumps({"current_title": "Engineer", "some_field_the_model_invented": "ignored"})
    fields, _warnings = resume_import.parse_llm_json(raw)
    assert fields == {"current_title": "Engineer"}
    assert "some_field_the_model_invented" not in fields


# ---------------------------------------------------------------------------
# Bug 5: work_history descriptions are normalised to bullet lines, never
# stored as a single prose paragraph. Nothing is invented -- only
# re-punctuated/re-segmented.
# ---------------------------------------------------------------------------


def test_normalize_description_preserves_existing_bullets_verbatim():
    raw = "- Led design for the core product.\n- Owned onboarding flows end to end."
    assert resume_import._normalize_description(raw) == raw


def test_normalize_description_uniforms_varied_bullet_markers():
    raw = "* Shipped the redesign\n• Ran user research\n1. Mentored two designers"
    assert resume_import._normalize_description(raw) == (
        "- Shipped the redesign\n- Ran user research\n- Mentored two designers"
    )


def test_normalize_description_splits_a_paragraph_into_sentence_bullets():
    raw = ("Led the redesign of onboarding. Reduced drop-off by 30%. "
           "Partnered closely with engineering.")
    out = resume_import._normalize_description(raw)
    assert out == (
        "- Led the redesign of onboarding.\n"
        "- Reduced drop-off by 30%.\n"
        "- Partnered closely with engineering."
    )
    # nothing invented -- every word of the original survives somewhere
    for word in ("redesign", "30%", "engineering"):
        assert word in out


def test_normalize_description_single_sentence_becomes_one_bullet():
    assert resume_import._normalize_description("Led design for the core product.") == \
        "- Led design for the core product."


def test_normalize_description_empty_stays_empty():
    assert resume_import._normalize_description("") == ""
    assert resume_import._normalize_description("   ") == ""


def test_parse_llm_json_normalizes_paragraph_description_instead_of_rejecting():
    # If the model returns a paragraph despite the prompt, the import is
    # NOT rejected -- the paragraph is converted to sentence bullets.
    raw = json.dumps({"work_history": [{
        "title": "Designer", "company": "Acme",
        "description": "Led the redesign of onboarding. Reduced drop-off by 30%.",
    }]})
    fields, warnings = resume_import.parse_llm_json(raw)
    assert warnings == []
    desc = fields["work_history"][0]["description"]
    assert desc == "- Led the redesign of onboarding.\n- Reduced drop-off by 30%."


def test_parse_llm_json_keeps_existing_bullets_from_the_model():
    raw = json.dumps({"work_history": [{
        "title": "Designer", "company": "Acme",
        "description": "- First bullet\n- Second bullet",
    }]})
    fields, _warnings = resume_import.parse_llm_json(raw)
    assert fields["work_history"][0]["description"] == "- First bullet\n- Second bullet"


# ---------------------------------------------------------------------------
# GPA: a deterministic, high-precision regex pass (no model needed), strict
# validation, and correct attachment to the right degree when there is more
# than one.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("GPA: 3.8/4.0", "3.8/4.0"),
        ("GPA 3.8", "3.8"),
        ("3.8/4.0 GPA", "3.8/4.0"),
        ("CGPA 8.9/10", "8.9/10"),
        ("Cumulative GPA: 3.85", "3.85"),
    ],
)
def test_find_gpa_mentions_recognizes_every_documented_format(text, expected):
    mentions = resume_import._find_gpa_mentions(text)
    assert len(mentions) == 1
    line_idx, raw = mentions[0]
    assert line_idx == 0
    assert resume_import._validate_gpa(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3.8", "3.8"),
        ("3.8/4.0", "3.8/4.0"),
        ("8.9/10", "8.9/10"),
        ("4.2/5", "4.2/5"),
    ],
)
def test_validate_gpa_accepts_plausible_values(raw, expected):
    assert resume_import._validate_gpa(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "9.9",          # no scale given -> default 4.0, way over
        "4.5",          # default scale 4.0, just over
        "3.8/6.0",      # scale not one of the honoured 4/5/10-point scales
        "11/10",        # value exceeds its own stated scale
        "-1",           # not a plausible GPA at all
        "not a gpa",
        "",
    ],
)
def test_validate_gpa_rejects_implausible_values(raw):
    assert resume_import._validate_gpa(raw) is None


def test_gpa_out_of_scale_is_rejected_not_stored_as_garbage():
    """An LLM-proposed GPA outside its scale must be rejected -- stored as
    "" (same as "not stated"), never surfaced as a fact about the applicant."""
    raw = json.dumps({"education": [{"school": "State University", "degree": "B.S.", "gpa": "9.9"}]})
    fields, _warnings = resume_import.parse_llm_json(raw)
    assert fields["education"][0]["gpa"] == ""


def test_gpa_valid_llm_value_is_kept():
    raw = json.dumps({"education": [{"school": "State University", "degree": "B.S.", "gpa": "3.8/4.0"}]})
    fields, _warnings = resume_import.parse_llm_json(raw)
    assert fields["education"][0]["gpa"] == "3.8/4.0"


def test_attach_gpa_single_degree_is_unambiguous_regardless_of_position():
    text = "Jane Doe\nGPA: 3.9/4.0\nEDUCATION\nState University, B.S. Computer Science"
    entries = [{"school": "State University", "degree": "B.S. Computer Science", "gpa": ""}]
    warnings = resume_import._attach_gpa_to_education(text, entries)
    assert entries[0]["gpa"] == "3.9/4.0"
    assert warnings == []


def test_attach_gpa_two_degrees_attaches_to_the_one_it_appears_beside():
    text = (
        "Jane Doe\n"
        "EDUCATION\n"
        "State University, M.S. Computer Science, 2018-2020\n"
        "GPA: 3.9/4.0\n"
        "\n"
        "Old College, B.S. Computer Science, 2012-2016\n"
    )
    entries = [
        {"school": "State University", "degree": "M.S. Computer Science", "gpa": ""},
        {"school": "Old College", "degree": "B.S. Computer Science", "gpa": ""},
    ]
    warnings = resume_import._attach_gpa_to_education(text, entries)
    assert entries[0]["gpa"] == "3.9/4.0"
    assert entries[1]["gpa"] == ""  # never guessed onto the other degree
    assert warnings == []


def test_attach_gpa_ambiguous_position_attaches_to_neither_and_warns():
    # The GPA appears before either degree's own text -- cannot tell which
    # one it belongs to.
    text = (
        "Jane Doe\n"
        "GPA: 3.9/4.0\n"
        "EDUCATION\n"
        "State University, M.S. Computer Science, 2018-2020\n"
        "Old College, B.S. Computer Science, 2012-2016\n"
    )
    entries = [
        {"school": "State University", "degree": "M.S. Computer Science", "gpa": ""},
        {"school": "Old College", "degree": "B.S. Computer Science", "gpa": ""},
    ]
    warnings = resume_import._attach_gpa_to_education(text, entries)
    assert entries[0]["gpa"] == ""
    assert entries[1]["gpa"] == ""
    assert warnings, "expected a warning that the GPA could not be confidently placed"


def test_attach_gpa_no_mentions_leaves_entries_untouched():
    entries = [{"school": "State University", "degree": "B.S.", "gpa": ""}]
    warnings = resume_import._attach_gpa_to_education("no gpa mentioned anywhere", entries)
    assert entries[0]["gpa"] == ""
    assert warnings == []


def test_attach_gpa_no_entries_is_a_no_op():
    assert resume_import._attach_gpa_to_education("GPA: 3.9/4.0", []) == []


def test_import_resume_end_to_end_attaches_gpa_to_correct_degree(tmp_path):
    text = (
        "Jane Doe\n"
        "jane.doe@example.com\n"
        "EDUCATION\n"
        "State University, M.S. Computer Science, 2018-2020\n"
        "GPA: 3.9/4.0\n"
        "\n"
        "Old College, B.S. Computer Science, 2012-2016\n"
    )
    llm_json = json.dumps({
        "education": [
            {"school": "State University", "degree": "M.S. Computer Science", "field": "",
             "start": "08/2018", "end": "05/2020"},
            {"school": "Old College", "degree": "B.S. Computer Science", "field": "",
             "start": "08/2012", "end": "05/2016"},
        ],
    })
    result = resume_import.import_resume(
        filename="resume.txt",
        data=text.encode("utf-8"),
        existing_profile={},
        profile_dir=tmp_path,
        llm_fn=lambda _text: llm_json,
    )
    edu = result.draft_profile["education"]
    assert edu[0]["gpa"] == "3.9/4.0"
    assert edu[1]["gpa"] == ""
    # Surfaced in provenance the same way the rest of the education
    # contribution is -- "from your résumé" in the options page.
    assert result.provenance["education"] == "llm"


# ---------------------------------------------------------------------------
# THE non-negotiable: canary fields are never inferred, even if a
# misbehaving LLM volunteers them despite being told not to.
# ---------------------------------------------------------------------------

HOSTILE_LLM_JSON = json.dumps(
    {
        "work_history": [
            {"title": "Engineer", "company": "Acme", "location": "", "start": "", "end": "", "current": False}
        ],
        "work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": False},
        "compensation": {"salary_expectation": "150000", "salary_currency": "USD"},
        "eeo_voluntary": {"gender": "Male", "veteran_status": "Not a veteran"},
        "current_title": "Engineer",
    }
)


def test_parse_llm_json_hostile_canary_fields_never_surface():
    """Even before strip_canary_fields runs, the allow-list in parse_llm_json
    itself must never carry a canary-shaped key through."""
    fields, _warnings = resume_import.parse_llm_json(HOSTILE_LLM_JSON)
    assert "work_authorization" not in fields
    assert "compensation" not in fields
    assert "eeo_voluntary" not in fields
    assert set(fields) <= {"work_history", "education", "current_title", "total_years_experience"}


def test_strip_canary_fields_removes_canary_sections_and_password():
    d = {
        "work_authorization": {"legally_authorized_to_work": True},
        "compensation": {"salary_expectation": "150000"},
        "eeo_voluntary": {"gender": "Male"},
        "personal": {"full_name": "Jane Doe", "password": "hunter2"},
        "work_history": [{"title": "Engineer"}],
    }
    stripped = resume_import.strip_canary_fields(d)
    assert "work_authorization" not in stripped
    assert "compensation" not in stripped
    assert "eeo_voluntary" not in stripped
    assert "password" not in stripped["personal"]
    assert stripped["personal"]["full_name"] == "Jane Doe"
    assert stripped["work_history"] == [{"title": "Engineer"}]


def test_resume_stating_work_authorization_produces_no_canary_values_in_draft(tmp_path):
    """The explicit task requirement: a résumé whose TEXT literally says
    'authorized to work in the US, no sponsorship required' -- and whose
    (fake, hostile-simulating) LLM pass volunteers a work_authorization
    block despite being told not to -- must still produce a draft with NO
    work_authorization/compensation/eeo_voluntary values at all, because
    the existing profile never had any either."""
    result = resume_import.import_resume(
        filename="resume.txt",
        data=RESUME_TXT.encode("utf-8"),  # contains "Authorized to work... no sponsorship required."
        existing_profile={},
        profile_dir=tmp_path,
        llm_fn=lambda _text: HOSTILE_LLM_JSON,
    )
    assert "work_authorization" not in result.draft_profile
    assert "compensation" not in result.draft_profile
    assert "eeo_voluntary" not in result.draft_profile
    # And provenance never claims to have sourced any such field either.
    assert not any(k.startswith("work_authorization") or k.startswith("compensation") for k in result.provenance)


def test_resume_import_never_overwrites_existing_operator_set_canary_values(tmp_path):
    """Stripping applies to the résumé's OWN contribution, not to the
    existing profile -- an operator-entered work_authorization must survive
    a résumé import completely untouched."""
    existing = {"work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": False}}
    result = resume_import.import_resume(
        filename="resume.txt",
        data=RESUME_TXT.encode("utf-8"),
        existing_profile=existing,
        profile_dir=tmp_path,
        llm_fn=lambda _text: HOSTILE_LLM_JSON,
    )
    assert result.draft_profile["work_authorization"] == {
        "legally_authorized_to_work": True,
        "require_sponsorship": False,
    }


# ---------------------------------------------------------------------------
# Merge semantics: draft = résumé contribution merged over existing profile
# ---------------------------------------------------------------------------


def test_merge_preserves_fields_the_resume_does_not_mention(tmp_path):
    existing = {"personal": {"full_name": "Old Name", "city": "Chicago"}, "experience": {"target_role": "PM"}}
    result = resume_import.import_resume(
        filename="resume.txt",
        data=RESUME_TXT.encode("utf-8"),
        existing_profile=existing,
        profile_dir=tmp_path,
        llm_fn=_fake_llm_fn,
        # a different person's résumé: merge semantics, identity change opted into
        allow_identity_change=True,
    )
    assert result.draft_profile["personal"]["city"] == "Chicago"
    assert result.draft_profile["experience"]["target_role"] == "PM"


def test_merge_overwrites_matched_fields_with_resume_values(tmp_path):
    existing = {"personal": {"full_name": "Old Name", "email": "old@example.com"}}
    result = resume_import.import_resume(
        filename="resume.txt",
        data=RESUME_TXT.encode("utf-8"),
        existing_profile=existing,
        profile_dir=tmp_path,
        llm_fn=_fake_llm_fn,
        # a different person's résumé: merge semantics, identity change opted into
        allow_identity_change=True,
    )
    assert result.draft_profile["personal"]["full_name"] == "Jane Doe"
    assert result.draft_profile["personal"]["email"] == "jane.doe@example.com"


def test_merge_never_touches_password(tmp_path):
    existing = {"personal": {"full_name": "Old Name", "password": "hunter2"}}
    result = resume_import.import_resume(
        filename="resume.txt",
        data=RESUME_TXT.encode("utf-8"),
        existing_profile=existing,
        profile_dir=tmp_path,
        llm_fn=_fake_llm_fn,
        # a different person's résumé: merge semantics, identity change opted into
        allow_identity_change=True,
    )
    assert result.draft_profile["personal"]["password"] == "hunter2"


def test_work_history_replaced_not_appended(tmp_path):
    existing = {"work_history": [{"title": "Old Job", "company": "OldCo"}]}
    result = resume_import.import_resume(
        filename="resume.txt",
        data=RESUME_TXT.encode("utf-8"),
        existing_profile=existing,
        profile_dir=tmp_path,
        llm_fn=_fake_llm_fn,
    )
    companies = [e["company"] for e in result.draft_profile["work_history"]]
    assert "OldCo" not in companies
    assert companies == ["Acme", "Globex"]


def test_work_history_preserved_when_llm_unavailable(tmp_path):
    existing = {"work_history": [{"title": "Old Job", "company": "OldCo"}]}

    def _raising_llm(_text: str) -> str:
        raise RuntimeError("No LLM provider configured.")

    result = resume_import.import_resume(
        filename="resume.txt",
        data=RESUME_TXT.encode("utf-8"),
        existing_profile=existing,
        profile_dir=tmp_path,
        llm_fn=_raising_llm,
    )
    assert result.draft_profile["work_history"] == [{"title": "Old Job", "company": "OldCo"}]
    assert result.warnings, "expected a warning explaining manual entry is needed"
    # Deterministic fields must still come through even with no LLM.
    assert result.draft_profile["personal"]["email"] == "jane.doe@example.com"


# ---------------------------------------------------------------------------
# import_resume: file storage side effects
# ---------------------------------------------------------------------------


def test_import_resume_saves_pdf_and_text_rendering(tmp_path):
    result = resume_import.import_resume(
        filename="whatever-the-browser-called-it.pdf",
        data=RESUME_PDF_BYTES,
        existing_profile={},
        profile_dir=tmp_path,
        llm_fn=_fake_llm_fn,
    )
    assert result.saved_filename == "resume.pdf"
    assert (tmp_path / "resume.pdf").read_bytes() == RESUME_PDF_BYTES
    rendered = (tmp_path / "resume.txt").read_text(encoding="utf-8")
    assert "Jane Doe" in rendered
    assert result.content_type == "application/pdf"


def test_import_resume_txt_upload_writes_resume_txt(tmp_path):
    result = resume_import.import_resume(
        filename="resume.txt",
        data=RESUME_TXT.encode("utf-8"),
        existing_profile={},
        profile_dir=tmp_path,
        llm_fn=_fake_llm_fn,
    )
    assert result.saved_filename == "resume.txt"
    assert (tmp_path / "resume.txt").read_text(encoding="utf-8") == RESUME_TXT


def test_import_resume_rejects_oversized_before_writing_anything(tmp_path):
    profile_dir = tmp_path / "profile"
    data = b"x" * (resume_import.MAX_UPLOAD_BYTES + 1)
    with pytest.raises(resume_import.ResumeImportError):
        resume_import.import_resume(
            filename="resume.txt", data=data, existing_profile={}, profile_dir=profile_dir,
        )
    assert not profile_dir.exists()


def test_import_resume_rejects_unsupported_extension_before_writing_anything(tmp_path):
    profile_dir = tmp_path / "profile"
    with pytest.raises(resume_import.ResumeImportError):
        resume_import.import_resume(
            filename="resume.exe", data=b"MZ...", existing_profile={}, profile_dir=profile_dir,
        )
    assert not profile_dir.exists()


def test_import_resume_corrupt_pdf_raises_before_writing_anything(tmp_path):
    profile_dir = tmp_path / "profile"
    with pytest.raises(resume_import.ResumeImportError):
        resume_import.import_resume(
            filename="resume.pdf", data=b"not a real pdf", existing_profile={}, profile_dir=profile_dir,
        )
    assert not profile_dir.exists()


# ---------------------------------------------------------------------------
# find_stored_resume / content_type_for
# ---------------------------------------------------------------------------


def test_find_stored_resume_prefers_original_over_text_rendering(tmp_path):
    (tmp_path / "resume.txt").write_text("rendering", encoding="utf-8")
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
    found = resume_import.find_stored_resume(tmp_path)
    assert found.name == "resume.pdf"


def test_find_stored_resume_none_when_nothing_uploaded(tmp_path):
    assert resume_import.find_stored_resume(tmp_path) is None


# ===========================================================================
# HTTP layer: POST /profile/import-resume, GET /resume, GET /resume/info
# ===========================================================================


def _disk_app(tmp_path):
    app = create_app(app_dir=tmp_path, root=tmp_path)
    return app, app.state.token


class _FakeLLMClient:
    def __init__(self, response: str):
        self._response = response

    def chat(self, _messages, **_kwargs):
        return self._response


def _patch_llm(monkeypatch, response: str = VALID_LLM_JSON):
    monkeypatch.setattr("applypilot.llm.get_client", lambda: _FakeLLMClient(response))


def _patch_llm_unavailable(monkeypatch):
    def _raise():
        raise RuntimeError("No LLM provider configured.")

    monkeypatch.setattr("applypilot.llm.get_client", _raise)


def test_import_resume_endpoint_requires_token(tmp_path):
    app, _token = _disk_app(tmp_path)
    client = TestClient(app)
    resp = client.post("/profile/import-resume", files={"file": ("resume.txt", RESUME_TXT.encode(), "text/plain")})
    assert resp.status_code == 401


def test_import_resume_endpoint_returns_draft_and_provenance(tmp_path, monkeypatch):
    _patch_llm(monkeypatch)
    app, token = _disk_app(tmp_path)
    client = TestClient(app)
    resp = client.post(
        "/profile/import-resume",
        headers={"X-ApplyPilot-Token": token},
        files={"file": ("resume.pdf", RESUME_PDF_BYTES, "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"]["personal"]["email"] == "jane.doe@example.com"
    assert body["profile"]["work_history"][0]["company"] == "Acme"
    assert body["provenance"]["personal.email"] == "deterministic"
    assert body["provenance"]["work_history"] == "llm"
    assert body["resume"]["filename"] == "resume.pdf"
    assert body["resume"]["content_type"] == "application/pdf"
    # Never present, per the canary non-negotiable.
    assert "work_authorization" not in body["profile"]
    assert "compensation" not in body["profile"]
    assert "eeo_voluntary" not in body["profile"]


def test_import_resume_endpoint_degrades_without_llm(tmp_path, monkeypatch):
    _patch_llm_unavailable(monkeypatch)
    # Also forces "no Claude CLI either" so this asserts genuine
    # no-LLM-at-all degradation regardless of whether this machine happens
    # to have the Claude Code CLI installed (llm_util's fallback -- see the
    # Claude-CLI tests below -- would otherwise pick it up here).
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: None)
    app, token = _disk_app(tmp_path)
    client = TestClient(app)
    resp = client.post(
        "/profile/import-resume",
        headers={"X-ApplyPilot-Token": token},
        files={"file": ("resume.txt", RESUME_TXT.encode(), "text/plain")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"]["personal"]["email"] == "jane.doe@example.com"
    assert body["warnings"], "expected a warning that the LLM-derived fields need manual entry"
    assert "work_history" not in body["profile"]


def test_import_resume_endpoint_uses_claude_cli_when_no_provider_configured(tmp_path, monkeypatch):
    # Bug 3, résumé-import side: no LLM_PROVIDER/API key set, but the
    # Claude Code CLI is "installed" (monkeypatched) -- the import should
    # use it automatically rather than degrading to a warning.
    _patch_llm_unavailable(monkeypatch)  # applypilot.llm.get_client() raises
    monkeypatch.setattr("applypilot.config.find_claude_binary", lambda: "/fake/claude.cmd")
    monkeypatch.setattr("applypilot.llm.ClaudeCodeClient", lambda model: _FakeLLMClient(VALID_LLM_JSON))

    app, token = _disk_app(tmp_path)
    client = TestClient(app)
    resp = client.post(
        "/profile/import-resume",
        headers={"X-ApplyPilot-Token": token},
        files={"file": ("resume.pdf", RESUME_PDF_BYTES, "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provenance"]["work_history"] == "llm"
    assert body["profile"]["work_history"][0]["company"] == "Acme"
    assert not body["warnings"]


def test_import_resume_endpoint_rejects_oversized(tmp_path):
    app, token = _disk_app(tmp_path)
    client = TestClient(app)
    data = b"x" * (resume_import.MAX_UPLOAD_BYTES + 1)
    resp = client.post(
        "/profile/import-resume",
        headers={"X-ApplyPilot-Token": token},
        files={"file": ("resume.txt", data, "text/plain")},
    )
    assert resp.status_code == 413


def test_import_resume_endpoint_rejects_unsupported_type(tmp_path):
    app, token = _disk_app(tmp_path)
    client = TestClient(app)
    resp = client.post(
        "/profile/import-resume",
        headers={"X-ApplyPilot-Token": token},
        files={"file": ("resume.exe", b"MZ...", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "resume.pdf" not in [p.name for p in tmp_path.iterdir()]


def test_import_resume_endpoint_never_writes_profile_json(tmp_path, monkeypatch):
    _patch_llm(monkeypatch)
    (tmp_path / "profile.json").write_text(json.dumps({"personal": {"full_name": "Original Name"}}), encoding="utf-8")
    before = (tmp_path / "profile.json").read_text(encoding="utf-8")

    app, token = _disk_app(tmp_path)
    client = TestClient(app)
    resp = client.post(
        "/profile/import-resume",
        headers={"X-ApplyPilot-Token": token},
        files={"file": ("resume.pdf", RESUME_PDF_BYTES, "application/pdf")},
        # "Jane Doe" over "Original Name": opt into the identity change, so
        # this still proves the endpoint never writes profile.json itself.
        data={"allow_identity_change": "true"},
    )
    assert resp.status_code == 200
    # The uploaded résumé claims "Jane Doe" -- if profile.json had been
    # written, it would no longer read "Original Name".
    after = (tmp_path / "profile.json").read_text(encoding="utf-8")
    assert after == before
    assert json.loads(after)["personal"]["full_name"] == "Original Name"


def test_import_resume_endpoint_saves_file_into_profile_directory(tmp_path, monkeypatch):
    _patch_llm(monkeypatch)
    app, token = _disk_app(tmp_path)
    client = TestClient(app)
    client.post(
        "/profile/import-resume",
        headers={"X-ApplyPilot-Token": token},
        files={"file": ("resume.pdf", RESUME_PDF_BYTES, "application/pdf")},
    )
    # Legacy (single-profile) layout: the profile directory IS root.
    assert (tmp_path / "resume.pdf").exists()
    assert (tmp_path / "resume.txt").exists()


# --- GET /resume, GET /resume/info -------------------------------------------


def test_get_resume_404_when_nothing_uploaded(tmp_path):
    app, token = _disk_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/resume", headers={"X-ApplyPilot-Token": token})
    assert resp.status_code == 404


def test_get_resume_info_404_when_nothing_uploaded(tmp_path):
    app, token = _disk_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/resume/info", headers={"X-ApplyPilot-Token": token})
    assert resp.status_code == 404


def test_get_resume_requires_token(tmp_path):
    app, _token = _disk_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/resume")
    assert resp.status_code == 401


def test_get_resume_serves_uploaded_bytes(tmp_path, monkeypatch):
    _patch_llm(monkeypatch)
    app, token = _disk_app(tmp_path)
    client = TestClient(app)
    client.post(
        "/profile/import-resume",
        headers={"X-ApplyPilot-Token": token},
        files={"file": ("resume.pdf", RESUME_PDF_BYTES, "application/pdf")},
    )

    resp = client.get("/resume", headers={"X-ApplyPilot-Token": token})
    assert resp.status_code == 200
    assert resp.content == RESUME_PDF_BYTES
    assert resp.headers["content-type"].startswith("application/pdf")
    assert "resume.pdf" in resp.headers.get("content-disposition", "")


def test_get_resume_info_reports_metadata(tmp_path, monkeypatch):
    _patch_llm(monkeypatch)
    app, token = _disk_app(tmp_path)
    client = TestClient(app)
    client.post(
        "/profile/import-resume",
        headers={"X-ApplyPilot-Token": token},
        files={"file": ("resume.pdf", RESUME_PDF_BYTES, "application/pdf")},
    )

    resp = client.get("/resume/info", headers={"X-ApplyPilot-Token": token})
    assert resp.status_code == 200
    body = resp.json()
    assert body["filename"] == "resume.pdf"
    assert body["content_type"] == "application/pdf"
    assert body["size"] == len(RESUME_PDF_BYTES)
    assert body["mtime"] > 0
