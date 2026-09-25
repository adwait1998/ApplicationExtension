"""Skills come from the résumé's own skills section, verbatim."""
from applypilot.extension import resume_import
from applypilot.extension.resume_import import extract_skills, merge_skills

CATEGORIZED = """Taylor Morgan
taylor@example.com

TECHNICAL SKILLS
Languages: Python, SQL, Go
Cloud & Infrastructure: AWS (S3, EC2, IAM, Lambda), Microsoft Fabric (Lakehouse, Warehouse,
OneLake), Docker,
Kubernetes, Terraform

EXPERIENCE
Acme — Engineer
"""

DESIGNER = """Taylor Morgan
SKILLS
Figma, Sketch, Adobe XD, Prototyping
User Research | Usability Testing
• Design Systems
EDUCATION
School of Design
"""


def test_categorized_lines_with_wrapping_and_parentheses():
    sk = extract_skills(CATEGORIZED)
    assert sk["languages"] == ["Python", "SQL", "Go"]
    assert sk["cloud_infrastructure"] == [
        "AWS (S3, EC2, IAM, Lambda)", "Microsoft Fabric (Lakehouse, Warehouse, OneLake)",
        "Docker", "Kubernetes", "Terraform"]
    assert "Acme — Engineer" not in str(sk)   # stops at the next section


def test_plain_list_pipes_and_bullets():
    assert extract_skills(DESIGNER) == {"skills": [
        "Figma", "Sketch", "Adobe XD", "Prototyping", "User Research", "Usability Testing", "Design Systems"]}


def test_inline_skills_line_and_no_section():
    assert extract_skills("T M\nSkills: Figma, Protopie\nExperience\nX") == {"skills": ["Figma", "Protopie"]}
    assert extract_skills("T M\nExperience\nDesigner, skilled in Figma") == {}


def test_merge_same_person_keeps_existing_and_names_what_resume_lacks():
    merged, warnings = merge_skills({"tools": ["Figma", "Jira"]}, {"skills": ["figma", "Sketch"]}, replace=False)
    assert merged == {"tools": ["Figma", "Jira"], "skills": ["Sketch"]}
    assert warnings and "Jira" in warnings[0]


def test_merge_replaces_on_identity_change_or_empty():
    assert merge_skills({"tools": ["Figma"]}, {"skills": ["SQL"]}, replace=True) == ({"skills": ["SQL"]}, [])
    assert merge_skills({}, {"skills": ["SQL"]}, replace=False) == ({"skills": ["SQL"]}, [])


def test_import_puts_skills_in_the_draft(tmp_path):
    r = resume_import.import_resume(filename="resume.txt", data=DESIGNER.encode(), existing_profile={},
                                    profile_dir=tmp_path, llm_fn=lambda _t: "{}")
    assert r.draft_profile["skills_boundary"]["skills"][:2] == ["Figma", "Sketch"]
    assert r.provenance["skills_boundary"] == "deterministic"
