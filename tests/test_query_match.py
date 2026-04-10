from applypilot.discovery.query_match import matches_query, query_to_search_text


def test_internship_synonym_matches_engineer_intern_query():
    title = "Software Developer Engineer, Internship"
    assert matches_query(title, "Software Engineer Intern")


def test_glob_pattern_matches_broader_software_engineer_titles():
    title = "Senior Software Engineer / Software Engineer (Digital Station)"
    assert matches_query(title, "*Software Engineer*")


def test_regex_prefix_matches_when_requested():
    title = "Research and Development Engineer Real-time Systems"
    assert matches_query(title, r"re:Research .* Engineer")


def test_query_to_search_text_strips_glob_markers_for_ats_apis():
    assert query_to_search_text("*Software Engineer*") == "Software Engineer"


if __name__ == "__main__":
    test_internship_synonym_matches_engineer_intern_query()
    test_glob_pattern_matches_broader_software_engineer_titles()
    test_regex_prefix_matches_when_requested()
    test_query_to_search_text_strips_glob_markers_for_ats_apis()
    print("ok")
