import pytest
from langsmith import tracing_context

from app.core.config import settings
from app.languages import isolation, parser_cache

collect_ignore = ["fixtures"]

pytest_plugins = ["tests.support.persistence", "tests.support.source_fixtures"]


@pytest.fixture(autouse=True)
def isolated_review_tracing(monkeypatch):
    monkeypatch.setattr(settings, "rivu_langsmith_tracing", False)
    monkeypatch.setattr(settings, "rivu_langsmith_capture_content", False)
    for name in ("RIVU_LANGSMITH_TRACING", "RIVU_LANGSMITH_CAPTURE_CONTENT",
                 "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.setenv(name, "false")
    with tracing_context(enabled=False, parent=False):
        yield


@pytest.fixture(autouse=True)
def isolated_parser_cache(tmp_path, monkeypatch):
    directory = tmp_path / ".rivu"
    monkeypatch.setattr(parser_cache, "_cache_path", lambda: directory / "parser-cache.db")
    monkeypatch.setattr(isolation, "_failure_directory", lambda: directory / "parser-failures")
    isolation._RECENT_FAILURES.clear()
    yield
    isolation._RECENT_FAILURES.clear()
