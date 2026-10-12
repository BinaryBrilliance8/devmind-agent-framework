# DevMind

**An execution-grounded agent framework for repository-level software engineering.** DevMind combines AST-based repository context, bounded subprocess execution, syntax-guarded edits, Git checkpoints, pytest feedback, and a small web IDE into one inspectable workflow.

> Research prototype / portfolio project. The executor applies useful process-level guardrails, but it is not an OS security boundary. Run only against repositories and code you trust. Do not expose the web server to the public internet or use it as a multi-tenant service.

## What it does

- Opens a local repository or shallow-clones a Git URL into a workspace.
- Builds a Python AST symbol map and narrows context using an issue description.
- Runs a deterministic five-stage task pipeline: localize, reproduce, patch, verify, and reflect or roll back.
- Restricts Python writes to syntactically valid files and creates a Git checkpoint before an edit.
- Executes commands as argv without shell composition, with a workspace cwd, timeout, bounded output, and API key/token environment filtering.
- Streams model output, provider usage where the provider returns it, provider/model identity, command results, and measured latency to the browser.
- Includes three local, multi-file bug benchmarks and a dark-mode FastAPI web IDE.

## Quick start

Requirements: Python 3.11+, Git, and (for model-assisted runs) either a local Ollama server or a hosted provider key.

```bash
git clone https://github.com/BinaryBrilliance8/devmind-agent-framework.git
cd devmind-agent-framework
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
```

For a local model, install/run Ollama separately, pull the configured model, then launch DevMind:

```bash
ollama pull qwen2.5-coder:14b
python -m devmind.server
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The server binds to localhost by default. Choose a benchmark in the UI for a deterministic local demo, or open a repository you control and describe a concrete issue. A benchmark is intentionally buggy: the initial tests should fail, and successful resolution requires a patch followed by a passing verification run.

Hosted providers are `openai`, `gemini`, and `groq`; set the corresponding `OPENAI_API_KEY`, `GEMINI_API_KEY`, or `GROQ_API_KEY` before starting the service. Optional configuration:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEVMIND_HOST` | `127.0.0.1` | Bind address; keep loopback for local use |
| `DEVMIND_PORT` | `8000` | HTTP port |
| `DEVMIND_WORKSPACES` | system temp `devmind-workspaces` | Workspace storage root |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434/v1/chat/completions` | Ollama-compatible endpoint |
| `DEVMIND_MODEL` | provider default | Override the model name |

## Architecture

```text
Browser UI ── FastAPI/SSE ── AgentRun state machine
                                 ├─ AST repo map
                                 ├─ LLMClient (Ollama / OpenAI / Gemini / Groq)
                                 ├─ FileEditor (syntax check + Git checkpoint)
                                 └─ Executor (argv subprocess + bounded output)
```

The API exposes health/provider/benchmark discovery and workspace, file tree, file, AST, diff, terminal, and streamed run endpoints. `devmind/agent/core.py` owns pipeline transitions; `devmind/agent/tools.py` owns controlled edits; `devmind/sandbox/` owns workspace ingestion, command execution, and benchmark materialization. The UI is packaged under `devmind/static/`.

Token counts are taken from the provider's streaming usage payload. If a provider omits usage, the UI reports unavailable counts rather than inventing estimates. Latency is measured around the actual provider request.

## Run and inspect a local benchmark

Use the UI benchmark picker or the API:

```bash
curl -s http://127.0.0.1:8000/api/benchmarks
curl -s -X POST http://127.0.0.1:8000/api/workspace \
  -H 'content-type: application/json' \
  -d '{"benchmark":"async-rate-limiter"}'
```

Then run an issue through the UI. Local benchmarks do not require cloning a public repository; model-assisted repair still requires a reachable configured model.

## Verification

```bash
python -m pytest -v
```

The architecture tests exercise real local Git operations, syntax rejection without disk mutation, command output/environment limits, and failing baseline tests for the benchmark repositories. CI runs the test suite on supported Python versions.

## Security and deployment

This application deliberately launches commands and tests from the opened workspace. Process timeouts, argv-only execution, path checks, output caps, and environment filtering reduce accidental risk; they do **not** contain malicious code. A hostile repository can still exploit the host through interpreters, dependencies, filesystem behavior, or resource use. The current web app also has no authentication or tenant isolation.

For a demo, run it locally or on a private, trusted network behind authentication and a separately configured OS/container sandbox. Never bind an unauthenticated instance to `0.0.0.0` or publish it as an open service. The included Dockerfile is a packaging convenience, not a security sandbox. Review [SECURITY.md](SECURITY.md) before operating it.

## Project status

DevMind is an educational engineering prototype, not a claim of production-grade autonomous issue resolution or a SWE-bench score. It is designed to make each action inspectable and to fail visibly when commands, tests, or provider calls fail.

## License

MIT; see [LICENSE](LICENSE).
