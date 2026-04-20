from applypilot.discovery.ashby import _parse_job as parse_ashby_job
from applypilot.discovery.recruitee import _extract_items as extract_recruitee_items
from applypilot.discovery.recruitee import _parse_job as parse_recruitee_job
from applypilot.discovery.smartrecruiters import _merge_detail as merge_smartrecruiters_detail
from applypilot.discovery.smartrecruiters import _parse_list_job as parse_smartrecruiters_job
from applypilot.discovery.workable import _parse_job as parse_workable_job


def test_parse_ashby_job_extracts_core_fields():
    job = parse_ashby_job(
        {"name": "OpenAI"},
        {
            "title": "Software Engineer",
            "location": "San Francisco",
            "secondaryLocations": [{"location": "Remote - US"}],
            "isRemote": True,
            "descriptionHtml": "<p>Build useful systems.</p>",
            "publishedAt": "2026-04-18T10:00:00Z",
            "jobUrl": "https://jobs.ashbyhq.com/openai/job",
            "applyUrl": "https://jobs.ashbyhq.com/openai/job/application",
            "compensation": {
                "compensationTierSummary": "$100K - $150K, Offers Equity",
            },
        },
    )

    assert job["title"] == "Software Engineer"
    assert job["site"] == "OpenAI"
    assert job["strategy"] == "ashby_api"
    assert job["url"] == "https://jobs.ashbyhq.com/openai/job"
    assert job["application_url"] == "https://jobs.ashbyhq.com/openai/job/application"
    assert job["location"] == "San Francisco; Remote - US"
    assert job["salary"] == "$100K - $150K, Offers Equity"
    assert job["full_description"] == "Build useful systems."


def test_parse_recruitee_job_handles_public_offer_shape():
    payload = {
        "offers": [
            {
                "title": "Backend Engineer",
                "slug": "backend-engineer",
                "location": {"city": "San Francisco", "country": "United States"},
                "remote": True,
                "description": "<p>Write services.</p>",
                "requirements": "<ul><li>Python</li></ul>",
                "careers_url": "https://example.recruitee.com/o/backend-engineer",
                "careers_apply_url": "https://example.recruitee.com/o/backend-engineer/c/new",
                "created_at": "2026-04-18T10:00:00Z",
            }
        ]
    }

    items = extract_recruitee_items(payload)
    job = parse_recruitee_job({"name": "Example", "subdomain": "example"}, items[0])

    assert len(items) == 1
    assert job["title"] == "Backend Engineer"
    assert job["strategy"] == "recruitee_api"
    assert job["url"] == "https://example.recruitee.com/o/backend-engineer"
    assert job["application_url"] == "https://example.recruitee.com/o/backend-engineer/c/new"
    assert job["location"] == "San Francisco, United States (Remote)"
    assert "Write services." in job["full_description"]
    assert "Python" in job["full_description"]


def test_parse_workable_job_handles_public_account_shape():
    job = parse_workable_job(
        {"name": "Example"},
        {
            "title": "Platform Engineer",
            "url": "https://apply.workable.com/example/j/ABC123",
            "application_url": "https://apply.workable.com/example/j/ABC123/apply",
            "location": {
                "location_str": "New York, United States",
                "telecommuting": True,
                "workplace_type": "hybrid",
            },
            "salary": {
                "salary_from": 150000,
                "salary_to": 200000,
                "salary_currency": "usd",
            },
            "description": "<p>Own the platform.</p>",
            "created_at": "2026-04-18T10:00:00Z",
        },
    )

    assert job["title"] == "Platform Engineer"
    assert job["strategy"] == "workable_api"
    assert job["location"] == "New York, United States (Remote)"
    assert job["salary"] == "USD 150,000-200,000"
    assert job["full_description"] == "Own the platform."


def test_parse_smartrecruiters_job_merges_detail_content():
    list_job = parse_smartrecruiters_job(
        {"name": "Example", "company_identifier": "example"},
        {
            "id": "123",
            "name": "Data Engineer",
            "releasedDate": "2026-04-18T10:00:00Z",
            "location": {"city": "Austin", "region": "TX", "country": "US", "remote": True},
            "company": {"identifier": "example"},
        },
    )
    merged = merge_smartrecruiters_detail(
        list_job,
        {
            "postingUrl": "https://jobs.smartrecruiters.com/example/123-data-engineer",
            "applyUrl": "https://jobs.smartrecruiters.com/example/123-data-engineer?oga=true",
            "jobAd": {
                "sections": {
                    "jobDescription": {"title": "Job Description", "text": "<p>Build data products.</p>"},
                    "qualifications": {"title": "Qualifications", "text": "<p>Python and SQL.</p>"},
                }
            },
            "compensation": {"summary": "USD 140,000-180,000"},
        },
    )

    assert list_job["url"] == "https://jobs.smartrecruiters.com/example/123"
    assert list_job["location"] == "Austin, TX, US (Remote)"
    assert merged["strategy"] == "smartrecruiters_api"
    assert merged["url"] == "https://jobs.smartrecruiters.com/example/123-data-engineer"
    assert merged["application_url"] == "https://jobs.smartrecruiters.com/example/123-data-engineer?oga=true"
    assert merged["salary"] == "USD 140,000-180,000"
    assert "Build data products." in merged["full_description"]
    assert "Python and SQL." in merged["full_description"]
