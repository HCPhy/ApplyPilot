import gzip

from applypilot.discovery import public_boards
from applypilot.discovery.public_boards import source_default_queries


class _FakeHeaders:
    def get(self, key, default=None):
        if key.lower() == "content-encoding":
            return "gzip"
        return default

    def get_content_charset(self):
        return "utf-8"


class _FakeResponse:
    headers = _FakeHeaders()

    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


def test_fetch_text_decompresses_gzip(monkeypatch):
    def fake_urlopen(req, timeout=30):
        return _FakeResponse(gzip.compress(b"<html>ok</html>"))

    monkeypatch.setattr(public_boards, "_urlopen", fake_urlopen)

    assert public_boards.fetch_text("https://example.com") == "<html>ok</html>"


def test_source_default_queries_fill_specialized_board_terms():
    board = {
        "default_queries": [
            {"query": "postdoc", "tier": 1},
            {"query": "research fellow", "tier": 2},
            {"query": "faculty", "tier": 3},
        ]
    }

    queries = source_default_queries(
        "academicjobsonline",
        board,
        {"academicjobsonline_max_tier": 2},
        "academicjobsonline_max_tier",
    )

    assert queries == ["postdoc", "research fellow"]


def test_source_default_queries_can_be_disabled_per_provider():
    queries = source_default_queries(
        "academicjobsonline",
        {"default_queries": ["postdoc"]},
        {"academicjobsonline_use_default_queries": False},
        "academicjobsonline_max_tier",
    )

    assert queries == []
