from datetime import date

from applypilot.discovery.academicjobsonline import parse_academicjobsonline_jobs


def test_parse_academicjobsonline_jobs_extracts_open_postings():
    html = """
    <html><body>
      <div class="clr">
        <h3 class="x1">
          <a href="/ajo/Argonne%20National%20Laboratory">Argonne National Laboratory</a>,
          <a href="/ajo/Argonne/HEP">HEP High Energy Physics Division</a>
        </h3>
        <ol class="sp5 ldt">
          <li>[<a href="/ajo/jobs/31214" id="k31214">POSTDOCTORAL</a>]
            <span id="j31214" aria-hidden="true">Postdoctoral Appointee - AI/ML and ATLAS</span>
            <span class="purplesml">(Illinois, US)</span>
            <span class="sml">&nbsp; <a href="https://academicjobsonline.org/ajo/jobs/31214/apply">Apply</a></span>
          </li>
          <li>[<a href="/ajo/jobs/31757" id="k31757">FUSIONPOSTDOC</a>]
            <span id="j31757" aria-hidden="true">Postdoctoral Research Associate</span>
            <span class="purplesml">(deadline 2026/04/15 11:59PM, Wisconsin, US)</span>
            <span class="sml">&nbsp; <a href="https://academicjobsonline.org/ajo/jobs/31757/apply">Apply</a></span>
          </li>
          <li>[<a href="/ajo/jobs/31901" id="k31901">POSTDOC</a>]
            <span id="j31901" aria-hidden="true">Postdoctoral Fellow in Quantum Matter</span>
            <span class="purplesml">(deadline 2026/05/20 11:59PM, California, US)</span>
            <span class="sml">&nbsp; <a href="https://example.edu/apply">Apply</a></span>
          </li>
          <li>[<a href="/ajo/jobs/31902" id="k31902">POSTDOC</a>]
            <span id="j31902" aria-hidden="true">Postdoctoral Researcher</span>
            <span class="purplesml">(filled, deadline 2026/05/20 11:59PM, New York, US)</span>
            <span class="sml"></span>
          </li>
        </ol>
      </div>
    </body></html>
    """

    jobs = parse_academicjobsonline_jobs(
        html,
        {"name": "AcademicJobsOnline Physics"},
        today=date(2026, 4, 19),
    )

    assert [job["url"] for job in jobs] == [
        "https://academicjobsonline.org/ajo/jobs/31214",
        "https://academicjobsonline.org/ajo/jobs/31901",
    ]
    assert jobs[0]["strategy"] == "academicjobsonline"
    assert jobs[0]["site"] == "AcademicJobsOnline Physics"
    assert jobs[0]["location"] == "Illinois, US"
    assert jobs[0]["application_url"] == "https://academicjobsonline.org/ajo/jobs/31214/apply"
    assert jobs[0]["title"] == (
        "Argonne National Laboratory, HEP High Energy Physics Division - "
        "Postdoctoral Appointee - AI/ML and ATLAS"
    )
    assert jobs[1]["location"] == "California, US"
    assert jobs[1]["application_url"] == "https://example.edu/apply"
