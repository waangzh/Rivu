import asyncio
import hashlib
import json
import subprocess
from types import SimpleNamespace
import pytest
from app.review.retrieval import _paths, find_paths, list_directory, search
from tests.support.source_fixtures import commit_sources

def context_for(service):
    return SimpleNamespace(evidence=service, denied_paths=frozenset(), check_active=None, cursors={})


def test_path_globs_keep_components_and_support_explicit_exclusions(source_service):
    source_service.allowed_paths = frozenset({
        "src/api.ts", "src/server/handlers.ts", "src/server/__tests__/handlers.test.ts",
        "src/server/deeper/router.ts", ".env",
    })
    context = context_for(source_service)
    assert _paths(context, glob="src/*.ts") == ["src/api.ts"]
    assert _paths(context, glob="src/**/*.ts", exclude_globs=["**/__tests__/**"]) == [
        "src/api.ts", "src/server/deeper/router.ts", "src/server/handlers.ts"]
    assert _paths(context, glob="*.test.ts") == ["src/server/__tests__/handlers.test.ts"]
    with pytest.raises(ValueError, match="unsupported_glob"):
        _paths(context, glob="!**/__tests__/**")
    with pytest.raises(ValueError, match="unsupported_glob"):
        _paths(context, exclude_globs=["!**/__tests__/**"])
    for invalid in (["../*"], [""] , ["*.ts"] * 17):
        with pytest.raises(ValueError):
            _paths(context, exclude_globs=invalid)


async def test_search_exclusions_are_visible_and_bound_to_cursor(source_service):
    commit_sources(source_service, {
        "src/__tests__/rule.test.ts": "VALUE\n",
        "src/rule.ts": "VALUE\nVALUE\n",
    })
    context = context_for(source_service)
    first = await search(context, "VALUE", path="src", glob="*.ts", limit=1,
                         exclude_globs=["**/__tests__/**"])
    assert first["matches"][0]["path"] == "src/rule.ts"
    assert first["filters"]["exclude_globs"] == ["**/__tests__/**"]
    assert not first["scope_empty"] and first["scope_files"] == 1
    with pytest.raises(ValueError, match="invalid_cursor"):
        await search(context, "VALUE", path="src", glob="*.ts", limit=1,
                     cursor=first["next_cursor"])
    final = await search(context, "VALUE", path="src", glob="*.ts", limit=1,
                         cursor=first["next_cursor"], exclude_globs=["**/__tests__/**"])
    assert final["scope_complete"] and final["matches"][0]["line"] == 2
    empty = await search(context, "VALUE", glob="missing/**/*.ts")
    assert empty["scope_empty"] and empty["matches"] == [] and empty["scope_files"] == 0
    assert empty["filters"]["glob"] == "missing/**/*.ts"


def test_path_exclusion_changes_invalidate_inventory_cursor(source_service):
    source_service.allowed_paths = frozenset({"src/a.ts", "src/b.ts", "src/tests/a.ts"})
    context = context_for(source_service)
    first = find_paths(context, path="src", limit=1, exclude_globs=["**/tests/**"])
    with pytest.raises(ValueError, match="invalid_cursor"):
        find_paths(context, path="src", limit=1, cursor=first["next_cursor"])
    final = find_paths(context, path="src", limit=1, cursor=first["next_cursor"],
                       exclude_globs=["**/tests/**"])
    assert final["paths"] == ["src/b.ts"] and not final["has_more"]



async def test_late_file_and_in_file_hits_are_reachable_at_fixed_snapshot(source_service, monkeypatch):
    texts = {f"pkg/f{index:03d}.py": "unrelated\n" for index in range(130)}
    texts["pkg/f129.py"] = "NEEDLE\n" * 27
    commit_sources(source_service, texts)
    (source_service.repo_path / "pkg/f129.py").write_text("dirty tree\n", encoding="utf-8")
    context = context_for(source_service)
    searches = []
    original = source_service.git.find_matching_paths

    def located(*args):
        searches.append(args[2])
        return original(*args)

    monkeypatch.setattr(source_service.git, "find_matching_paths", located)
    pages, cursor = [], ""
    while True:
        page = await search(context, "NEEDLE", cursor=cursor)
        assert page["ok"] and not page["truncated"] and "evidence" not in page
        assert len(json.dumps(page)) < 24000
        pages.append(page)
        if not page["has_more"]:
            break
        assert len(pages) < 10
        cursor = page["next_cursor"]
    assert pages[0]["matches"][0]["path"] == "pkg/f129.py" and pages[0]["has_more"]
    assert [item["line"] for page in pages for item in page["matches"]] == list(range(1, 28))
    assert all(item["path"] == "pkg/f129.py" for page in pages for item in page["matches"])
    assert searches == ["NEEDLE"]  # Subsequent hit pages reuse the signed bounded search window.
    filtered = await search(context, "NEEDLE", path="pkg", glob="*129.py", limit=40)
    assert len(filtered["matches"]) == 27 and not filtered["has_more"]
    base = await search(context, "OLD", path="removed.py", side="base")
    assert base["matches"][0]["line"] == 1
    head = await search(context, "OLD", path="removed.py")
    assert head["matches"] == [] and head["unreadable_files"] == 0


async def test_path_pagination_query_and_snapshot_binding(source_service):
    source_service.allowed_paths = frozenset(f"pkg/f{i:03d}.py" for i in range(170))
    context = context_for(source_service)
    pages, cursor = [], ""
    while True:
        page = find_paths(context, path="pkg", glob="*.py", cursor=cursor)
        pages.extend(page["paths"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert pages == sorted(source_service.allowed_paths)
    first = find_paths(context)
    for kwargs in ({"query": "f"}, {"path": "pkg"}, {"glob": "*.py"}):
        with pytest.raises(ValueError, match="invalid_cursor"):
            find_paths(context, cursor=first["next_cursor"], **kwargs)
    source_service.snapshot = source_service.snapshot.model_copy(update={"head_sha": "f" * 40})
    with pytest.raises(ValueError, match="invalid_cursor"):
        find_paths(context, cursor=first["next_cursor"])


async def test_unreadable_earlier_page_and_cursor_restart_preserve_scope(source_service):
    texts = {f"pkg/f{index:03d}.py": "no match\n" for index in range(125)}
    texts["pkg/f000.py"] = "large" * 20
    texts["pkg/f124.py"] = "NEEDLE\nNEEDLE\n"
    commit_sources(source_service, texts)
    source_service.max_file_bytes = 50
    context = context_for(source_service)
    first = await search(context, "NEEDLE", path="pkg", limit=1)
    assert first["has_more"] and first["unreadable_files"] == 1
    # A recreated invocation shares only the immutable archive, not in-memory cursor state.
    restarted = context_for(source_service)
    with pytest.raises(ValueError, match="invalid_cursor"):
        await search(restarted, "NEEDLE", path="pkg", cursor=first["next_cursor"], limit=1)
    last = await search(context, "NEEDLE", path="pkg", cursor=first["next_cursor"], limit=1)
    assert not last["has_more"] and last["matches"][0]["path"] == "pkg/f124.py"
    assert last["unreadable_files"] == 0 and not last["scope_complete"]


async def test_search_cursor_filters_ranges_and_authorization(source_service):
    commit_sources(source_service, {"many.py": "VALUE\n" * 20})
    context = context_for(source_service)
    context.denied_paths = frozenset({"safe.py"})
    first = await search(context, "VALUE", path="many.py")
    for kwargs in ({"query": "OTHER", "path": "many.py"},
                   {"query": "VALUE", "path": "many.py", "side": "base"},
                   {"query": "VALUE", "path": "many.py", "glob": "*.py"}):
        with pytest.raises(ValueError, match="invalid_cursor"):
            await search(context, cursor=first["next_cursor"], **kwargs)
    for cursor in ("!", hashlib.sha256(b'{"column":0,"position":1,"scope":"' +
                                       b"0" * 64 + b'"}').hexdigest()):
        with pytest.raises(ValueError, match="invalid_cursor"):
            await search(context, "VALUE", path="many.py", cursor=cursor)
    for path in ("../safe.py", ".env", "safe.py", "/absolute", "C:/file"):
        with pytest.raises(ValueError):
            await search(context, "VALUE", path=path)
    with pytest.raises(ValueError):
        await search(context, "VALUE", limit=0)
    with pytest.raises(ValueError):
        await search(context, "VALUE", glob="../*")
    with pytest.raises(ValueError):
        await search(context, "VALUE\n")


async def test_large_binary_unreadable_and_empty_scopes_fail_closed(source_service, monkeypatch):
    commit_sources(source_service, {"large.py": "SECRET\n" * 50, "blob.dat": "binary\x00SECRET"})
    source_service.max_file_bytes = 100
    context = context_for(source_service)
    result = await search(context, "SECRET", path="large.py")
    assert result["matches"] == [] and result["unreadable_files"] == 1
    assert not result["scope_complete"]
    result = await search(context, "SECRET", path="blob.dat")
    assert result["matches"] == []
    assert source_service.git.find_matching_paths(source_service.repo_path,
        source_service.snapshot.head_sha, "VALUE", []) == set()
    source_service.max_file_bytes = 512_000
    context.evidence.allowed_paths = frozenset(
        {f"pkg/{index:02d}.py" for index in range(19)} | {"pkg/zz.py"})
    for index in range(19):
        target = source_service.repo_path / f"pkg/{index:02d}.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x" * 450_000, encoding="utf-8")
    target = source_service.repo_path / "pkg/zz.py"
    target.write_text("TARGET\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source_service.repo_path), "add", "."],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source_service.repo_path), "-c", "user.name=Test",
        "-c", "user.email=test@example.test", "commit", "-m", "large files"],
        check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(source_service.repo_path), "rev-parse", "HEAD"],
                          check=True, capture_output=True).stdout.decode().strip()
    source_service.snapshot = source_service.snapshot.model_copy(update={"head_sha": head})
    pages = []
    cursor = ""
    while True:
        page = await search(context, "TARGET", path="pkg", cursor=cursor)
        pages.append(page)
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert [match["path"] for page in pages for match in page["matches"]] == ["pkg/zz.py"]
    assert all(page["scope_complete"] is False for page in pages[:-1])
    assert pages[-1]["scope_complete"] is True

    def timeout(*args):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(source_service.git, "find_matching_paths", timeout)
    with pytest.raises(ValueError, match="snapshot_search_timeout"):
        await search(context, "VALUE")


async def test_search_cancellation_is_propagated(source_service):
    async def cancelled():
        raise asyncio.CancelledError()

    context = context_for(source_service)
    context.check_active = cancelled
    with pytest.raises(asyncio.CancelledError):
        await search(context, "VALUE")


async def test_search_supports_ignore_case_word_boundary_regex_context_and_files_only(source_service):
    commit_sources(source_service, {
        "src/a.py": "alpha = 1\nFoo = 2\nfoobar = 3\nbeta = 4\n",
        "src/b.py": "foo = 10\nfoo = 20\nbar_item = 30\n",
        "src/c.py": "only_bar = 1\n",
    })
    context = context_for(source_service)

    case_hits = await search(context, "foo", path="src", ignore_case=True, word_boundary=True)
    assert [(m["path"], m["line"]) for m in case_hits["matches"]] == [
        ("src/a.py", 2), ("src/b.py", 1), ("src/b.py", 2),
    ]
    assert case_hits["filters"]["ignore_case"] is True
    assert case_hits["filters"]["word_boundary"] is True

    ctx_hits = await search(context, "Foo", path="src", context_lines=1)
    assert len(ctx_hits["matches"]) == 1
    assert ctx_hits["matches"][0]["before"] == ["alpha = 1\n"]
    assert ctx_hits["matches"][0]["after"] == ["foobar = 3\n"]

    regex_hits = await search(context, "Foo|bar_item", path="src", regex=True)
    assert [(m["path"], m["line"]) for m in regex_hits["matches"]] == [
        ("src/a.py", 2), ("src/b.py", 3),
    ]

    grouped_first = await search(context, "foo", path="src", ignore_case=True,
                                 files_only=True, limit=1)
    assert grouped_first["has_more"] is True
    assert grouped_first["matches"] == [{
        "path": "src/a.py", "match_count": 2, "first_line": 2, "sample": "Foo = 2\n", "side": "head",
    }]
    # Changing search option invalidates the cursor.
    with pytest.raises(ValueError, match="invalid_cursor"):
        await search(context, "foo", path="src", ignore_case=True,
                     files_only=False, limit=1, cursor=grouped_first["next_cursor"])
    grouped_second = await search(context, "foo", path="src", ignore_case=True,
                                  files_only=True, limit=1, cursor=grouped_first["next_cursor"])
    assert grouped_second["scope_complete"] is True
    assert grouped_second["matches"] == [{
        "path": "src/b.py", "match_count": 2, "first_line": 1, "sample": "foo = 10\n", "side": "head",
    }]

    with pytest.raises(ValueError, match="invalid_regex"):
        await search(context, "(?P<x>foo)", regex=True)
    with pytest.raises(ValueError, match="invalid_regex"):
        await search(context, "[unclosed", regex=True)
    with pytest.raises(ValueError, match="invalid_context_lines"):
        await search(context, "foo", context_lines=4)


async def test_regex_search_uses_single_posix_ere_engine_for_files_and_lines(source_service):
    commit_sources(source_service, {
        "src/nums.py": "id=42\nname=alice\ncode=99\nmark=(x?y)\n",
    })
    context = context_for(source_service)

    hits = await search(context, "[[:digit:]]+", path="src", regex=True)
    assert hits["scope_complete"] is True
    assert [(m["path"], m["line"], m["text"]) for m in hits["matches"]] == [
        ("src/nums.py", 1, "id=42\n"),
        ("src/nums.py", 3, "code=99\n"),
    ]

    # Valid POSIX ERE character class containing '(' and '?' must not be rejected by substring heuristics.
    char_class = await search(context, "[(?]", path="src", regex=True)
    assert char_class["scope_complete"] is True
    assert [(m["path"], m["line"]) for m in char_class["matches"]] == [("src/nums.py", 4)]

    grouped = await search(context, "[[:digit:]]+", path="src", regex=True, files_only=True)
    assert grouped["scope_complete"] is True
    assert grouped["matches"] == [{
        "path": "src/nums.py", "match_count": 2, "first_line": 1, "sample": "id=42\n", "side": "head",
    }]


async def test_regex_search_respects_work_deadline_and_cancellation(source_service, monkeypatch):
    from datetime import timedelta
    from app.review.domain import utcnow

    commit_sources(source_service, {"src/a.py": "value=123\n"})
    expired_context = SimpleNamespace(
        evidence=source_service,
        denied_paths=frozenset(),
        check_active=None,
        cursors={},
        work=SimpleNamespace(id="w1"),
        provider=SimpleNamespace(run=SimpleNamespace(
            work_deadline=lambda work: utcnow() - timedelta(seconds=1),
        )),
    )
    with pytest.raises(ValueError, match="snapshot_search_timeout"):
        await search(expired_context, "value", path="src", regex=True)

    # If metadata lookup exhausts the shared absolute deadline, grep must not run afterwards.
    grep_called = False
    original_tree = source_service.git.get_tree_entries
    original_paths = source_service.git.find_matching_paths
    original_lines = source_service.git.find_matching_lines

    def slow_tree(*args, **kwargs):
        deadline = kwargs.get("deadline")
        assert deadline is not None
        import time as _time
        _time.sleep(0.05)
        return original_tree(*args, **kwargs)

    def spy_grep(*args, **kwargs):
        nonlocal grep_called
        grep_called = True
        return set()

    monkeypatch.setattr(source_service.git, "get_tree_entries", slow_tree)
    monkeypatch.setattr(source_service.git, "find_matching_paths", spy_grep)
    monkeypatch.setattr(source_service.git, "find_matching_lines", spy_grep)
    tight_context = SimpleNamespace(
        evidence=source_service,
        denied_paths=frozenset(),
        check_active=None,
        cursors={},
        work=SimpleNamespace(id="w2"),
        provider=SimpleNamespace(run=SimpleNamespace(
            work_deadline=lambda work: utcnow() + timedelta(milliseconds=20),
        )),
    )
    with pytest.raises(ValueError, match="snapshot_search_timeout"):
        await search(tight_context, "value", path="src", regex=True)
    assert grep_called is False
    monkeypatch.setattr(source_service.git, "get_tree_entries", original_tree)
    monkeypatch.setattr(source_service.git, "find_matching_paths", original_paths)
    monkeypatch.setattr(source_service.git, "find_matching_lines", original_lines)

    # If post-hit blob read exceeds the remaining work deadline, files_only must time out instead of returning complete.
    original_show = source_service.git.get_file_content_at_revision

    def slow_show(*args, **kwargs):
        import time as _time
        _time.sleep(0.08)
        return original_show(*args, **kwargs)

    monkeypatch.setattr(source_service.git, "get_file_content_at_revision", slow_show)
    slow_blob_context = SimpleNamespace(
        evidence=source_service,
        denied_paths=frozenset(),
        check_active=None,
        cursors={},
        work=SimpleNamespace(id="w3"),
        provider=SimpleNamespace(run=SimpleNamespace(
            work_deadline=lambda work: utcnow() + timedelta(milliseconds=40),
        )),
    )
    with pytest.raises(ValueError, match="snapshot_search_timeout"):
        await search(slow_blob_context, "value", path="src", regex=True, files_only=True)
    monkeypatch.setattr(source_service.git, "get_file_content_at_revision", original_show)

    loop = asyncio.get_running_loop()
    observed_cancel = []
    started = asyncio.Event()

    def slow_lines(*args, **kwargs):
        cancel_event = kwargs.get("cancel_event")
        observed_cancel.append(cancel_event)
        loop.call_soon_threadsafe(started.set)
        assert cancel_event is not None and cancel_event.wait(timeout=2.0)
        from app.tools.git_tool import GitToolCancelled
        raise GitToolCancelled("cancelled")

    monkeypatch.setattr(source_service.git, "find_matching_lines", slow_lines)
    context = context_for(source_service)
    task = asyncio.create_task(search(context, "value", path="src", regex=True))
    await asyncio.wait_for(started.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert observed_cancel and observed_cancel[0].is_set()
    monkeypatch.setattr(source_service.git, "find_matching_lines", original_lines)

    # Default literal branch also propagates cancel_event into find_matching_paths.
    literal_cancel = []
    literal_started = asyncio.Event()

    def slow_paths(*args, **kwargs):
        cancel_event = kwargs.get("cancel_event")
        literal_cancel.append(cancel_event)
        loop.call_soon_threadsafe(literal_started.set)
        assert cancel_event is not None and cancel_event.wait(timeout=2.0)
        from app.tools.git_tool import GitToolCancelled
        raise GitToolCancelled("cancelled")

    monkeypatch.setattr(source_service.git, "find_matching_paths", slow_paths)
    literal_task = asyncio.create_task(search(context_for(source_service), "value", path="src"))
    await asyncio.wait_for(literal_started.wait(), timeout=2.0)
    literal_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await literal_task
    assert literal_cancel and literal_cancel[0].is_set()


async def test_search_bounds_hit_index_skips_single_page_cache_and_rebuilds_full_window(
    source_service, monkeypatch,
):
    commit_sources(source_service, {
        "src/01_a.py": "NEEDLE = 1\n",
        "src/02_b.py": "NEEDLE = 2\n",
        "src/03_c.py": "NEEDLE = 3\n",
        "src/04_dense.py": ("x\n" * 32_768),
    })
    context = context_for(source_service)

    # Single-page search (has_more=False) must NOT write to search_windows cache.
    single = await search(context, "NEEDLE", path="src", regex=True, limit=10)
    assert single["has_more"] is False and single["scope_complete"] is True
    assert len(getattr(context, "search_windows", {})) == 0

    # Per-page hit collection on a 65 KiB dense file never collects full-file line numbers.
    collected_sizes = []
    original_lines = source_service.git.find_matching_lines

    def spy_lines(*args, **kwargs):
        res = original_lines(*args, **kwargs)
        collected_sizes.append((kwargs.get("max_matches"), sum(len(v) for v in res.values())))
        return res

    monkeypatch.setattr(source_service.git, "find_matching_lines", spy_lines)
    dense_page = await search(context, "^x$", path="src/04_dense.py", regex=True, limit=5)
    assert len(dense_page["matches"]) == 5 and dense_page["has_more"] is True
    assert collected_sizes == [(6, 6)]
    assert len(context.search_windows) == 1
    assert "hit_lines" not in json.dumps(list(context.search_windows.values()))
    monkeypatch.setattr(source_service.git, "find_matching_lines", original_lines)

    # Byte-budgeted window cache skips oversized entries and evicts LRU entries within limit.
    from app.review import retrieval
    monkeypatch.setattr(retrieval, "MAX_SEARCH_WINDOW_CACHE_BYTES", 80)
    evict_ctx = context_for(source_service)
    p1 = await search(evict_ctx, "NEEDLE", path="src", regex=True, limit=1)
    assert [m["path"] for m in p1["matches"]] == ["src/01_a.py"]
    assert sum(item["bytes"] for item in evict_ctx.search_windows.values()) <= 80

    # Evicting cache, jumping to third cursor, then replaying second cursor must rebuild full window.
    replay_ctx = context_for(source_service)
    page1 = await search(replay_ctx, "NEEDLE", path="src", regex=True, limit=1)
    page2 = await search(replay_ctx, "NEEDLE", path="src", regex=True, limit=1, cursor=page1["next_cursor"])
    assert [m["path"] for m in page1["matches"]] == ["src/01_a.py"]
    assert [m["path"] for m in page2["matches"]] == ["src/02_b.py"]
    replay_ctx.search_windows.clear()

    page3 = await search(replay_ctx, "NEEDLE", path="src", regex=True, limit=1, cursor=page2["next_cursor"])
    assert [m["path"] for m in page3["matches"]] == ["src/03_c.py"]
    assert page3["has_more"] is False and page3["scope_complete"] is True

    replayed_page2 = await search(
        replay_ctx, "NEEDLE", path="src", regex=True, limit=1, cursor=page1["next_cursor"],
    )
    assert [m["path"] for m in replayed_page2["matches"]] == ["src/02_b.py"]
    assert replayed_page2["has_more"] is True and replayed_page2["scope_complete"] is False


async def test_regex_search_validates_syntax_on_empty_scope(source_service):
    context = context_for(source_service)
    with pytest.raises(ValueError, match="invalid_regex"):
        await search(context, "[", glob="missing/**/*.ts", regex=True)
    with pytest.raises(ValueError, match="invalid_regex"):
        source_service.git.find_matching_paths(
            source_service.repo_path, source_service.snapshot.head_sha, "[", [], regex=True,
        )
    with pytest.raises(ValueError, match="invalid_regex"):
        source_service.git.find_matching_lines(
            source_service.repo_path, source_service.snapshot.head_sha, "[", [], regex=True,
        )
    literal_empty = await search(context, "[", glob="missing/**/*.ts", regex=False)
    assert literal_empty["ok"] is True and literal_empty["scope_empty"] is True


async def test_search_cursors_do_not_persist_window_hit_lines_and_bound_extraction(source_service):
    commit_sources(source_service, {
        "src/big1.py": "NEEDLE = 1\n" * 4000,
        "src/big2.py": "NEEDLE = 2\n" * 4000,
    })
    context = context_for(source_service)
    cursor = ""
    for _ in range(3):
        page = await search(context, "NEEDLE", path="src", ignore_case=True, limit=5, cursor=cursor)
        assert len(page["matches"]) == 5 and page["has_more"] is True
        cursor = page["next_cursor"]
    serialized = json.dumps(context.cursors)
    assert "hit_lines" not in serialized
    assert len(serialized) < 2048

    # Evicting the in-memory window cache falls back transparently on continuation.
    context.search_windows.clear()
    continued = await search(context, "NEEDLE", path="src", ignore_case=True, limit=5, cursor=cursor)
    assert [m["line"] for m in continued["matches"]] == [16, 17, 18, 19, 20]

    # Single-file literal search with huge match count stays bounded per page.
    from app.review.retrieval import _extract_file_matches
    extracted = _extract_file_matches(
        source_service, "src/big1.py", "head", "NEEDLE", 0, max_items=4,
    )
    assert len(extracted) == 4


def test_find_matching_lines_parses_nul_delimited_paths_containing_newlines(source_service, monkeypatch):
    sha = source_service.snapshot.head_sha
    odd_path = "dir/line1\nline2.py"

    def fake_grep(command, *, deadline=None, cancel_event=None, on_stdout_chunk=None):
        payload = f"{sha}:{odd_path}\x007\x00hit_line_text\n".encode("utf-8")
        assert on_stdout_chunk is not None
        # Feed in small chunks splitting right across the embedded newline and NULs.
        for offset in range(0, len(payload), 5):
            on_stdout_chunk(payload[offset:offset + 5])
        return 0, b"", b""

    monkeypatch.setattr(source_service.git, "_run_grep_bytes", fake_grep)
    lines = source_service.git.find_matching_lines(
        source_service.repo_path, sha, "hit", [odd_path], regex=True,
    )
    assert lines == {odd_path: [7]}


def test_git_path_batches_respect_command_line_length_limit(source_service, monkeypatch):
    from app.tools.git_tool import _argv_chars, _iter_path_batches

    long_paths = [f"{'segment/' * 15}file_{index:03d}.py" for index in range(256)]
    assert all(len(path) >= 130 for path in long_paths)
    base_argv = ["git", "--literal-pathspecs", "-C", str(source_service.repo_path), "ls-tree",
                 "--full-tree", "-l", "-z", source_service.snapshot.head_sha, "--"]
    batches = list(_iter_path_batches(base_argv, long_paths))
    assert len(batches) >= 2
    assert sum(len(batch) for batch in batches) == 256
    assert all(_argv_chars([*base_argv, *batch]) <= 16_000 for batch in batches)

    commit_sources(source_service, {path: "NEEDLE\n" for path in long_paths})
    observed_ls_tree = []
    original_run = source_service.git._run

    def capture_run(command, **kwargs):
        observed_ls_tree.append(_argv_chars(command))
        return original_run(command, **kwargs)

    monkeypatch.setattr(source_service.git, "_run", capture_run)
    entries = source_service.git.get_tree_entries(
        source_service.repo_path, source_service.snapshot.head_sha, long_paths,
    )
    assert len(entries) == 256
    assert len(observed_ls_tree) >= 2
    assert all(length < 32_767 for length in observed_ls_tree)

    observed_grep = []
    original_grep = source_service.git._run_grep_bytes

    def capture_grep(command, **kwargs):
        observed_grep.append(_argv_chars(command))
        return original_grep(command, **kwargs)

    monkeypatch.setattr(source_service.git, "_run_grep_bytes", capture_grep)
    matched = source_service.git.find_matching_paths(
        source_service.repo_path, source_service.snapshot.head_sha, "NEEDLE", long_paths,
    )
    assert len(matched) == 256
    assert len(observed_grep) >= 2
    assert all(length < 32_767 for length in observed_grep)


def test_list_directory_groups_subtrees_respects_depth_exclusions_and_cursor(source_service):
    source_service.allowed_paths = frozenset({
        "README.md",
        ".env",
        "src/api.ts",
        "src/server/handlers.ts",
        "src/server/deep/router.ts",
        "src/server/__tests__/handlers.test.ts",
        "tests/unit.py",
    })
    context = context_for(source_service)

    top = list_directory(context, depth=1)
    assert top["ok"] is True and top["source"] == "dir_hint"
    assert top["entries"] == [
        {"path": "README.md", "kind": "file"},
        {"path": "src", "kind": "dir", "file_count": 4},
        {"path": "tests", "kind": "dir", "file_count": 1},
    ]
    assert top["scope_files"] == 6

    deeper = list_directory(context, path="src", depth=2, exclude_globs=["**/__tests__/**"])
    assert deeper["entries"] == [
        {"path": "src/api.ts", "kind": "file"},
        {"path": "src/server", "kind": "dir", "file_count": 2},
        {"path": "src/server/deep", "kind": "dir", "file_count": 1},
        {"path": "src/server/handlers.ts", "kind": "file"},
    ]

    page1 = list_directory(context, depth=1, limit=2)
    assert page1["has_more"] is True and len(page1["entries"]) == 2
    with pytest.raises(ValueError, match="invalid_cursor"):
        list_directory(context, depth=2, limit=2, cursor=page1["next_cursor"])
    page2 = list_directory(context, depth=1, limit=2, cursor=page1["next_cursor"])
    assert page2["has_more"] is False and len(page2["entries"]) == 1

    with pytest.raises(ValueError, match="invalid_depth"):
        list_directory(context, depth=0)
    with pytest.raises(ValueError, match="path_not_authorized"):
        list_directory(context, path=".env")


def test_batch_file_reader_skips_missing_binary_and_oversized_blobs(source_service):
    commit_sources(source_service, {
        "a.py": "alpha\n",
        "b.py": "beta\n" * 20,
        "bin.dat": "abc\x00def",
    })
    head = source_service.snapshot.head_sha
    loaded = source_service.git.get_files_content_at_revision(
        source_service.repo_path, head, ["a.py", "b.py", "bin.dat", "missing.py", "a.py"],
        max_file_bytes=40,
    )
    assert loaded == {"a.py": "alpha\n"}
    with pytest.raises(ValueError, match="invalid_batch_read_scope"):
        source_service.git.get_files_content_at_revision(
            source_service.repo_path, head, [".env"],
        )
