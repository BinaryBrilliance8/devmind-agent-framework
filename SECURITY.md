# Security policy

## Scope

DevMind is a research/portfolio prototype that executes commands and test suites in a user-selected repository. The current executor is not an operating-system sandbox. It does not provide tenant isolation, resource quotas, network isolation, or authentication for the web API.

## Safe operation

- Run against code you own or trust.
- Keep the web server bound to `127.0.0.1` and do not expose it to the public internet.
- Do not put production credentials in the server environment. The executor filters environment variable names containing `API_KEY` or `TOKEN`, but other secrets and filesystem access are not isolated.
- For untrusted code, add a separately maintained OS/container/VM boundary, deny network access, impose CPU/memory/disk quotas, and add authenticated access before running it.
- Treat the Dockerfile as packaging, not containment.

## Reporting

Please report vulnerabilities privately to the repository maintainers before public disclosure. Include a minimal reproduction, affected revision, and impact. There is no guaranteed response-time SLA for this prototype.
