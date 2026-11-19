import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.models.pr_discussion import DiscussionPolicy
from app.review.context import AgentContext
from app.review.index import OnDemandIndex
from app.review.tools import find_relations, read_diff, read_discussion, read_file, read_symbol
from app.services.review.review_service import ReviewService
from app.tools.github.github_discussions import GitHubDiscussionReader
from tests.review.vnext_helpers import setup_source_run


def runtime_for(store, run, provider):
    budget = SimpleNamespace(active=AsyncMock())
    return SimpleNamespace(context=AgentContext(provider, store.work_items(run.id)[0], budget))


@pytest.mark.parametrize("direction,expected", [
    ("in", {("one/client.py", "a.py")}),
    ("out", {("a.py", "one/helpers.py")}),
    ("both", {("one/client.py", "a.py"), ("a.py", "one/helpers.py")}),
])
async def test_relation_scope_is_independent_of_prior_queries(tmp_path, direction, expected):
    store, run, _, provider, _ = setup_source_run(
        tmp_path, head_source=(
            "from one.helpers import numerator\nfrom other.helpers import denominator\n\n"
            "def local(x):\n    return x\n\n"
            "def ratio(x):\n    return numerator(x) / denominator(local(x))\n"
        ), support_sources={
            "one/client.py": "from a import ratio\ndef first():\n    return ratio(1)\n",
            "other/client.py": "from a import ratio\ndef second():\n    return ratio(1)\n",
            "one/helpers.py": "def numerator(x):\n    return x\n",
            "other/helpers.py": "def denominator(x):\n    return x\n",
        },
    )
    provider.index = OnDemandIndex(provider.evidence)
    runtime = runtime_for(store, run, provider)
    args = dict(path="a.py", symbol="ratio", direction=direction, runtime=runtime)
    narrow = await find_relations.coroutine(**args, scope="one/")
    parsed_narrow = {path for _, path in provider.index.analyses}
    broad = await find_relations.coroutine(**args)
    # Read the same scope from a fresh attempt after warming the shared cache.
    args["runtime"] = runtime_for(store, run, provider)
    again = await find_relations.coroutine(**args, scope="one")
    assert narrow == again
    assert {(edge["source_file"], edge["target_file"]) for edge in narrow["edges"]} == expected
    assert len(broad["edges"]) > len(narrow["edges"])
    assert all(path == "a.py" or path.startswith("one/") for path in parsed_narrow)
    assert not again["scope_complete"]


@pytest.mark.parametrize("scope", ["", "clients"])
async def test_incoming_relation_respects_import_binding_to_unparsed_module(tmp_path, scope):
    store, run, _, provider, _ = setup_source_run(
        tmp_path, head_source="import helpers\n\ndef ratio(x):\n    return helpers.ratio(x)\n",
        support_sources={
            "clients/other.py": "import a\nimport helpers\n\ndef other():\n    return helpers.ratio(1)\n",
            "clients/real.py": "import a\n\ndef real():\n    return a.ratio(1)\n",
            "helpers.py": "def ratio(x):\n    return x\n",
        },
    )
    provider.index = OnDemandIndex(provider.evidence)
    runtime = runtime_for(store, run, provider)
    args = dict(path="a.py", symbol="ratio", scope=scope, runtime=runtime)
    for direction in ("in", "both"):
        result = await find_relations.coroutine(**args, direction=direction)
        incoming = [edge for edge in result["edges"] if edge["target_file"] == "a.py"]
        assert [(edge["source_file"], edge["resolution_status"]) for edge in incoming] == [
            ("clients/real.py", "resolved"),
        ]
    if scope:
        assert ("head", "helpers.py") not in provider.index.analyses


async def test_read_symbol_pages_full_range_with_fixed_source_refs(tmp_path):
    source = "def long():\n" + "    value = 1\n" * 250 + "    return value\n"
    store, run, _, provider, _ = setup_source_run(tmp_path, head_source=source)
    provider.index = OnDemandIndex(provider.evidence)
    runtime = runtime_for(store, run, provider)
    first = await read_symbol.coroutine(path="a.py", symbol="long", runtime=runtime)
    assert first["ok"] and first["truncated"] and first["next_start"] == 241
    assert first["symbol_range"] == [1, 252]
    assert first["source"]["end_line"] == 240
    second = await read_symbol.coroutine(path="a.py", symbol="long", start=first["next_start"],
                                         runtime=runtime)
    assert second["ok"] and not second["truncated"] and second["next_start"] is None
    assert second["symbol_range"] == first["symbol_range"]
    assert first["content"] + second["content"] == source
    assert len(runtime.context.pending) == 2
    for ref in runtime.context.pending.values():
        assert provider.evidence.resolve(ref.id) == ref


@pytest.mark.parametrize(("path", "symbol", "error"), [
    ("a.py", "missing", "symbol_not_found"),
    ("a.py", "duplicate", "symbol_ambiguous"),
    ("notes.txt", "missing", "parser_unavailable"),
])
async def test_read_symbol_reports_distinct_failures(tmp_path, path, symbol, error):
    store, run, _, provider, _ = setup_source_run(
        tmp_path, head_source="def duplicate():\n    pass\ndef duplicate():\n    pass\n",
        support_sources={"notes.txt": "text\n"},
    )
    provider.index = OnDemandIndex(provider.evidence)
    runtime = runtime_for(store, run, provider)
    result = await read_symbol.coroutine(path=path, symbol=symbol, runtime=runtime)
    assert result["ok"] is False and result["error"] == error
    if error == "symbol_ambiguous":
        assert [item["range"] for item in result["matches"]] == [[1, 2], [3, 4]]
    assert runtime.context.pending == {}


async def test_read_file_bounded_mode_preserves_whole_lines_and_exact_mode(tmp_path):
    store, run, _, provider, _ = setup_source_run(tmp_path, head_source="12345\n67890\nlast\n")
    provider.evidence.max_output_chars = 11
    runtime = runtime_for(store, run, provider)
    exact = await read_file.coroutine(path="a.py", revision="head", start=1, end=2, runtime=runtime)
    assert exact["ok"] is False and exact["error"] == "source_output_too_large"
    page = await read_file.coroutine(path="a.py", revision="head", start=1, end=None, runtime=runtime)
    assert page["ok"] and page["truncated"] and page["next_start"] == 2
    assert page["content"] == "12345\n" and page["source"]["end_line"] == 1
    tail = await read_file.coroutine(path="a.py", revision="head", start=2, end=None, runtime=runtime)
    assert tail["ok"] and not tail["truncated"] and tail["next_start"] is None
    assert tail["content"] == "67890\nlast\n" and tail["source"]["end_line"] == 3


async def test_read_diff_success_stages_fixed_unit_material(tmp_path):
    store, run, _, provider, _ = setup_source_run(tmp_path)
    runtime = runtime_for(store, run, provider)
    key = run.plan.units[0].required_hunk_ids[0]
    result = await read_diff.coroutine(hunk_id=key, runtime=runtime)
    assert result["ok"] and result["member"]["id"] == key
    assert {item["source"]["side"] for item in result["sources"]} == {"base", "head"}
    assert key in runtime.context.pending_hunks
    other = await read_diff.coroutine(hunk_id="other-unit", runtime=runtime)
    assert other["ok"] is False


async def test_initial_discussion_directory_prioritizes_unit_and_excludes_bodies(tmp_path):
    store, run, _, provider, _ = setup_source_run(tmp_path)
    run.configuration.options.include_pr_discussions = True
    provider.discussions = [
        {"source": "inline", "path": "outside.py"},
        {"source": "review", "path": None},
        {"source": "inline", "path": "a.py", "resolved": False, "outdated": True,
         "complete": False, "comments": [{"body": "private discussion body"}]},
        {"source": "inline", "path": ".env"},
        *[{"source": "inline", "path": "a.py", "complete": True} for _ in range(9)],
    ]
    provider.discussion_sources = {"inline": "partial", "review": "complete"}
    runtime = runtime_for(store, run, provider)
    packet = await provider.initial(runtime.context)
    directory = packet["discussion_directory_untrusted"]
    assert directory["frozen_thread_count"] == 13
    assert directory["sources"]["inline"] == "partial"
    assert directory["truncated"]
    assert len(directory["threads"]) <= 8
    assert directory["threads"][0] == {
        "index": 2, "source": "inline", "path": "a.py", "resolved": False,
        "outdated": True, "complete": False,
    }
    assert "private discussion body" not in json.dumps(packet)
    assert ".env" not in json.dumps(directory)
    discussion = await read_discussion.coroutine(index=directory["threads"][0]["index"], runtime=runtime)
    assert discussion["ok"] and discussion["partial"]
    assert discussion["source"] == "untrusted_discussion" and "source_ref" not in discussion
    assert json.loads(discussion["content"])["comments"][0]["body"] == "private discussion body"
    denied = await read_discussion.coroutine(index=3, runtime=runtime)
    assert denied["ok"] is False

    # The optional directory uses only the space remaining after fixed materials.
    without_directory = {key: value for key, value in packet.items() if key != "discussion_directory_untrusted"}
    baseline_size = len(json.dumps(without_directory))
    constrained = await provider.initial(runtime_for(store, run, provider).context,
        request_budget=baseline_size, estimate_request=lambda item: len(json.dumps(item)))
    assert constrained == without_directory


@pytest.mark.parametrize("limit", [1, 2])
async def test_rest_discussion_completeness_reaches_directory_and_receipt(tmp_path, limit):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    run.configuration.options.include_pr_discussions = True
    pages = {
        1: [{"id": 1, "path": "a.py", "body": "Thread root"}],
        2: [{"id": 2, "in_reply_to_id": 1, "path": "a.py", "body": "Reply"}],
    }

    async def get(url, headers, *, params):
        comments = pages.get(params["page"], []) if url.endswith("/pulls/1/comments") else []
        return httpx.Response(200, json=comments)

    reader = GitHubDiscussionReader(SimpleNamespace(_get=get),
                                    DiscussionPolicy(page_size=1, max_comments=limit))
    snapshot = await reader.collect(task_id=run.id, pr_url=run.pr_url,
        head_sha=run.snapshot.head_sha, base_sha=run.snapshot.base_sha,
        owner=run.pr.owner, repo=run.pr.repo, number=run.pr.number)
    assert snapshot.sources["inline"] == "partial"
    assert len(snapshot.threads[0].comments) == limit
    assert snapshot.threads[0].complete is (limit == 2)
    artifact = provider.evidence.artifacts.put_text(task_id=run.id, kind="discussions",
                                                   content=snapshot.model_dump_json())
    run.discussion_uri = artifact.uri
    service = ReviewService(None, provider.evidence.git, None, None, store,
                            artifacts=provider.evidence.artifacts)
    frozen = await service._discussions(run, lease)
    provider.discussions, provider.discussion_sources = frozen["threads"], frozen["sources"]
    runtime = runtime_for(store, run, provider)
    packet = await provider.initial(runtime.context)
    thread = packet["discussion_directory_untrusted"]["threads"][0]
    assert thread["complete"] is (limit == 2)
    receipt = await read_discussion.coroutine(index=thread["index"], runtime=runtime)
    assert receipt["ok"] and receipt["partial"] is (limit == 1)
    assert json.loads(receipt["content"])["complete"] is (limit == 2)


async def test_rest_cutoff_reopens_graphql_thread_only_for_uncollected_reply():
    async def get(url, headers, *, params):
        comments = [
            {"id": 1, "path": "a.py", "body": "First root"},
            {"id": 2, "path": "a.py", "body": "Second root"},
            {"id": 3, "in_reply_to_id": 2, "path": "a.py", "body": "New reply"},
        ] if url.endswith("/pulls/1/comments") else []
        return httpx.Response(200, json=comments)

    reader = GitHubDiscussionReader(SimpleNamespace(_token="fixture", _get=get),
                                    DiscussionPolicy(max_comments=2))
    threads = [
        {"id": f"thread:{index}", "path": "a.py", "isResolved": False, "isOutdated": False,
         "comments": {"pageInfo": {"hasNextPage": False},
                      "nodes": [{"databaseId": index, "body": "Root"}]}}
        for index in (1, 2)
    ]
    first_page = {"repository": {"pullRequest": {"reviewThreads": {
        "nodes": threads, "pageInfo": {"hasNextPage": True, "endCursor": "another-page"},
    }}}}
    reader.graphql = AsyncMock(side_effect=[first_page, ValueError("graphql_unavailable")])
    snapshot = await reader.collect(task_id="run", pr_url="https://example.test/pull/1",
        head_sha="b" * 40, base_sha="a" * 40, owner="o", repo="r", number=1)
    assert "inline:collection_limit" in snapshot.limitations
    assert [(thread.id, thread.complete, len(thread.comments)) for thread in snapshot.threads] == [
        ("inline:1", True, 1), ("inline:2", False, 1),
    ]


async def test_frozen_truncated_rest_snapshot_corrects_legacy_thread_flag(tmp_path):
    store, run, lease, provider, _ = setup_source_run(tmp_path)
    frozen = {
        "head_sha": run.snapshot.head_sha, "base_sha": run.snapshot.base_sha,
        "sources": {"inline": "partial"},
        "limitations": ["inline_thread_state_unavailable", "inline:collection_limit"],
        "threads": [
            {"source": "inline", "complete": True, "resolved": None, "outdated": None},
            {"source": "inline", "complete": True, "resolved": False, "outdated": False},
            {"source": "review", "complete": True},
        ],
    }
    artifact = provider.evidence.artifacts.put_text(task_id=run.id, kind="discussions",
                                                   content=json.dumps(frozen))
    run.discussion_uri = artifact.uri
    service = ReviewService(None, provider.evidence.git, None, None, store,
                            artifacts=provider.evidence.artifacts)
    result = await service._discussions(run, lease)
    assert [thread["complete"] for thread in result["threads"]] == [False, True, True]
    assert json.loads(provider.evidence.artifacts.read_text(artifact.uri)) == frozen
