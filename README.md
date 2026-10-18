# ARRAX AI

> A fresh, safety-first agentic assistant prototype built for the Frontier Buildathon.

## Project status

Milestones 1 through 3 are implemented as a small, offline deterministic agent core with a Flask API boundary and a server-served vanilla dashboard. The destination repository contains the task model, planner, allowlisted fixture tools, orchestrator, SQLite persistence, bounded local workers, frontend, and pytest coverage described below.

The application is intentionally being built as a fresh implementation. Completed work is marked explicitly as it is added and verified.

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

- Structured task and plan-step models with lifecycle, risk, approval, retry, result, and verification fields.
- Deterministic planning for structured actions and fixture demo goals; no AI provider is required.
- An explicit allowlisted tool protocol and registry with input validation and bounded execution timeouts.
- Safe in-memory fixture tools for success, approval, failure recovery, verification, and timeout testing.
- A synchronous plan/policy/act/observe/verify/recover orchestrator with cancellation boundaries.
- SQLite task, step, and event repositories using stdlib `sqlite3` and transactions.
- Offline pytest coverage for the complete safe core flow.
- Flask application factory with validated task, plan, approval, cancellation, health, capabilities, and event-history endpoints.
- Bounded asynchronous task workers with an explicit application shutdown hook for tests and embedding code.
- Responsive HTML/CSS/vanilla JavaScript dashboard served by the Flask factory, connected to the real health, capabilities, task, event, approval, and cancellation endpoints.
- Safe DOM rendering for user and API text, persisted event timeline, plan-step evidence, approval controls, cancellation, and API/provider availability states.

Not yet implemented:

- Desktop automation, unrestricted shell or filesystem actions, network calls, or external APIs.
- Natural-language interpretation, AI-provider integration, authentication, or production deployment.

## Configured technology baseline

The following foundation is configured for the core and future application layers:

- **Core:** Python 3.12 or newer with the standard library; the core does not require Flask.
- **Backend:** Flask provides the local HTTP boundary around the core.
- **Frontend:** Server-served HTML, CSS, and vanilla JavaScript.
- **Tests:** `pytest` with mocks for external providers and unsafe tools.
- **Persistence:** Local SQLite through the Python standard library.
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

## Current folder structure

The core and API boundary stay in a small set of focused modules:

```text
ARRAX_Agentic_AI/
├── src/arrax/
│   ├── domain.py          # task, plan, risk, and status contracts
│   ├── planner.py         # deterministic structured-goal planner
│   ├── tools.py           # allowlisted protocol and safe fixtures
│   ├── storage.py         # SQLite repositories and schema
│   ├── orchestrator.py    # synchronous lifecycle and policy flow
│   └── api.py             # Flask factory, serialization, workers, and dashboard serving
├── frontend/
│   ├── index.html         # server-served command center
│   ├── style.css          # responsive midnight/graphite interface
│   └── script.js          # vanilla API client and safe DOM renderer
├── tests/                 # offline lifecycle and boundary tests
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

The Flask API in `api.py` is the HTTP boundary around this core. The dashboard is served at `/` and its CSS/JavaScript assets are served below `/assets/`. Platform-specific adapters remain future layers.

## Installation and setup

The core can be installed and tested with:

```text
python -m venv .venv
python -m pip install -r requirements.lock
python -m pytest -q
```

The synchronous API is available without Flask. From a checkout without an
editable package install, expose the `src` layout first:

PowerShell:

```powershell
$env:PYTHONPATH = "src"
```

POSIX shells:

```sh
export PYTHONPATH=src
```

Then:

```python
from arrax import AgentCore, TaskStatus

agent = AgentCore(":memory:")
task = agent.create_task({"type": "success", "message": "offline demo"})
finished = agent.run_task(task.id)
assert finished.status is TaskStatus.COMPLETED
history = agent.get_events(task.id)
agent.close()
```

The application factory is available without starting a server. From a checkout with `src` on `PYTHONPATH`:

```python
from arrax import create_app

app = create_app(db_path="arrax.sqlite")
# A deployment or test harness starts the server explicitly.
# When finished, call app.extensions["arrax"]["shutdown"]().
```

The factory does not bind a socket or create network clients. Its default mode is local and deterministic, and its default request body limit is 64 KiB. Tests can inject an existing `AgentCore` with `create_app(core=core)` and use the same shutdown hook to wait for bounded workers.

To run the local dashboard with Flask's development server, keep the binding
local and use an explicit non-debug command:

PowerShell:

```powershell
$env:PYTHONPATH = "src"
python -m arrax
```

POSIX shells:

```sh
PYTHONPATH=src python -m arrax
```

Open <http://127.0.0.1:5000/> after starting the command. The factory itself
still does not bind a socket; `python -m arrax` is the explicit local
development entry point. It binds to `127.0.0.1` with debug and reloader mode
disabled. Set `ARRAX_PORT`, `ARRAX_HOST`, or `ARRAX_DB_PATH` only when a local
development override is needed.

Future application layers can extend this section with exact commands for:

1. Selecting the supported Python version.
2. Creating an isolated environment.
3. Installing the pinned dependencies.
4. Creating safe local configuration from `.env.example`.
5. Starting the development server.

Commands will be tested from a clean checkout before they are documented as working.

## Environment variables

The `.env.example` file is present as a safe template. Milestone 1 consumes no environment variables and stores no credentials or secrets.

Future integrations will expose only documented variable names in `.env.example`. Real API keys, passwords, tokens, personal data, and machine-specific paths must remain outside Git.

## Database and memory

Milestone 1 uses a local SQLite database through the stdlib `sqlite3` module. Pass a filesystem path to `AgentCore` for persistence or use `":memory:"` for isolated tests.

The eventual storage boundary must:

- Work with isolated test data.
- Validate stored values.
- Avoid committing personal runtime history.
- Handle transactions and failures explicitly.
- Support a deterministic offline test mode.

The project does not claim MySQL compatibility; SQLite is the deliberate local persistence boundary for this milestone.

## Running the application

Milestone 3 adds the local dashboard on top of the Flask API but no desktop runtime or production deployment. Use the application factory shown above, then run the offline test suite.

The default development mode is expected to be local-only and safe. Network exposure, authentication, and any platform-specific adapters will be addressed explicitly rather than enabled by default.

## Testing

The offline test suite is implemented and runs with:

```text
python -m pytest -q
```

The lightweight browser-facing checks can also be run without a frontend test
framework:

```text
node --check frontend/script.js
python -m compileall -q src tests
git diff --check
```

The target is one offline command that exercises the complete safe test suite, including:

- Request validation.
- Task-state transitions.
- Tool-schema validation.
- Permission and approval decisions.
- Timeout and cancellation behavior.
- Result verification.
- persistence and event-history reloads.

Tests must never perform real shutdowns, restarts, destructive filesystem operations, unrestricted shell commands, or real messages.

## Security model and limitations

The implemented core security model is:

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

- No production deployment is supported.
- No AI provider is connected.
- Natural-language goal interpretation is deferred; the dashboard selects one of the implemented deterministic scenarios.
- No desktop automation adapter is available.
- No authentication or production deployment is supported.
- The final supported platform has not yet been validated.
- No Dockerfile or Docker validation exists yet.

These items will be removed from this list only after implementation and verification.

## Roadmap

1. Establish the project configuration and reproducible test command. **Complete.**
2. Implement a minimal task lifecycle with deterministic test tools. **Complete.**
3. Add the API boundary and safe execution-status responses. **Complete.**
4. Add a small dashboard for task input, plan review, approval, and results. **Complete.**
5. Add cancellation, timeouts, and result verification. **Core behavior complete; application surfaces remain.**
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
