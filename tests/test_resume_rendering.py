from __future__ import annotations

from applypilot.scoring.pdf import build_html, parse_resume
from applypilot.scoring.tailor import assemble_resume_text


def _resume_data() -> dict:
    return {
        "title": "Product Designer",
        "summary": "Product designer focused on marketplace UX.",
        "skills": {"Design": "Figma, Prototyping"},
        "experience": [
            {
                "header": "Product Designer at Acme",
                "subtitle": "Figma | 2024 - Present",
                "bullets": ["Designed checkout flows."],
            }
        ],
        "projects": [
            {
                "header": "Design System",
                "subtitle": "Figma | 2024",
                "bullets": ["Built reusable components."],
            },
            {
                "header": "Discovery Experience",
                "subtitle": "Miro | 2024",
                "bullets": ["Mapped research insights."],
            },
        ],
        "education": "Arizona State University",
    }


def test_assemble_resume_text_includes_portfolio_and_website_links():
    text = assemble_resume_text(
        _resume_data(),
        {
            "personal": {
                "full_name": "Nida Shah",
                "email": "nida@example.com",
                "phone": "4800000000",
                "linkedin_url": "https://linkedin.com/in/nidashah",
                "portfolio_url": "https://portfolio.example",
                "website_url": "https://site.example",
            }
        },
    )

    header = text.split("SUMMARY", 1)[0]
    assert "https://portfolio.example" in header
    assert "https://site.example" in header


def test_project_heading_is_wrapped_with_first_project_entry():
    text = assemble_resume_text(
        _resume_data(),
        {
            "personal": {
                "full_name": "Nida Shah",
                "email": "nida@example.com",
            }
        },
    )

    html = build_html(parse_resume(text))

    assert '<div class="section projects">' in html
    assert '<div class="section-lead"><div class="section-title">Projects</div><div class="entry">' in html
