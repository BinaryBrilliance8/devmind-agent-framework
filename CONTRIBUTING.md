# Contributing

Thanks for helping improve DevMind. Keep changes focused and make execution claims reproducible.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -v
```

Before opening a pull request:

- Add or update tests for behavior changes.
- Keep subprocess and provider failures explicit; do not replace real outcomes with simulated telemetry.
- Do not commit credentials, workspace checkouts, generated output, or local model artifacts.
- Update the README when setup, configuration, API, or security behavior changes.

## Pull requests

Describe the user-visible behavior, the commands/tests actually run, and any limitations. Avoid claims of benchmark performance unless the result is measured and the benchmark protocol is documented.
