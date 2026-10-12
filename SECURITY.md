# Security policy

## Scope

DevMind has two execution modes. Local mode runs commands on the host and is not an operating-system sandbox. Public mode uses disposable E2B microVMs for terminal commands and agent test runs, with separate workspace cookies, repository-size limits, in-memory per-IP request limits, and a per-process concurrency cap. The public demo remains anonymous and has no durable abuse controls.

## Safe operation

- Keep local mode bound to `127.0.0.1`; it is intended only for trusted code.
- For public mode, configure `DEVMIND_PUBLIC_MODE=true` and a dedicated E2B key. Never pass the host's model credentials into an execution VM.
- Public mode enables VM network access so repositories can install dependencies. Treat code and tests inside that VM as untrusted and assume they can make outbound network requests.
- Set explicit spend limits with the sandbox provider and monitor usage. The in-memory per-IP limits can be bypassed across service restarts and replicas.
- Do not provide private-repository credentials. Public mode accepts only public GitHub HTTPS URLs.
- Treat the web service container as packaging, not the execution boundary; E2B provides the separate VM boundary.

## Reporting

Please report vulnerabilities privately to the repository maintainers before public disclosure. Include a minimal reproduction, affected revision, and impact. There is no guaranteed response-time SLA for this prototype.
