from pathlib import Path

from devmind.agent.repomap import build_repomap, parse_javascript_file


def test_javascript_declarations_and_methods_are_indexed_without_comment_noise(tmp_path: Path) -> None:
    source = tmp_path / "service.js"
    source.write_text(
        """// function fake() { ignored(); }
const example = "class NotAClass { nope() {} }";
class Counter {
  increment(amount) {
    if (amount) { return amount + 1; }
    return 0;
  }
}
async function loadUser(id) {
  return id;
}
""",
        encoding="utf-8",
    )

    symbols = parse_javascript_file(source, tmp_path)

    assert [(item.kind, item.name) for item in symbols] == [
        ("module", "service"),
        ("class", "Counter"),
        ("function", "loadUser"),
        ("method", "increment"),
    ]
    method = next(item for item in symbols if item.name == "increment")
    assert method.parent == "Counter"
    assert (method.line, method.end_line) == (4, 7)


def test_query_ranks_javascript_module_containing_the_issue_terms(tmp_path: Path) -> None:
    (tmp_path / "config.js").write_text("const port = 3000;\n", encoding="utf-8")
    (tmp_path / "sessionStore.js").write_text(
        "const store = createMongoStore();\nstore.on('error', (err) => console.error(err));\n",
        encoding="utf-8",
    )

    symbols = build_repomap(tmp_path, "Mongo session store error callback")

    assert symbols[0]["path"] == "sessionStore.js"
    assert symbols[0]["kind"] == "module"
