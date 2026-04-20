from applypilot.discovery.major_tech import (
    extract_google_search_data,
    parse_amazon_job,
    parse_apple_search_results,
    parse_google_job,
    parse_uber_job,
)


def test_parse_amazon_job_extracts_core_fields():
    job = parse_amazon_job(
        {
            "id": "a9f39b36",
            "id_icims": "3092179",
            "title": "Software Engineer",
            "description": "<p>Build discovery systems.</p>",
            "basic_qualifications": "- 1+ years of production software experience",
            "preferred_qualifications": (
                "Our compensation reflects the cost of labor. "
                "The base pay for this position ranges from $99,500/year."
            ),
            "normalized_location": "San Francisco, California, USA",
            "job_path": "/en/jobs/3092179/software-engineer",
            "posted_date": "April 17, 2026",
            "url_next_step": "https://account.amazon.com/jobs/3092179/apply",
        }
    )

    assert job["title"] == "Software Engineer"
    assert job["site"] == "Amazon"
    assert job["strategy"] == "amazon_jobs"
    assert job["url"] == "https://www.amazon.jobs/en/jobs/3092179/software-engineer"
    assert job["application_url"] == "https://account.amazon.com/jobs/3092179/apply"
    assert job["location"] == "San Francisco, California, USA"
    assert job["updated_at"] == "2026-04-17T00:00:00+00:00"
    assert "Build discovery systems." in job["full_description"]
    assert "production software experience" in job["full_description"]


def test_parse_apple_search_results_extracts_cards():
    html = """
    <html><head><link rel="next" href="/en-us/search?page=2"/></head><body>
      <li class="rc-accordion-item">
        <div class="job-title-location">
          <span class="a11y">Location</span>
          <span id="search-store-name-container-1">Cupertino</span>
        </div>
        <h3>
          <a href="/en-us/details/200642870-0836/software-engineer-apple-services-engineering?team=SFTWR">
            Software Engineer, Apple Services Engineering
          </a>
        </h3>
        <span class="team-name">Software and Services</span>
        <span class="job-posted-date">Apr 17, 2026</span>
        <div id="search-search-job-content-200642870-0836-job-summary-1">
          <p><span>Build scalable systems for Apple services.</span></p>
        </div>
      </li>
    </body></html>
    """

    jobs, has_next = parse_apple_search_results(html)

    assert has_next is True
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Software Engineer, Apple Services Engineering"
    assert jobs[0]["site"] == "Apple"
    assert jobs[0]["strategy"] == "apple_careers"
    assert jobs[0]["url"] == (
        "https://jobs.apple.com/en-us/details/200642870-0836/"
        "software-engineer-apple-services-engineering?team=SFTWR"
    )
    assert jobs[0]["location"] == "Cupertino, United States"
    assert jobs[0]["updated_at"] == "2026-04-17T00:00:00+00:00"
    assert "Build scalable systems" in jobs[0]["full_description"]
    assert "Role Number: 200642870-0836" in jobs[0]["full_description"]


def test_parse_uber_job_extracts_example_fields():
    job = parse_uber_job(
        {
            "id": 157476,
            "title": "Software Engineer I",
            "description": (
                "**Rate of Pay:** $150,000 to $166,000 per year\n\n"
                "Build customer-facing features. "
                "[Benefits](https://www.uber.com/careers/benefits)."
            ),
            "updatedDate": "2026-04-17T17:03:00.000Z",
            "allLocations": [
                {
                    "country": "USA",
                    "region": "California",
                    "city": "San Francisco",
                    "countryName": "United States",
                }
            ],
        }
    )

    assert job["title"] == "Software Engineer I"
    assert job["site"] == "Uber"
    assert job["strategy"] == "uber_careers"
    assert job["url"] == "https://www.uber.com/global/en/careers/list/157476/"
    assert job["application_url"] == "https://www.uber.com/careers/apply/interstitial/157476"
    assert job["location"] == "San Francisco, California, United States"
    assert job["salary"] == "$150,000 to $166,000 per year"
    assert "Build customer-facing features." in job["full_description"]
    assert "Benefits" in job["full_description"]


def test_extract_google_search_data_parses_ds1_literal():
    html = """
    <script>
    AF_initDataCallback({key: 'ds:1', hash: '2',
      data:[[["123","Software Engineer","https://apply.example",
      [null,"<ul><li>Build things.</li></ul>"],[null,"<p>Python</p>"],
      "company-path",null,"Google","en-US",
      [["San Francisco, CA, USA",["addr"],"San Francisco",null,"CA","US"]],
      [null,"<p>Write useful software.</p>"],[2],
      [1776000000,0],[1776000000,0],[1776000100,0],
      [null,""],null,null,[null,""],[null,"<p>BS degree</p>"],2]],null,1,20],
      sideChannel: {}});
    </script>
    """

    data = extract_google_search_data(html)

    assert data[2] == 1
    assert data[0][0][0] == "123"
    assert data[0][0][1] == "Software Engineer"


def test_parse_google_job_extracts_core_fields():
    raw_job = [
        "122948486462087878",
        "Software Engineer, Cross-Platform Material",
        "https://www.google.com/about/careers/applications/signin?jobId=abc",
        [None, "<ul><li>Prototype and design desired treatments.</li></ul>"],
        [None, "<h3>Minimum qualifications:</h3><ul><li>8 years of experience.</li></ul>"],
        "company-path",
        None,
        "Google",
        "en-US",
        [["San Francisco, CA, USA", ["1 Market St"], "San Francisco", "94105", "CA", "US"]],
        [
            None,
            (
                "<p>Google's software engineers develop next-generation technologies.</p>"
                "<p>The US base salary range for this full-time position is "
                "$207,000-$300,000 + bonus + equity + benefits.</p>"
            ),
        ],
        [2],
        [1775649638, 0],
        [1775649638, 0],
        [1775649639, 0],
        [None, "Applicants in San Francisco are considered."],
        None,
        None,
        [None, ""],
        [None, "<ul><li>Bachelor's degree or equivalent practical experience.</li></ul>"],
        3,
    ]

    job = parse_google_job(raw_job)

    assert job["title"] == "Software Engineer, Cross-Platform Material"
    assert job["site"] == "Google"
    assert job["strategy"] == "google_careers"
    assert job["url"] == (
        "https://www.google.com/about/careers/applications/jobs/results/"
        "122948486462087878/"
    )
    assert job["application_url"].endswith("jobId=abc")
    assert job["location"] == "San Francisco, CA, USA"
    assert job["salary"] == "$207,000-$300,000 + bonus + equity + benefits"
    assert "Prototype and design desired treatments." in job["full_description"]
    assert "8 years of experience." in job["full_description"]
