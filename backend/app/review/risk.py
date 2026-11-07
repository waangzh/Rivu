"""Risk signals reused from the existing static planner."""

import re
from collections import Counter

REFERENCEABLE_SUFFIXES = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts",
    ".java", ".go", ".rs", ".vue", ".svelte",
)
CONFIG_NAMES = frozenset({
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini",
    "requirements.txt", "package.json", "tsconfig.json", "vite.config.ts",
    "vite.config.js", "go.mod", "cargo.toml", "pom.xml", "build.gradle",
    "build.gradle.kts", "makefile", "pipfile",
})
RISK_PATH_GROUPS = {
    "auth": frozenset({
        "auth", "authentication", "authorization", "permission", "permissions",
        "oauth", "oidc", "saml", "password", "passwords", "token", "tokens",
        "session", "sessions", "credential", "credentials", "security",
        "secret", "secrets",
    }),
    "crypto": frozenset({"crypto", "cryptography", "encryption", "signing"}),
    "money": frozenset({
        "payment", "payments", "billing", "invoice", "invoices", "money",
        "price", "pricing", "currency", "refund", "ledger",
    }),
    "data": frozenset({
        "migration", "migrations", "schema", "schemas", "transaction", "transactions",
    }),
    "concurrency": frozenset({
        "concurrency", "worker", "workers", "queue", "queues", "lock", "locks",
    }),
    "supply_chain": frozenset({
        "requirements", "dockerfile", "workflows", "dependencies", "dependency",
    }),
    "storage": frozenset({"storage", "upload", "uploads", "download", "downloads"}),
}
RISK_OPERATION_GROUPS = {
    "crypto": re.compile(
        r"\b(?:hmac|hashlib|bcrypt|scrypt|argon2|cryptography)\b"
        r"|\b(?:encrypt|decrypt|compare_digest)\s*\(", re.I),
    "deserialization": re.compile(
        r"\b(?:pickle|marshal)\b|\byaml\.(?:un)?safe_load\b", re.I),
    "command_execution": re.compile(
        r"\b(?:subprocess|os\.system|os\.popen)\b|\b(?:eval|exec)\s*\(|shell\s*=\s*True",
        re.I),
    "sql": re.compile(
        r"\b(?:execute|executemany)\s*\(|\b(?:BEGIN|COMMIT|ROLLBACK)\b", re.I),
    "path": re.compile(r"\bos\.path\b|\b(?:realpath|normpath)\b", re.I),
    "concurrency": re.compile(
        r"\b(?:threading|multiprocessing|RLock|Semaphore)\b|\b(?:Lock|gather|wait)\s*\(",
        re.I),
    "money": re.compile(r"\bDecimal\b", re.I),
}
PUBLIC_API_PARTS = frozenset({"api", "contracts", "openapi", "public", "sdk"})
PUBLIC_API_NAMES = frozenset({"__init__.py", "index.ts", "index.js", "index.tsx", "index.jsx"})
DOCUMENTATION_SUFFIXES = (".md", ".mdx", ".rst", ".adoc", ".txt")
DOCUMENT_REFERENCES = re.compile(
    r"@(?P<mention>[\w./-]+)|\[[^\]\n]*\]\((?P<link>[^\s)]+)\)|`(?P<code>[^`\s]+)`")
SUPPORT_NAMES = frozenset({".gitignore", ".gitattributes"})
QUOTED_TEXT = r"'(?:\\.|[^'\\\n])*'" r'|"(?:\\.|[^"\\\n])*"'
PYTHON_QUOTED_TEXT = (
    r"'''[\s\S]*?(?:'''|\Z)" + r'|"""[\s\S]*?(?:"""|\Z)|' + QUOTED_TEXT)
PYTHON_NON_CODE = re.compile(
    r"(?P<dynamic>\b(?i:f|fr|rf)(?:" + PYTHON_QUOTED_TEXT + r"))"
    r"|(?:\b(?i:r|u|b|br|rb))?(?:" + PYTHON_QUOTED_TEXT + r")|#[^\n]*")
C_STYLE_NON_CODE = re.compile(
    r"/\*[\s\S]*?(?:\*/|\Z)|//[^\n]*|<!--[\s\S]*?(?:-->|\Z)|"
    r'"""[\s\S]*?(?:"""|\Z)|(?P<dynamic>`[\s\S]*?(?:`|\Z))|' + QUOTED_TEXT)
SQL_NON_CODE = re.compile(r"--[^\n]*|" + C_STYLE_NON_CODE.pattern)

def _code_lines(path, lines, *, keep_literals=False):
    path = path.casefold()
    if path.endswith(DOCUMENTATION_SUFFIXES):
        return [""] * len(lines)
    if path.endswith(".sql"):
        pattern = SQL_NON_CODE
    elif path.endswith((".py", ".pyi", ".sh", ".bash")) or path.rsplit("/", 1)[-1] == "dockerfile":
        pattern = PYTHON_NON_CODE
    else:
        pattern = C_STYLE_NON_CODE
    text = "\n".join(line.get("content", "") for line in lines)
    # Interpolated strings may execute code; keep them as conservative lexical hints.
    def replace(match):
        if match.group("dynamic"):
            return match.group()
        if keep_literals and not match.group().startswith(("#", "//", "/*", "<!--", "--")):
            return match.group()
        return re.sub(r"[^\n]", " ", match.group())
    return pattern.sub(replace, text).split("\n")


def _changed_code(change, *, keep_literals=False):
    for hunk in change.get("hunks", []):
        lines = hunk.get("lines", [])
        head_lines = [line for line in lines if line.get("kind") != "deleted"]
        base_lines = [line for line in lines if line.get("kind") != "added"]
        head = iter(_code_lines(change["file_path"], head_lines))
        base = iter(_code_lines(change["file_path"], base_lines))
        literal_head = iter(_code_lines(change["file_path"], head_lines, keep_literals=True)
                            if keep_literals else [""] * len(head_lines))
        literal_base = iter(_code_lines(change["file_path"], base_lines, keep_literals=True)
                            if keep_literals else [""] * len(base_lines))
        head_line = hunk["new_start"] + (1 if not hunk["new_length"] else 0)
        for line in lines:
            kind = line.get("kind")
            old_code = next(base) if kind != "added" else ""
            new_code = next(head) if kind != "deleted" else ""
            old_literal = next(literal_base) if kind != "added" else ""
            new_literal = next(literal_head) if kind != "deleted" else ""
            if kind in {"added", "deleted"}:
                code = new_code if kind == "added" else old_code
                if code.strip():
                    value = (new_literal if kind == "added" else old_literal) if keep_literals else code
                    yield kind, head_line, value
            if kind != "deleted":
                head_line += 1


def _module(path):
    parts = path.split("/")[:-1]
    index = 0
    while index < len(parts) - 1 and parts[index].casefold() in {"app", "backend", "frontend", "src", "lib"}:
        index += 1
    return "/".join(parts[:index + 1])


def _source_or_config(path):
    return path.casefold().endswith(REFERENCEABLE_SUFFIXES) or (
        path.rsplit("/", 1)[-1].casefold() in CONFIG_NAMES)


def _documentation_support(path):
    return path.casefold().endswith((".md", ".mdx", ".rst", ".adoc")) or path in SUPPORT_NAMES


def _public_entry(path):
    parts = path.casefold().split("/")
    return parts[-1] in PUBLIC_API_NAMES or any(
        part in PUBLIC_API_PARTS for part in parts[:-1])


def _declaration_hint(path, code, literal_code):
    """Recognize changed declaration headers, never compatibility or visibility proof.

    Only complete single-line headers are compared. Partial/multiline declarations
    retain the weaker public-entry hint; no parser or target execution is needed.
    The masked line locates syntax, while literals preserve defaults/re-export targets.
    """
    suffix = path.casefold().rsplit(".", 1)[-1]
    if suffix in {"py", "pyi"} and _public_entry(path):
        declaration = re.match(r"\s*(?:async\s+)?(?:def|class)\s+([\w]+)\b", code)
        if declaration and not declaration[1].startswith("_"):
            depth = 0
            for index in range(declaration.end(), len(code)):
                char = code[index]
                if char in "([{":
                    depth += 1
                elif char in ")]}":
                    depth -= 1
                elif char == ":" and depth == 0:
                    return literal_code[:index + 1].strip()
        if path.casefold().endswith("/__init__.py") or path.casefold() == "__init__.py":
            if re.match(r"\s*(?:from\s+\S+\s+import\b|import\s+|__all__\s*=)", code):
                return literal_code.strip()
    elif suffix in {"js", "jsx", "ts", "tsx", "mjs", "cjs", "mts", "cts"}:
        if not re.match(r"\s*export\b", code):
            return None
        # Callable bodies may share the declaration's line. Compare only the header.
        if re.match(r"\s*export\s+(?:default\s+)?(?:async\s+)?function\b", code):
            boundary = code.find("{")
            if boundary >= 0 and ")" in code[:boundary]:
                return literal_code[:boundary].strip()
            return None
        if "=>" in code:
            return literal_code[:code.index("=>")].strip()
        if re.match(r"\s*export\s+(?:default\s+)?(?:abstract\s+)?class\b", code):
            return literal_code.split("{", 1)[0].strip()
        return literal_code.strip()
    return None


def _api_signals(change):
    """Diff-only scheduling hints; missing hints do not establish API stability."""
    path = change["file_path"]
    previous_path = change.get("old_file_path") or path
    changed = list(_changed_code(change))
    declarations = {"added": Counter(), "deleted": Counter()}
    for (kind, _, code), (_, _, literal_code) in zip(
            changed, _changed_code(change, keep_literals=True), strict=True):
        side_path = previous_path if kind == "deleted" else path
        header = _declaration_hint(side_path, code, literal_code)
        if header:
            declarations[kind][header] += 1
    public_entry = (_public_entry(path) or _public_entry(previous_path))
    source_path = _source_or_config(path) or _source_or_config(previous_path)
    path_hint = public_entry and source_path and bool(changed or (
        change.get("change_type") in {"added", "deleted", "renamed"}))
    return path_hint, declarations["added"] != declarations["deleted"]


def _risk(paths, changes):
    segments = {segment for path in paths
                if any(hunk["old_length"] or hunk["new_length"]
                       for hunk in changes[path].get("hunks", []))
                for segment in re.split(r"[/_.-]", path.casefold()) if segment}
    volume = sum(hunk["old_length"] + hunk["new_length"]
                 for path in paths for hunk in changes[path].get("hunks", []))
    # A pure rename carries no text change; structural references cover it.
    path_hits = sorted(group for group, words in RISK_PATH_GROUPS.items()
                       if segments & words)
    added = "\n".join(code for path in paths
                      for kind, _, code in _changed_code(changes[path]) if kind == "added")
    operation_hits = sorted(group for group, pattern in RISK_OPERATION_GROUPS.items()
                            if pattern.search(added))
    api_signals = [_api_signals(changes[path]) for path in paths]
    public_path = any(path_hint for path_hint, _ in api_signals)
    public_api = any(declaration_hint for _, declaration_hint in api_signals)
    cross_module = (len({_module(path) for path in paths}) > 1
                    and not all(_documentation_support(path) for path in paths))
    file_event = any(changes[path].get("change_type") in {"added", "deleted"}
                     and _source_or_config(path) for path in paths)
    structural = any(changes[path].get("change_type") == "renamed" for path in paths)
    reasons = [f"sensitive_path:{group}" for group in path_hits]
    reasons += [f"sensitive_operation:{group}" for group in operation_hits]
    if public_api:
        reasons.append("public_api_change")
    elif public_path:
        reasons.append("public_api_path")
    if cross_module:
        reasons.append("cross_module")
    if volume >= 200:
        reasons.append("large_change")
    if file_event:
        reasons.append("file_added_or_deleted")
    if structural:
        reasons.append("structural_change")
    score = (2 if path_hits else 0) + (2 if operation_hits else 0) + (2 if public_api else 0)
    score += 1 if public_path and not public_api else 0
    score += (1 if cross_module else 0) + (1 if file_event else 0) + (1 if structural else 0)
    if volume >= 200:
        score += 2 if volume >= 800 else 1
    if score >= 2 and (path_hits or operation_hits or public_api):
        return "high", reasons
    return ("medium" if score >= 1 else "low"), reasons
