# ARRAX AI

> A fresh, safety-first agentic assistant prototype built for the Frontier Buildathon.

## Project status

This repository is currently in the documentation and configuration phases. The destination repository contains no runtime implementation yet.

The application described below is the intended fresh implementation, not a claim that these features already exist. Completed work will be marked explicitly as it is added and verified.

## Overview

ARRAX AI is intended to help a user turn a natural-language task into a transparent, controlled sequence of actions. The project focuses on a small number of reliable workflows rather than unrestricted control of a computer.

The first implementation will prioritize:

- A clear task and execution-status view.
- Structured plans that can be inspected before execution.
- An explicit allowlisted tool registry.
- Deterministic permission checks outside the AI layer.
- Approval gates for sensitive actions.
- Verifiable execution results and failure states.
- An offline, deterministic test mode.

The core workflow must remain useful and testable without requiring a live AI provider, database server, or real destructive desktop action.

## Buildathon provenance

This is a fresh implementation for the destination repository. The previous ARRAX project is used only as a requirements and behavior reference. Its source files, Git history, runtime memory, screenshots, credentials, and machine-specific data are not project dependencies and must not be copied into this repository.

The project is being developed in OmniRush sessions in accordance with the Frontier Buildathon guidance. Any future third-party dependency, asset, or generated content will be reviewed for its license and provenance before inclusion.

## Implemented features

Currently implemented:

- None. This repository currently contains documentation only.

Not yet implemented:

- Task intake or planning.
- Tool execution.
- Desktop automation.
- AI-provider integration.
- Database or persistent memory.
- Frontend dashboard.
- Authentication.
- Production deployment.

## Configured technology baseline

The following foundation is now configured, but the application runtime is not yet implemented:

- **Backend:** Python 3.12 or newer with Flask.
- **Frontend:** Server-served HTML, CSS, and vanilla JavaScript.
- **Tests:** `pytest` with mocks for external providers and unsafe tools.
- **Persistence:** A portable local store selected after the execution model is defined.
- **Packaging:** Pinned dependency manifests, a Python version declaration, and a future Dockerfile suitable for offline test execution.

The current dependency files are `requirements.txt`, `requirements-dev.txt`, and `requirements.lock`. The supported local interpreter is pinned in `.python-version`.

The baseline may be adjusted if the implementation or buildathon rebuild constraints demonstrate a better, smaller option. No framework or external service will be added only for visual polish.

## Planned architecture

The planned architecture separates policy from execution:

```text
User interface
    |
    v
HTTP/API boundary  --> request validation and consistent responses
    |
    v
Task service       --> task state, plan, approvals, cancellation
    |
    +--> Policy engine      --> deterministic permission decisions
    |
    +--> Tool registry      --> allowlisted tools with schemas and limits
    |
    +--> Result verifier    --> evidence that an action completed
    |
    v
Safe local adapters and test doubles
```

AI output, if added, will be treated as untrusted input. It will not be allowed to grant itself permissions, select unregistered tools, or bypass confirmation requirements.

## Planned folder structure

The repository currently has no source folders. The target structure is expected to be similar to:

```text
ARRAX_Agentic_AI/
├── src/arrax/
│   ├── app.py             # application factory and startup
│   ├── config.py          # validated environment configuration
│   ├── api/               # HTTP routes and response schemas
│   ├── agent/             # task lifecycle and planning contracts
│   ├── tools/             # allowlisted tools and permission policies
│   ├── storage/           # persistence boundary
│   └── verification/      # execution-result verification
├── frontend/              # dashboard assets
├── tests/                 # offline unit, API, and lifecycle tests
├── .env.example           # safe empty configuration template
├── .gitignore              # excludes secrets and runtime data
├── Dockerfile             # future reproducible build/test environment
├── .python-version         # supported Python version
├── requirements.txt        # runtime dependencies
├── requirements-dev.txt    # development/test dependencies
├── requirements.lock       # pinned runtime and test baseline
├── pyproject.toml         # test and tool configuration
└── README.md
```

The structure is provisional until the first implementation slice is designed and tested.

## Installation and setup

There is no runnable application yet because the application implementation has not started. The repository foundation can be prepared with:

```text
python -m venv .venv
python -m pip install -r requirements.lock
```

These commands install the configured baseline only; they do not start an application. They will be verified from a clean checkout before being treated as a completed setup path.

When the first runnable slice is added, this section will contain the exact commands for:

1. Selecting the supported Python version.
2. Creating an isolated environment.
3. Installing the pinned dependencies.
4. Creating safe local configuration from `.env.example`.
5. Starting the development server.

Commands will be tested from a clean checkout before they are documented as working.

## Environment variables

The `.env.example` file is present as a safe template, but no environment variables are currently consumed because no runtime code exists yet.

Future integrations will expose only documented variable names in `.env.example`. Real API keys, passwords, tokens, personal data, and machine-specific paths must remain outside Git.

## Database and memory

No database or persistent memory layer is currently implemented or configured.

The eventual storage boundary must:

- Work with isolated test data.
- Validate stored values.
- Avoid committing personal runtime history.
- Handle transactions and failures explicitly.
- Support a deterministic offline test mode.

The storage choice will be documented only after it is implemented and verified. The project does not currently claim MySQL compatibility or database persistence.

## Running the application

The application cannot be run yet. A working run command will be added after the first vertical implementation slice is complete.

The default development mode is expected to be local-only and safe. Network exposure, authentication, and any platform-specific adapters will be addressed explicitly rather than enabled by default.

## Testing

The pytest configuration is present, but no test suite exists yet. Running pytest before tests are added is not a passing application check.

The target is one offline command that exercises the complete safe test suite, including:

- Request validation.
- Task-state transitions.
- Tool-schema validation.
- Permission and approval decisions.
- Timeout and cancellation behavior.
- Result verification.
- API error responses.
- Mocked AI, storage, and platform adapters.

Tests must never perform real shutdowns, restarts, destructive filesystem operations, unrestricted shell commands, or real messages.

## Security model and limitations

The current repository has no runtime security surface because it contains no application code.

The intended security model is:

- Deny unknown tools by default.
- Keep permission decisions in deterministic application code.
- Validate every request and tool input.
- Require explicit approval for sensitive or destructive operations.
- Avoid unrestricted shell execution and arbitrary file access.
- Keep local development binding restricted unless explicitly configured.
- Do not log secrets or raw credentials.
- Render user-generated content safely in the frontend.
- Make external AI and desktop integrations optional and mockable.

This project will not claim that a tool is successful until its completion can be verified. A safety policy is not a replacement for operating-system security, authentication, backups, or user review.

## Known issues and limitations

The current known limitations are:

- No executable application exists yet.
- No frontend exists yet.
- No persistence exists yet.
- No AI provider is connected.
- No desktop automation adapter is available.
- No authentication or production deployment is supported.
- The final supported platform has not yet been validated.
- No Dockerfile or Docker validation exists yet.

These items will be removed from this list only after implementation and verification.

## Roadmap

1. Establish the project configuration and reproducible test command.
2. Implement a minimal task lifecycle with deterministic test tools.
3. Add the API boundary and safe execution-status responses.
4. Add a small dashboard for task input, plan review, approval, and results.
5. Add cancellation, timeouts, and result verification.
6. Add optional AI planning behind strict schema and permission validation.
7. Add optional platform adapters only after the core workflow is tested.
8. Validate the project in a clean Docker environment with network access disabled for tests.

## Contribution guidance

Before contributing:

1. Read the README and the project instructions.
2. Keep changes small and focused.
3. Do not add secrets, personal runtime data, generated caches, or machine-specific paths.
4. Add or update tests for behavior changes.
5. Run the documented offline test command.
6. Review the diff for unsafe operations and unintended files.
7. Use a concise conventional commit message.

Feature proposals should explain the user outcome, the permission boundary, the failure behavior, and how the feature can be tested without unsafe side effects.

## License status

No license has been selected for this project yet. Until a license is explicitly added, reuse and redistribution rights should not be assumed beyond the rights granted by applicable law.

## Related reference

The historical ARRAX AI Desktop Assistant repository is available at:

<https://github.com/amaanshaikhh710-ux/ARRAX-AI-DESKTOP-ASSISTANT>

It is a read-only reference for requirements and behavior. This repository is intentionally being implemented independently.
