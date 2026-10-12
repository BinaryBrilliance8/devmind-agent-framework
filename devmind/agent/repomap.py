"""AST-based repository symbol map with query relevance ranking."""
from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass
from pathlib import Path


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


def build_repomap(workspace: str | Path, query: str = "", *, limit: int = 250) -> list[dict]:
    root = Path(workspace).resolve()
    symbols: list[Symbol] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in {".git", ".venv", "venv", "node_modules", "__pycache__"} for part in path.parts):
            continue
        symbols.extend(parse_python_file(path, root))
    terms = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", query.lower()))
    def rank(symbol: Symbol) -> tuple[int, int, str]:
        haystack = f"{symbol.path} {symbol.name} {symbol.signature} {symbol.parent or ''}".lower()
        score = sum(3 if t in symbol.name.lower() else 0 for t in terms)
        score += sum(2 if t in symbol.path.lower() else 0 for t in terms)
        score += sum(1 for t in terms if t in haystack)
        return (-score, symbol.line, symbol.path)
    symbols.sort(key=rank)
    return [asdict(symbol) for symbol in symbols[:limit]]
