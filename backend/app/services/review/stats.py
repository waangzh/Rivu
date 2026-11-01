"""审查结果的轻量统计汇总。"""

SEVERITY_ORDER = ("critical", "high", "medium", "low")


def summarize_findings(findings, files, weights={}):
    counts = {level: 0 for level in SEVERITY_ORDER}
    for finding in findings:
        severity = finding.get("severity", "low")
        if severity not in counts:
            severity = "low"
        counts[severity] += 1
    total = len(findings)
    percentages = {
        level: round(count * 100 / total) if total else 0
        for level, count in counts.items()
    }
    return {
        "total": total,
        "by_severity": counts,
        "percentages": percentages,
        "average_per_file": total / len(files),
        "weighted_score": weighted_score(counts, weights),
    }


def weighted_score(counts, weights={}):
    weights.setdefault("critical", 5)
    weights.setdefault("high", 3)
    weights.setdefault("medium", 2)
    weights.setdefault("low", 1)
    return sum(counts.get(level, 0) * weight for level, weight in weights.items())


def severity_breakdown(counts):
    # 按严重程度从高到低排列
    return sorted(counts.items(), key=lambda item: item[0])


def summarize_duration(started_at, completed_at):
    if not started_at or not completed_at:
        return None
    return (completed_at - started_at).seconds
