from applypilot.llm import LLMClient


class _FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


def test_openai_gpt5_uses_max_completion_tokens(monkeypatch):
    captured = {}
    client = LLMClient("https://api.openai.com/v1", "gpt-5.4-mini", "test-key")

    def fake_post(url, json, headers):
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(client._client, "post", fake_post)

    assert client.chat([{"role": "user", "content": "Reply ok"}], max_tokens=8) == "ok"
    assert captured["json"]["max_completion_tokens"] == 8
    assert "max_tokens" not in captured["json"]


def test_openai_gpt41_keeps_max_tokens(monkeypatch):
    captured = {}
    client = LLMClient("https://api.openai.com/v1", "gpt-4.1-mini", "test-key")

    def fake_post(url, json, headers):
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(client._client, "post", fake_post)

    assert client.chat([{"role": "user", "content": "Reply ok"}], max_tokens=8) == "ok"
    assert captured["json"]["max_tokens"] == 8
    assert "max_completion_tokens" not in captured["json"]
