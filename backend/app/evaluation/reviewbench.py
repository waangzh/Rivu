"""Run Rivu on ReviewBench samples and reuse its official judging CLI."""

import argparse
import asyncio
import json
import os
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4


def sample_key(entry):
    return f"{entry['nwo'].replace('/', '_')}_{entry['pr_number']}-{entry['head'][:8]}"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def export_findings(run, entry, elapsed_ms):
    findings, skipped = [], []
    for finding in run.report.findings:
        location = finding.location
        if location is None or location.side != "head":
            skipped.append({"finding_id": finding.id, "reason": "no_head_location"})
            continue
        findings.append({
            "file": location.path, "start_line": location.line, "end_line": location.line,
            "message": "\n\n".join([finding.claim, f"触发条件：{finding.trigger}",
                                    f"影响：{finding.impact}"]),
            "producer": "rivu",
        })
    usage = {"time_in_ms": elapsed_ms}
    for source, target in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                           ("cached_input_tokens", "cached_tokens")):
        value = run.report.usage.get(source)
        if value is not None:
            usage[target] = value
    if "prompt_tokens" in usage and "completion_tokens" in usage:
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return {
        "pr": {key: entry[key] for key in ("repo", "pr_number", "base", "head")},
        "agent": "rivu", "findings": findings, "usage": usage,
    }, skipped


def prepare_repository(entry, directory, git):
    directory.mkdir(parents=True, exist_ok=True)
    command = [git._git, "-c", f"core.hooksPath={os.devnull}", "-C", str(directory)]
    if not (directory / ".git").exists():
        git._run([*command, "init", "--quiet", "--template="])
    mirror = f"https://github.com/review-bench/{entry['nwo'].replace('/', '_')}.git"
    from app.tools.git_tool import GitToolError
    for url in (mirror, entry["repo"] + ".git"):
        try:
            git._run([*command, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules",
                "--", url, f"{entry['base']}:refs/heads/reviewbench-base",
                f"{entry['head']}:refs/heads/reviewbench-head"])
            git._run([*command, "update-ref", "--no-deref", "HEAD", entry["head"]])
            return directory
        except GitToolError:
            if url != mirror:
                raise


async def review_sample(service, entry, repository, *, model=None, depth="standard", timeout=None,
                        skip_overview=False):
    from app.core.config import settings
    from app.review.contracts import ReviewRequest
    from app.review.domain import TERMINAL, utcnow

    key = sample_key(entry)
    repository_key = f"bench-{entry['head'][:16]}-{entry['pr_number']}"
    settings.rivu_local_repositories[repository_key] = repository
    request = ReviewRequest(source={
        "kind": "local_git", "repository_key": repository_key,
        "base_ref": entry["base"], "head_ref": entry["head"],
        "title": (entry.get("title") or key)[:200], "body": (entry.get("body") or "")[:4000],
    }, model=model, depth=depth, include_pr_discussions=False, generate_overview=not skip_overview,
        limits={"timeout_seconds": timeout} if timeout is not None else {})
    started = perf_counter()
    run = await service.create(request, "reviewbench-" + uuid4().hex)
    while run.status not in TERMINAL:
        if utcnow() > run.hard_deadline and (utcnow() - run.hard_deadline).total_seconds() > 30:
            await service.cancel_task(run.id)
        await asyncio.sleep(0.25)
        run = await asyncio.to_thread(service.get_task, run.id)
    detail = {
        "key": key, "review_id": run.id, "status": run.status,
        "error": run.error_code, "unresolved": 0, "skipped_findings": [],
    }
    if run.report:
        detail["unresolved"] = sum(candidate.verification is None
            or candidate.verification.verdict == "insufficient" for candidate in run.report.candidates)
        detail["reasons"] = run.report.reasons
        detail["usage"] = run.report.usage
        detail["overview"] = {
            "status": run.report.overview.status,
            "error_code": run.report.overview.error_code,
            "diagnostics": [item.model_dump() for item in run.report.overview.diagnostics],
        }
        if not detail["error"] and run.status == "failed":
            detail["error"] = next((item.error_code for item in run.report.units
                                    if item.error_code), "review_failed")
    if run.status not in {"completed", "partial"} or run.report is None:
        return None, detail
    output, skipped = export_findings(run, entry, int((perf_counter() - started) * 1000))
    detail.update(findings=len(output["findings"]), skipped_findings=skipped)
    return output, detail


def configure_runtime(directory):
    for name, value in {
        "RIVU_DB_PATH": directory / "reviews.db",
        "RIVU_ARTIFACT_DIR": directory / "artifacts",
        "RIVU_WORKDIR": directory / "workspaces",
        "RIVU_PR_DISCUSSIONS_ENABLED": "false", "RIVU_GITHUB_PUBLISH_ENABLED": "false",
        "RIVU_LANGSMITH_TRACING": "false", "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
    }.items():
        os.environ[name] = str(value)


async def run_reviews(args, entries, directory):
    configure_runtime(directory)
    from alembic import command
    from alembic.config import Config
    from app.core.config import settings

    backend = Path(__file__).resolve().parents[2]
    migration = Config(str(backend / "alembic.ini"))
    migration.set_main_option("script_location", str(backend / "alembic"))
    await asyncio.to_thread(command.upgrade, migration, "head")
    from app.bootstrap.dependencies import get_review_service
    from app.languages.isolation import close_parser_workers
    from app.review.observability import close_observability

    service = get_review_service()
    settings.rivu_local_repositories = {}
    summary = {
        "selected": len(entries), "model": args.model or settings.rivu_model,
        "depth": args.depth, "skip_overview": args.skip_overview, "samples": [],
    }
    print(f"Reviewer: {summary['model']}; selected: {len(entries)}", flush=True)
    try:
        for entry in entries:
            key = sample_key(entry)
            print(f"Reviewing {entry['nwo']}#{entry['pr_number']}", flush=True)
            try:
                repository = await asyncio.to_thread(prepare_repository, entry,
                    directory / "repos" / key, service._git_tool)
                output, detail = await review_sample(service, entry, repository,
                    model=args.model, depth=args.depth, timeout=args.timeout,
                    skip_overview=args.skip_overview)
                if output is not None:
                    write_json(directory / "findings" / f"{key}.json", output)
            except Exception as exc:
                detail = {"key": key, "status": "failed",
                          "error": getattr(exc, "code", type(exc).__name__)}
            summary["samples"].append(detail)
            summary["counts"] = dict(Counter(item["status"] for item in summary["samples"]))
            summary["scored_inputs"] = len(list((directory / "findings").glob("*.json")))
            write_json(directory / "summary.json", summary)
            print(f"  {detail['status']}; findings={detail.get('findings', 0)}"
                  f"; error={detail.get('error') or '-'}", flush=True)
    finally:
        await service.close()
        await asyncio.to_thread(close_parser_workers)
        await close_observability()
    return summary


def judge(args, directory):
    findings = directory / "findings"
    files = list(findings.glob("*.json"))
    if not files:
        print("没有可评分的 findings 文件，请查看 summary.json。")
        return 1
    allow_empty = not any(json.loads(path.read_text(encoding="utf-8"))["findings"] for path in files)
    result = subprocess.run([
        "npm", "run", "judge", "--", "--candidate", str(findings),
        "--provider", args.judge_provider, "--model", args.judge_model,
        "--output", str(directory / "scoring/results.json"),
        "--concurrency", str(args.judge_concurrency),
        *(["--allow-empty"] if allow_empty else []),
    ], cwd=args.benchmark_dir, check=False)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-dir", type=Path, required=True, help="官方 ReviewBench checkout")
    parser.add_argument("--run-dir", type=Path, help="新评测目录；--judge-only 时指定已有目录")
    parser.add_argument("--set", choices=("test", "full"), default="test")
    parser.add_argument("--limit", type=int, default=1, help="样本数，默认 1")
    parser.add_argument("--pr", type=int, help="只运行清单中指定索引，索引从 0 开始")
    parser.add_argument("--model", help="Reviewer 模型，默认沿用 Rivu 配置")
    parser.add_argument("--depth", choices=("quick", "standard", "deep"), default="standard")
    parser.add_argument("--timeout", type=int, help="每 PR 审查秒数，受现有服务上限约束")
    parser.add_argument("--no-judge", action="store_true", help="仅生成 findings")
    parser.add_argument("--skip-overview", action="store_true", help="不生成总览，专注发现质量与调查成本")
    parser.add_argument("--judge-only", action="store_true", help="仅为已有运行执行 judge")
    parser.add_argument("--judge-provider", help="官方 judge 的 provider 名称")
    parser.add_argument("--judge-model", help="官方 judge 的模型 ID")
    parser.add_argument("--judge-concurrency", type=int, default=1)
    args = parser.parse_args()
    if not args.no_judge and not (args.judge_provider and args.judge_model):
        parser.error("提供 --judge-provider 和 --judge-model，或先用 --no-judge")
    if args.limit < 1 or (args.pr is not None and args.pr < 0):
        parser.error("--limit 至少为 1，--pr 从 0 开始")
    args.benchmark_dir = args.benchmark_dir.resolve()
    if args.judge_only:
        if args.run_dir is None or args.no_judge or args.skip_overview:
            parser.error("--judge-only 需要 --run-dir，且不能与 --no-judge 或 --skip-overview 同用")
        return judge(args, args.run_dir.resolve())
    manifest = args.benchmark_dir / ("corpus/test/test.json" if args.set == "test"
                                     else "corpus/manifest.json")
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    selected = entries[args.pr:args.pr + 1] if args.pr is not None else entries[:args.limit]
    if not selected:
        parser.error("选中的样本不存在")
    directory = (args.run_dir or Path(".rivu/evaluations") /
        f"reviewbench-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}").resolve()
    directory.mkdir(parents=True, exist_ok=False)
    print(f"运行目录：{directory}", flush=True)
    summary = asyncio.run(run_reviews(args, selected, directory))
    print(f"结果：{summary['counts']}；可评分 PR：{summary['scored_inputs']}", flush=True)
    return (0 if summary["scored_inputs"] else 1) if args.no_judge else judge(args, directory)


if __name__ == "__main__":
    raise SystemExit(main())
