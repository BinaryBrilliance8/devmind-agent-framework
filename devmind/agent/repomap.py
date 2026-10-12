"""AST-based repository symbol map with query relevance ranking."""
from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass
from pathlib import Path


SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}
IGNORED_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build", "coverage"}


@dataclass
class Symbol:
    path: str
    kind: str
    name: str
    signature: str
    line: int
    end_line: int
    parent: str | None = None


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(node.args)
        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    except Exception:
        args, returns = "...", ""
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    return f"{prefix}{node.name}({args}){returns}"


def parse_python_file(path: Path, root: Path) -> list[Symbol]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    rel = path.relative_to(root).as_posix()
    symbols: list[Symbol] = []
    def visit(nodes: list[ast.stmt], parent: str | None = None) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                symbols.append(Symbol(rel, "class", node.name, f"class {node.name}", node.lineno, getattr(node, "end_lineno", node.lineno), parent))
                visit(node.body, node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if parent else "function"
                symbols.append(Symbol(rel, kind, node.name, _signature(node), node.lineno, getattr(node, "end_lineno", node.lineno), parent))
                # Include nested declarations so closures and inner classes remain visible.
                visit(node.body, node.name)
    visit(tree.body)
    return symbols


def _mask_javascript(source: str) -> str:
    """Blank comments and quoted literals while retaining offsets and newlines."""
    output = list(source)
    index = 0
    state = "code"
    quote = ""
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            if char == "/" and nxt == "/":
                output[index] = output[index + 1] = " "
                index += 2
                state = "line_comment"
                continue
            if char == "/" and nxt == "*":
                output[index] = output[index + 1] = " "
                index += 2
                state = "block_comment"
                continue
            if char in {"'", '"', "`"}:
                quote = char
                output[index] = " "
                state = "string"
        elif state == "line_comment":
            if char == "\n":
                state = "code"
            else:
                output[index] = " "
        elif state == "block_comment":
            if char == "*" and nxt == "/":
                output[index] = output[index + 1] = " "
                index += 2
                state = "code"
                continue
            if char != "\n":
                output[index] = " "
        elif state == "string":
            if char != "\n":
                output[index] = " "
            if char == "\\":
                if index + 1 < len(source) and source[index + 1] != "\n":
                    output[index + 1] = " "
                index += 2
                continue
            if char == quote:
                state = "code"
        index += 1
    return "".join(output)


def _brace_end(masked: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(masked)):
        if masked[index] == "{":
            depth += 1
        elif masked[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(masked) - 1


def parse_javascript_file(path: Path, root: Path) -> list[Symbol]:
    """Extract useful JS/TS declarations without executing repository code.

    This is deliberately a conservative declaration scanner, not a full language
    parser. It masks comments and strings first so examples and URLs are not
    mistaken for declarations. Module symbols retain a path-level anchor for
    top-level handlers and configuration that have no named function.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if len(source) > 500_000:
        return []
    masked = _mask_javascript(source)
    rel = path.relative_to(root).as_posix()
    total_lines = max(1, source.count("\n") + (not source.endswith("\n")))
    symbols = [Symbol(rel, "module", path.stem, path.name, 1, total_lines)]
    patterns = (
        ("class", re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)[^\{;]*\{")),
        ("function", re.compile(r"\b(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{")),
        ("function", re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>\s*\{")),
        ("function", re.compile(r"\bmodule\.exports\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>\s*\{")),
    )
    for kind, pattern in patterns:
        for match in pattern.finditer(masked):
            opening = masked.find("{", match.start(), match.end())
            end = _brace_end(masked, opening)
            name = match.group(1)
            args = match.group(2) if match.lastindex and match.lastindex >= 2 else ""
            line = masked.count("\n", 0, match.start()) + 1
            end_line = masked.count("\n", 0, end) + 1
            if kind == "class":
                signature = f"class {name}"
            else:
                signature = f"{name}({args.strip()})"
            symbols.append(Symbol(rel, kind, name, signature, line, end_line))

    # Class methods are indexed only at direct class-body depth.
    class_pattern = re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)[^\{;]*\{")
    method_pattern = re.compile(r"(?:async\s+)?(?:static\s+)?(?:get\s+|set\s+)?([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{")
    excluded = {"if", "for", "while", "switch", "catch", "with", "function"}
    for class_match in class_pattern.finditer(masked):
        opening = masked.find("{", class_match.start(), class_match.end())
        closing = _brace_end(masked, opening)
        body = masked[opening + 1:closing]
        # Only declarations at depth zero belong to this class; method bodies
        # and nested blocks are ignored.
        for method in method_pattern.finditer(body):
            before_method = body[:method.start()]
            depth = before_method.count("{") - before_method.count("}")
            if depth != 0 or method.group(1) in excluded:
                continue
            method_open = opening + 1 + body.find("{", method.start(), method.end())
            method_end = _brace_end(masked, method_open)
            line = masked.count("\n", 0, opening + 1 + method.start()) + 1
            end_line = masked.count("\n", 0, method_end) + 1
            symbols.append(Symbol(rel, "method", method.group(1), f"{method.group(1)}({method.group(2).strip()})", line, end_line, class_match.group(1)))
    return symbols


def build_repomap(workspace: str | Path, query: str = "", *, limit: int = 250) -> list[dict]:
    root = Path(workspace).resolve()
    symbols: list[Symbol] = []
    file_text: dict[str, str] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES):
        if any(part in IGNORED_PARTS for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root).as_posix()
        try:
            file_text[rel] = path.read_text(encoding="utf-8")[:500_000]
        except (OSError, UnicodeDecodeError):
            continue
        if path.suffix.lower() == ".py":
            symbols.extend(parse_python_file(path, root))
        else:
            symbols.extend(parse_javascript_file(path, root))
    terms = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", query.lower()))
    def rank(symbol: Symbol) -> tuple[int, int, str]:
        haystack = f"{symbol.path} {symbol.name} {symbol.signature} {symbol.parent or ''}".lower()
        score = sum(3 if t in symbol.name.lower() else 0 for t in terms)
        score += sum(2 if t in symbol.path.lower() else 0 for t in terms)
        score += sum(1 for t in terms if t in haystack)
        if symbol.kind == "module":
            content = file_text.get(symbol.path, "").lower()
            score += sum(2 for term in terms if re.search(rf"\b{re.escape(term)}\b", content))
        return (-score, symbol.line, symbol.path)
    symbols.sort(key=rank)
    return [asdict(symbol) for symbol in symbols[:limit]]
