from applypilot.scoring.scorer import _rescore_selection_query


def _squash(sql: str) -> str:
    return " ".join(sql.split())


def test_threshold_rescore_selects_existing_scores_strictly_above_minimum():
    sql, params = _rescore_selection_query(limit=25, rescore_min_score=7)
    normalized = _squash(sql or "")

    assert "full_description IS NOT NULL" in normalized
    assert "fit_score IS NOT NULL" in normalized
    assert "fit_score > ?" in normalized
    assert normalized.endswith("LIMIT ?")
    assert params == (7, 25)


def test_all_rescore_does_not_apply_score_threshold():
    sql, params = _rescore_selection_query(limit=0, rescore=True)
    normalized = _squash(sql or "")

    assert "full_description IS NOT NULL" in normalized
    assert "fit_score > ?" not in normalized
    assert "LIMIT ?" not in normalized
    assert params == ()


def test_normal_scoring_uses_pending_score_selection():
    sql, params = _rescore_selection_query()

    assert sql is None
    assert params == ()
