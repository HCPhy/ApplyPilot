from applypilot.discovery.avature import (
    _extract_next_url,
    _parse_detail_page,
    _parse_listing_page,
    _posted_recently,
    _with_records_per_page,
)


LISTING_HTML = """
<section class="section js_views">
  <article class="article article--result 1" id="article--1">
    <div class="article__header">
      <div class="article__header__text">
        <h3 class="article__header__text__title title title--h3 title--white" data-au="ag-h3-6">
          <a class="link" href="https://jobs.siemens.com/en_US/externaljobs/JobDetail/500639" data-au="ag-a-10">
            Research Engineer
          </a>
        </h3>
        <div class="article__header__text__subtitle">
          <span class="list-item-location">
            <span class="list-item-jobCity">Princeton</span><span class="separator">, </span>
            <span class="list-item-jobState">NJ</span><span class="separator">, </span>
            <span class="list-item-jobCountry">United States</span>
          </span>
          <span class="separator">&nbsp;&#8226;&nbsp;</span>
          <span class="list-item-jobId">Job ID: 500639</span>
          <span class="separator">&nbsp;&#8226;&nbsp;</span>
          <span class="list-item-family">Research & Development</span>
        </div>
      </div>
    </div>
    <div class="article__footer">
      <a class="button button--primary" href="https://jobs.siemens.com/en_US/externaljobs/JobDetail/500639">Learn more</a>
    </div>
  </article>
  <div class="list-controls__pagination">
    <ul class="list-controls__pagination__list">
      <li class="list-controls__pagination__item paginationNextLink">
        <a href="https://jobs.siemens.com/en_US/externaljobs/SearchJobs/?folderRecordsPerPage=6&amp;folderOffset=6">Next &gt;&gt;</a>
      </li>
    </ul>
  </div>
</section>
"""


DETAIL_HTML = """
<section class="section js_views">
  <div class="section__header">
    <div class="section__header__text">
      <h3 class="section__header__text__title title title--h3 title--white">
        Research Engineer
      </h3>
    </div>
  </div>
  <article class="article article--details regular-fields--cols-2Z regular-fields-label--inline">
    <div class="article__content__view">
      <div class="article__content__view__field ">
        <div class="article__content__view__field__label">Job ID</div>
        <div class="article__content__view__field__value">500639</div>
      </div>
      <div class="article__content__view__field ">
        <div class="article__content__view__field__label">Posted since</div>
        <div class="article__content__view__field__value">09-Apr-2026</div>
      </div>
      <div class="article__content__view__field ">
        <div class="article__content__view__field__label">Field of work</div>
        <div class="article__content__view__field__value">Research &amp; Development</div>
      </div>
      <div class="article__content__view__field tf_locations">
        <div class="article__content__view__field__label">Location(s)</div>
        <div class="article__content__view__field__value">
          <ul class="list list--bullet list--locations">
            <li class="list__item">Princeton - NJ - United States</li>
          </ul>
        </div>
      </div>
    </div>
  </article>
  <article class="article article--details ">
    <div class="article__content__view">
      <div class="article__content__view__field tf_replaceFieldVideoTokens">
        <div class="article__content__view__field__value">
          <p>Build simulation systems.</p>
          <ul><li>Use Python</li><li>Use C++</li></ul>
        </div>
      </div>
    </div>
  </article>
  <aside class="aside" role="presentation">
    <article class="article article--actions">
      <div class="article__content">
        <div class="button-bar button-bar--cols-1">
          <div class="button-bar__wrap">
            <a class="button button--hero" href="https://jobs.siemens.com/en_US/externaljobs/ApplicationMethods?folderId=500639">
              Apply
            </a>
          </div>
        </div>
      </div>
    </article>
  </aside>
</section>
"""


def test_listing_parser_extracts_jobs_and_next_page():
    board = {
        "name": "Siemens",
        "search_url": "https://jobs.siemens.com/en_US/externaljobs/SearchJobs",
    }
    jobs = _parse_listing_page(board, LISTING_HTML)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["title"] == "Research Engineer"
    assert job["url"] == "https://jobs.siemens.com/en_US/externaljobs/JobDetail/500639"
    assert job["location"] == "Princeton, NJ, United States"
    assert job["family"] == "Research & Development"

    next_url = _extract_next_url(board["search_url"], LISTING_HTML)
    assert next_url == "https://jobs.siemens.com/en_US/externaljobs/SearchJobs/?folderRecordsPerPage=6&folderOffset=6"


def test_detail_parser_extracts_description_and_apply_url():
    parsed = _parse_detail_page(
        {
            "url": "https://jobs.siemens.com/en_US/externaljobs/JobDetail/500639",
            "application_url": "https://jobs.siemens.com/en_US/externaljobs/JobDetail/500639",
            "title": "Research Engineer",
            "location": "Princeton, NJ, United States",
            "site": "Siemens",
            "strategy": "avature_html",
        },
        DETAIL_HTML,
    )

    assert parsed["application_url"] == "https://jobs.siemens.com/en_US/externaljobs/ApplicationMethods?folderId=500639"
    assert parsed["updated_at"] == "09-Apr-2026"
    assert parsed["location"] == "Princeton - NJ - United States"
    assert "Build simulation systems." in parsed["full_description"]
    assert "Use Python" in parsed["full_description"]


def test_posted_recently_handles_avature_date_format():
    assert _posted_recently("09-Apr-2026", hours_old=24 * 14)
    assert not _posted_recently("01-Jan-2020", hours_old=24 * 14)


def test_with_records_per_page_overrides_existing_page_size():
    url = "https://jobs.siemens.com/en_US/externaljobs/SearchJobs/?folderRecordsPerPage=6&folderOffset=6"
    normalized = _with_records_per_page(url, 100)
    assert "folderRecordsPerPage=100" in normalized
    assert "folderOffset=6" in normalized


if __name__ == "__main__":
    test_listing_parser_extracts_jobs_and_next_page()
    test_detail_parser_extracts_description_and_apply_url()
    test_posted_recently_handles_avature_date_format()
    test_with_records_per_page_overrides_existing_page_size()
    print("ok")
