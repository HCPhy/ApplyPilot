from applypilot.discovery import oracle
from applypilot.discovery.oracle import (
    merge_oracle_detail,
    oracle_search_jobs,
    parse_oracle_list_job,
)


def test_parse_oracle_list_job_extracts_core_fields():
    board = {
        "name": "JPMorgan Chase",
        "api_base_url": "https://jpmc.fa.oraclecloud.com",
        "site_number": "CX_1001",
    }

    job = parse_oracle_list_job(
        {
            "Id": "210651918",
            "Title": "Quantitative Trading & Research - FX Quantitative Trading - Associate",
            "PostedDate": "2026-03-19",
            "PrimaryLocation": "New York, NY, United States",
            "ShortDescriptionStr": "<p>Join the quantitative trading team.</p>",
        },
        board,
    )

    assert job["title"] == "Quantitative Trading & Research - FX Quantitative Trading - Associate"
    assert job["site"] == "JPMorgan Chase"
    assert job["strategy"] == "oracle_cloud"
    assert job["url"] == (
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210651918"
    )
    assert job["application_url"] == (
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210651918/apply/email"
    )
    assert job["location"] == "New York, NY, United States"
    assert job["updated_at"] == "2026-03-19"
    assert job["full_description"] == "Join the quantitative trading team."


def test_parse_oracle_list_job_uses_public_role_base_url():
    board = {
        "name": "Goldman Sachs",
        "api_base_url": "https://hdpc.fa.us2.oraclecloud.com",
        "site_number": "LateralHiring",
        "site_path": "LateralHiring",
        "public_role_base_url": "https://higher.gs.com/roles",
    }

    job = parse_oracle_list_job({"Id": "166070", "Title": "Quantitative Engineering"}, board)

    assert job["url"] == "https://higher.gs.com/roles/166070"
    assert job["application_url"] == (
        "https://hdpc.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
        "LateralHiring/job/166070/apply/email"
    )


def test_merge_oracle_detail_adds_description_salary_and_location():
    board = {
        "name": "JPMorgan Chase",
        "api_base_url": "https://jpmc.fa.oraclecloud.com",
        "site_number": "CX_1001",
    }
    job = parse_oracle_list_job({"Id": "210651918", "Title": "Quantitative Developer"}, board)

    merged = merge_oracle_detail(
        board,
        job,
        {
            "Id": "210651918",
            "ExternalPostedStartDate": "2026-03-19T17:18:23+00:00",
            "PrimaryLocation": "New York, NY, United States",
            "ExternalDescriptionStr": "<p>Build low latency trading infrastructure.</p>",
            "CorporateDescriptionStr": "<p>Equal opportunity employer.</p>",
            "requisitionFlexFields": [
                {
                    "Prompt": "Base Pay/Salary",
                    "Value": "New York,NY $135,000.00-$200,000.00",
                }
            ],
        },
    )

    assert merged["updated_at"] == "2026-03-19T17:18:23+00:00"
    assert merged["location"] == "New York, NY, United States"
    assert merged["salary"] == "New York,NY $135,000.00-$200,000.00"
    assert "Build low latency trading infrastructure." in merged["full_description"]
    assert "Equal opportunity employer." in merged["full_description"]


def test_oracle_search_jobs_parses_requisition_list(monkeypatch):
    seen = {}

    def fake_fetch_json(url, timeout=30):
        seen["url"] = url
        seen["timeout"] = timeout
        return {
            "items": [
                {
                    "TotalJobsCount": 1,
                    "requisitionList": [
                        {
                            "Id": "166070",
                            "Title": "Asset & Wealth Management - Quantitative Engineering",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(oracle, "fetch_json", fake_fetch_json)

    raw_jobs, total = oracle_search_jobs(
        {
            "api_base_url": "https://hdpc.fa.us2.oraclecloud.com",
            "site_number": "LateralHiring",
        },
        "*Quantitative Developer*",
        offset=25,
        limit=25,
        timeout=12,
    )

    assert total == 1
    assert raw_jobs[0]["Id"] == "166070"
    assert "recruitingCEJobRequisitions" in seen["url"]
    assert "siteNumber=LateralHiring,keyword=Quantitative+Developer" in seen["url"]
    assert "offset=25" in seen["url"]
    assert seen["timeout"] == 12
