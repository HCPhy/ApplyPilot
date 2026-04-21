import httpx

from applypilot.apply.openai_runner import (
    check_openai_computer_use_access,
    extract_text,
    parse_result,
    run_openai_computer_apply,
)


def test_parse_result_maps_applypilot_statuses():
    assert parse_result("RESULT:APPLIED") == "applied"
    assert parse_result("RESULT:EXPIRED") == "expired"
    assert parse_result("RESULT:CAPTCHA") == "captcha"
    assert parse_result("RESULT:LOGIN_ISSUE") == "login_issue"
    assert parse_result("RESULT:FAILED:sso_required") == "failed:sso_required"


def test_parse_result_ignores_non_result_text():
    assert parse_result("I am still working") is None


def test_extract_text_reads_message_and_reasoning_items():
    response = {
        "output": [
            {
                "type": "reasoning",
                "summary": [{"text": "Checking the page"}],
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "RESULT:APPLIED"}],
            },
        ]
    }

    assert extract_text(response) == "Checking the page\nRESULT:APPLIED"


def test_model_not_found_maps_to_clear_failure(monkeypatch, tmp_path):
    class FakeResponse:
        status_code = 404
        text = '{"error":{"code":"model_not_found"}}'

        def json(self):
            return {"error": {"code": "model_not_found"}}

    def fake_require_openai_key():
        return "test-key"

    def fake_post_response(*args, **kwargs):
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        response = httpx.Response(404, request=request, json={"error": {"code": "model_not_found"}})
        raise httpx.HTTPStatusError("model missing", request=request, response=response)

    class FakePage:
        url = "https://example.com/apply"

        def set_viewport_size(self, size):
            return None

        def goto(self, *args, **kwargs):
            return None

        def wait_for_load_state(self, *args, **kwargs):
            return None

        def screenshot(self, **kwargs):
            return b"png"

    class FakeContext:
        def new_page(self):
            return FakePage()

    class FakeBrowser:
        contexts = [FakeContext()]

    class FakeChromium:
        def connect_over_cdp(self, url):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        def __init__(self, *args, **kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("applypilot.apply.openai_runner._require_openai_key", fake_require_openai_key)
    monkeypatch.setattr("applypilot.apply.openai_runner._post_response", fake_post_response)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr("httpx.Client", FakeClient)
    monkeypatch.setattr("applypilot.apply.openai_runner.config.LOG_DIR", tmp_path)
    monkeypatch.setattr("applypilot.apply.openai_runner.update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr("applypilot.apply.openai_runner.add_event", lambda *args, **kwargs: None)

    result, _ = run_openai_computer_apply(
        job={"url": "https://example.com/job", "title": "Role", "site": "Example"},
        port=9222,
        worker_id=0,
        model="computer-use-preview",
        prompt="Apply",
        resume_pdf_path="/tmp/resume.pdf",
    )

    assert result == "failed:openai_computer_use_unavailable"


def test_access_check_reports_unavailable_model(monkeypatch):
    def fake_require_openai_key():
        return "test-key"

    class FakeResponse:
        status_code = 404

        def raise_for_status(self):
            return None

    monkeypatch.setattr("applypilot.apply.openai_runner._require_openai_key", fake_require_openai_key)
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: FakeResponse())

    ok, reason = check_openai_computer_use_access("computer-use-preview")

    assert not ok
    assert "unavailable" in reason
