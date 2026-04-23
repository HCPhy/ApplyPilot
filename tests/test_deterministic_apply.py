from applypilot.apply import dashboard, deterministic


def test_detect_apply_platform():
    assert deterministic.detect_apply_platform("https://job-boards.greenhouse.io/company/jobs/123") == "greenhouse"
    assert deterministic.detect_apply_platform("https://jobs.lever.co/company/123") == "lever"
    assert deterministic.detect_apply_platform("https://jobs.ashbyhq.com/openai/job/application") == "ashby"
    assert deterministic.detect_apply_platform("https://foo.wd5.myworkdayjobs.com/en-US/bar/job/x") == "workday"
    assert deterministic.detect_apply_platform("https://example.com/jobs/1") is None


def test_build_applicant_context_extracts_resume_and_profile_fields(monkeypatch):
    profile = {
        "personal": {
            "full_name": "Hanchen Liu",
            "preferred_name": "Hanchen",
            "email": "hanchen@example.com",
            "phone": "6314283551",
            "city": "Boston",
            "province_state": "MA",
            "country": "USA",
            "postal_code": "02116",
            "address": "123 Main St",
            "linkedin_url": "https://www.linkedin.com/in/hanchen-liu/",
            "github_url": "https://github.com/HCPhy",
            "portfolio_url": "https://hanchen.dev",
            "website_url": "https://hanchenliu.com",
            "password": "Secret123!",
        },
        "work_authorization": {
            "legally_authorized_to_work": True,
            "require_sponsorship": True,
            "work_permit_type": "F1-OPT",
        },
        "experience": {
            "education_level": "PhD",
            "current_title": "",
            "years_of_experience_total": "",
            "target_role": "Software Engineer",
        },
        "compensation": {
            "salary_expectation": "",
            "salary_range_min": "",
        },
        "availability": {"relocation_scope": "us", "earliest_start_date": "Immediately"},
        "resume_facts": {"preserved_school": ""},
    }
    resume_text = """HANCHEN LIU
SUMMARY
Something
EDUCATION
Boston College — Ph.D. in Physics, Expected 2026
Stony Brook University — M.A. in Physics, 2020
"""

    monkeypatch.setattr(deterministic.config, "load_profile", lambda: profile)
    monkeypatch.setattr(
        deterministic,
        "_load_resume_assets_local",
        lambda job, use_base_resume=False: (resume_text, "/tmp/resume.pdf", "tailored"),
    )

    ctx = deterministic.build_applicant_context({"url": "https://example.com/job"})

    assert ctx.first_name == "Hanchen"
    assert ctx.last_name == "Liu"
    assert ctx.github_username == "HCPhy"
    assert ctx.education_school == "Boston College"
    assert ctx.education_degree == "Ph.D."
    assert ctx.education_discipline == "Physics"
    assert ctx.education_end_year == "2026"
    assert ctx.salary_fallback_text == "Flexible / open to discuss"
    assert ctx.preferred_name == "Han"
    assert ctx.address == "123 Main St"
    assert ctx.postal_code == "02116"
    assert ctx.website_url == "https://hanchenliu.com"
    assert ctx.site_password == "Secret123!"
    assert ctx.legally_authorized_to_work is True


def test_normalize_workday_apply_url():
    assert deterministic._normalize_workday_apply_url(
        "https://foo.wd5.myworkdayjobs.com/en-US/site/job/Role_123"
    ) == "https://foo.wd5.myworkdayjobs.com/en-US/site/job/Role_123/apply"
    assert deterministic._normalize_workday_apply_url(
        "https://foo.wd5.myworkdayjobs.com/en-US/site/job/Role_123/apply"
    ) == "https://foo.wd5.myworkdayjobs.com/en-US/site/job/Role_123/apply"
    assert deterministic._normalize_workday_apply_url(
        "https://foo.wd5.myworkdayjobs.com/en-US/site/job/Role_123/apply/applyManually?source=linkedin"
    ) == "https://foo.wd5.myworkdayjobs.com/en-US/site/job/Role_123/apply/applyManually"


def test_trace_progress_updates_dashboard_state():
    worker_id = 97
    dashboard.init_worker(worker_id)

    deterministic._trace_progress(
        worker_id,
        "Filling contact info",
        verbose=True,
        page=None,
        delay_ms=0,
    )

    state = dashboard.get_state(worker_id)
    assert state is not None
    assert state.actions == 1
    assert state.last_action == "Filling contact info"


def test_degree_terms_match_greenhouse_labels():
    assert deterministic._degree_terms("Ph.D.")[0] == "Doctor of Philosophy (Ph.D.)"
    assert deterministic._degree_terms("Master of Science")[0] == "Master's Degree"
    assert deterministic._degree_terms("B.S.")[0] == "Bachelor's Degree"


def test_choose_eligibility_terms_uses_exact_greenhouse_options():
    yes_ctx = deterministic.ApplicantContext(
        full_name="Hanchen Liu",
        first_name="Hanchen",
        last_name="Liu",
        email="hanchen@example.com",
        phone="6314283551",
        phone_digits="6314283551",
        city="Boston",
        state="MA",
        country="USA",
        current_location="Boston, MA, USA",
        linkedin_url="https://linkedin.com/in/hanchen",
        github_url="https://github.com/HCPhy",
        github_username="HCPhy",
        require_sponsorship=True,
        work_permit_type="F1-OPT",
        education_school="Boston College",
        education_degree="Ph.D.",
        education_discipline="Physics",
        education_end_year="2026",
        education_level="PhD",
        current_title="",
        years_experience_total="",
        salary_expectation="",
        salary_fallback_text="Flexible / open to discuss",
        resume_pdf_path="/tmp/resume.pdf",
        cover_letter_pdf_path=None,
        resume_text="",
        target_role="Software Engineer",
        relocation_scope="us",
    )
    no_ctx = deterministic.ApplicantContext(
        **{**yes_ctx.__dict__, "require_sponsorship": False, "work_permit_type": "US Citizen"}
    )

    assert deterministic._choose_eligibility_terms(yes_ctx) == ["Yes, will require firm sponsorship"]
    assert deterministic._choose_eligibility_terms(no_ctx) == ["No. already has permanent work authorization"]


def test_greenhouse_option_mappings_for_experience_degree_and_internships():
    ctx = deterministic.ApplicantContext(
        full_name="Hanchen Liu",
        first_name="Hanchen",
        last_name="Liu",
        email="hanchen@example.com",
        phone="6314283551",
        phone_digits="6314283551",
        city="Boston",
        state="MA",
        country="USA",
        current_location="Boston, MA, USA",
        linkedin_url="https://linkedin.com/in/hanchen",
        github_url="https://github.com/HCPhy",
        github_username="HCPhy",
        require_sponsorship=True,
        work_permit_type="F1-OPT",
        education_school="Boston College",
        education_degree="Ph.D.",
        education_discipline="Physics",
        education_end_year="2026",
        education_level="PhD",
        current_title="",
        years_experience_total="",
        salary_expectation="",
        salary_fallback_text="Flexible / open to discuss",
        resume_pdf_path="/tmp/resume.pdf",
        cover_letter_pdf_path=None,
        resume_text="Research assistant work with no internship listed.",
        target_role="Software Engineer",
        relocation_scope="us",
    )

    assert deterministic._choose_experience_terms(ctx) == ["Graduate (no full time experience)"]
    assert deterministic._current_degree_terms(ctx) == ["PhD"]
    assert deterministic._gpa_terms(ctx) == ["NA"]
    assert deterministic._internship_terms(ctx) == ["No, I have not completed any internships."]

    internship_ctx = deterministic.ApplicantContext(**{**ctx.__dict__, "resume_text": "Software Engineering Intern at Example Co."})
    assert deterministic._internship_terms(internship_ctx) == [
        "Yes, I have internship experience, but not within a hedge fund or proprietary trading firm."
    ]


def test_current_company_text_prefers_current_title_and_school():
    ctx = deterministic.ApplicantContext(
        full_name="Hanchen Liu",
        first_name="Hanchen",
        last_name="Liu",
        email="hanchen@example.com",
        phone="6314283551",
        phone_digits="6314283551",
        city="Boston",
        state="MA",
        country="USA",
        current_location="Boston, MA, USA",
        linkedin_url="https://linkedin.com/in/hanchen",
        github_url="https://github.com/HCPhy",
        github_username="HCPhy",
        require_sponsorship=True,
        work_permit_type="F1-OPT",
        education_school="Boston College",
        education_degree="Ph.D.",
        education_discipline="Physics",
        education_end_year="2026",
        education_level="PhD",
        current_title="Research Assistant",
        years_experience_total="",
        salary_expectation="",
        salary_fallback_text="Flexible / open to discuss",
        resume_pdf_path="/tmp/resume.pdf",
        cover_letter_pdf_path=None,
        resume_text="",
        target_role="Software Engineer",
        relocation_scope="us",
    )

    assert deterministic._current_company_text(ctx) == "Research Assistant at Boston College"
