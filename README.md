# ARRAX AI

> A fresh, safety-first agentic assistant prototype built for the Frontier Buildathon.

## Project status

The provider milestone is implemented as an optional, server-side Gemini planning boundary on top of the deterministic core. The destination repository contains the task model, deterministic and structured planners, allowlisted fixture tools, opt-in bounded workspace tools, orchestrator, SQLite persistence, bounded local workers, frontend, provider status API, a capability-aware measured system snapshot API, an honest offline Evaluation Lab, and reproducible test packaging.

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
- An optional provider mode that is account- and model-dependent, with explicit runtime verification.

The core workflow must remain useful and testable without requiring a live AI provider, database server, or real destructive desktop action.

## Buildathon provenance

This is a fresh implementation for the destination repository. The previous ARRAX project is used only as a requirements and behavior reference. Its source files, Git history, runtime memory, screenshots, credentials, and machine-specific data are not project dependencies and must not be copied into this repository.

The project is being developed in OmniRush sessions in accordance with the Frontier Buildathon guidance. Any future third-party dependency, asset, or generated content will be reviewed for its license and provenance before inclusion.

## Implemented features

Currently implemented:

- Structured task and plan-step models with lifecycle, risk, approval, retry, result, and verification fields.
- Deterministic planning for structured actions and fixture demo goals; no AI provider or key is required.
- A standard-library Gemini REST client using the official `models.list` and `models:generateContent` shapes, bounded retries, timeouts, redacted errors, and lazy verification.
- A JSON-only provider planner whose output is validated by the existing `PlanStep`, deterministic planner, tool registry, dependency checks, path policy, approval boundary, and registry risk floors.
- `deterministic`, `auto`, and `gemini` modes. `auto` visibly falls back to deterministic planning when a key, exact model, supported `generateContent` method, canary, or provider response is unavailable.
- An explicit allowlisted tool protocol and registry with input validation and bounded execution timeouts.
- Safe in-memory fixture tools for success, approval, failure recovery, verification, and timeout testing.
- A synchronous plan/policy/act/observe/verify/recover orchestrator with cancellation boundaries.
- SQLite task, step, and event repositories using stdlib `sqlite3` and transactions.
- Offline pytest coverage for the complete safe core flow.
- Flask application factory with validated task, plan, approval, cancellation, health, capabilities, and event-history endpoints.
- Bounded asynchronous task workers with an explicit application shutdown hook for tests and embedding code.
- Responsive HTML/CSS/vanilla JavaScript dashboard served by the Flask factory, connected to the real health, capabilities, task, event, approval, cancellation, workflow, memory, and system-observation endpoints.
- Safe DOM rendering for user and API text, persisted event timeline, plan-step evidence, approval controls, cancellation, and API/provider availability states.
- Opt-in workspace tools for bounded listing, UTF-8 text reading, text search, deterministic readiness-report creation, and actual-file verification. They are absent from the registry unless `ARRAX_WORKSPACE_ROOT` or an explicit workspace-root argument is configured.
- A deterministic `workspace_report` goal that snapshots the approved root, pauses before the sensitive report write, then verifies the generated file from disk.
- A deterministic Evaluation Lab catalog and synchronous report API covering verified success, bounded recovery, approval gates, verification guards, and timeout boundaries. Each run uses a fresh in-memory core and records measured evidence rather than fabricated benchmark values.
- A capability-aware system snapshot service and API for bounded CPU, memory, disk, network, and process observations. It reports unavailable metrics explicitly and never returns absolute paths, command lines, process IDs, or user data.
- Transactional reusable workflow persistence with versioned deterministic-goal validation, safe workflow CRUD, and bounded worker-backed workflow runs.
- Transactional user-controlled memory persistence with bounded JSON values, credential-like data rejection, key upsert semantics, CRUD, and explicit reset.
- A pinned `python:3.13.1-slim-bookworm` Docker test image, `.dockerignore`, lockfile installation, and documented judge-style offline test command.

Not yet implemented:

- Desktop automation, unrestricted shell or filesystem actions, authentication, or production deployment.
- A claim that any particular Gemini model is available to a specific account. Live access is checked only at runtime with the authenticated models endpoint and canary.

## Configured technology baseline

The following foundation is configured for the core and future application layers:

- **Core:** Python 3.13.1 as declared in `.python-version`, with the standard library; the core does not require Flask.
- **Backend:** Flask provides the local HTTP boundary around the core.
- **System measurements:** `psutil==7.0.0` is the pinned, optional-at-runtime observation backend; standard-library disk usage remains available when it is absent.
- **Frontend:** Server-served HTML, CSS, and vanilla JavaScript.
- **Tests:** `pytest` with mocks for external providers and unsafe tools.
- **Persistence:** Local SQLite through the Python standard library.
- **Packaging:** Pinned dependency manifests, a Python version declaration, and a Dockerfile suitable for offline test execution.

The current dependency files are `requirements.txt`, `requirements-dev.txt`, and `requirements.lock`. The supported local interpreter is pinned in `.python-version` as Python 3.13.1. The Dockerfile uses the compatible `python:3.13.1-slim-bookworm` image and installs the lockfile directly.

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
│   ├── provider.py        # optional Gemini REST client and safe hybrid planner
│   ├── tools.py           # allowlisted protocol, risk metadata, and safe fixtures
│   ├── workspace.py       # opt-in root-scoped workspace tools and verifiers
│   ├── storage.py         # SQLite repositories and schema
│   ├── services.py        # workflow and memory validation/application services
│   ├── orchestrator.py    # synchronous lifecycle and policy flow
│   ├── evaluation.py      # deterministic offline regression catalog and runner
│   ├── system.py          # bounded capability-aware system measurements
│   └── api.py             # Flask factory, serialization, workers, and dashboard serving
├── frontend/
│   ├── index.html         # server-served command center
│   ├── style.css          # responsive midnight/graphite interface
│   └── script.js          # vanilla API client and safe DOM renderer
├── tests/                 # offline lifecycle and boundary tests
├── .env.example           # safe empty configuration template
├── .gitignore              # excludes secrets and runtime data
├── Dockerfile             # pinned-Python offline build/test environment
├── .python-version         # supported Python version
├── requirements.txt        # runtime dependencies
├── requirements-dev.txt    # development/test dependencies
├── requirements.lock       # pinned runtime and test baseline
├── pyproject.toml         # test and tool configuration
└── README.md
```

The Flask API in `api.py` is the HTTP boundary around this core. The dashboard is served at `/` and its CSS/JavaScript assets are served below `/assets/`. `provider.py` is the only provider boundary; platform-specific adapters remain future layers.

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

The factory does not bind a socket or make provider network calls. Its default
mode is local and deterministic, and its default request body limit is 64 KiB.
Tests can inject an existing `AgentCore` with `create_app(core=core)` and use
the same shutdown hook to wait for bounded workers.

### Optional workspace capability

Workspace access is disabled by default. To enable it, configure one existing
directory explicitly when constructing the core or Flask app:

```python
from pathlib import Path

from arrax import AgentCore, create_app

core = AgentCore(db_path="arrax.sqlite", workspace_root=Path(".").resolve())
app = create_app(core=core)
```

The factory also accepts `workspace_root="C:/approved/project"`, reads the
`ARRAX_WORKSPACE_ROOT` app configuration value, or reads the
`ARRAX_WORKSPACE_ROOT` environment variable. The safe module entry point passes
that environment variable through:

PowerShell:

```powershell
$env:PYTHONPATH = "src"
$env:ARRAX_WORKSPACE_ROOT = (Get-Location).Path
python -m arrax
```

An unset or blank value leaves the application fixture-only. The configured
root must already exist, be a directory, and not itself be a symlink. The API
capability response lists the workspace tools only when they are actually
registered; it never returns the absolute root.

The dashboard's **Workspace readiness report** scenario is disabled until the
capability response contains the workspace tools. When enabled, it submits the
real structured `workspace_report` goal. The task first runs a read-only
snapshot, pauses at an explicit sensitive approval gate, writes
`ARRAX_READINESS_REPORT.md` inside the approved root, and verifies the actual
file and marker. A report path that already exists is rejected unless the goal
explicitly requests overwrite and the plan is approved.

### Optional Gemini provider

Provider configuration is parsed from `ARRAX_AI_MODE`, `GEMINI_API_KEY`, and
`GEMINI_MODEL`. The safe default is `auto` with no key, which starts and stays
in deterministic mode without making a network call. `deterministic` disables
Gemini explicitly. `auto` uses Gemini only after a server-side check confirms
the exact configured model is present in the authenticated `models` response,
supports `generateContent`, and answers a minimal canary request. `gemini`
requires that verification and returns a clear planning failure when it cannot
be completed; it does not silently fall back.

`gemini-3.8-flash` in `.env.example` is a candidate model name only. Model
availability, account access, quotas, and pricing are account-dependent; this
project does not claim free-tier access or access to any particular account.
The API key is read and used only by the server-side REST client. It is never
returned by status, task, event, exception, or frontend responses.

Provider status is available without network I/O from `/api/health`,
`/api/capabilities`, and `GET /api/provider/status`. The last endpoint accepts
`?verify=1` for an explicit authenticated model-list and canary check. Normal
health polling never verifies or calls Gemini. Status distinguishes configured,
verified, deterministic fallback, missing-key, unavailable-model, rate-limit,
timeout, network, and invalid-response states without returning the external
response body.

The unauthenticated local control plane is intentionally loopback-only when
started with `python -m arrax`. `ARRAX_HOST=localhost`, `127.0.0.1`, and `::1`
are accepted; non-loopback hosts are rejected until a real authentication and
authorization boundary is implemented.

### Reusable workflows and safe memory

Reusable workflows are stored in the same SQLite database as tasks and events.
Creating a workflow validates its name, description, version, bounded JSON goal,
deterministic goal contract, registered tools, tool inputs, verifier, and risk
floor. It does not create a task or execute a tool. The supported workflow
version is `1`; request objects reject unknown fields.

Workflow routes:

```text
GET    /api/workflows
POST   /api/workflows
GET    /api/workflows/<id>
DELETE /api/workflows/<id>
POST   /api/workflows/<id>/run
```

Create a workflow with a JSON object containing `name`, `description`, and
`goal`; `version` is optional and defaults to `1`:

```json
{
  "name": "Offline greeting",
  "description": "Run the deterministic success fixture",
  "goal": {"type": "success", "message": "hello"}
}
```

`POST /api/workflows/<id>/run` creates a normal persisted AgentCore task and
submits it to the existing bounded worker manager. It returns the same task
document as the task API, normally with HTTP `202`. Missing workflows, invalid
goals, duplicate names, worker capacity failures, and malformed JSON use the
stable `{"error": {"code": "...", "message": "..."}}` response shape.

### Task history and command-center aggregates

Task records and events remain available after an application reload. The
bounded history route returns recent task summaries without loading full plans
or event payloads:

```text
GET /api/tasks/history?limit=20&offset=0
GET /api/tasks/overview
```

History limits are capped at 50 records per page and offsets are bounded. The
overview is derived from SQLite task and event records and reports active task
count, verified completions, failures, partial completions, approval waits,
and persisted `step.recovering` events. It contains no fabricated telemetry.
The dashboard uses this route for the Command Center's operational overview
and the Live Console uses the history route to restore a previous run after a
refresh.

User-controlled memory is persisted as records with `id`, `key`, `value`,
`category`, `created_at`, and `updated_at`. Values must be bounded JSON and
are returned only after validation. Posting an existing key updates that record
and retains its ID. Credential-like keys or nested values containing `api_key`,
`password`, `token`, `secret`, `private_key`, `authorization`, or `bearer` are
rejected without echoing the rejected value. Absolute machine-path values are
also rejected so memory responses remain local-data safe.

Memory routes:

```text
GET    /api/memory
POST   /api/memory
DELETE /api/memory/<id>
POST   /api/memory/reset
```

Reset accepts only an empty JSON object (or an empty body) and is the only
memory reset operation. It returns the number of deleted records. Workflow
deletion and memory reset are explicit operations; normal startup never
deletes user data. Memory and workflow responses contain JSON-safe values and
do not expose database paths, machine paths, or credentials.

### Capability-aware system snapshot

The local API exposes two read-only system-intelligence routes:

```text
GET /api/system/capabilities
GET /api/system/snapshot
```

`/api/system/capabilities` describes whether each metric can be collected and
reports the bounded process output/scan limits. It does not collect a snapshot.
`/api/system/snapshot` returns a JSON document with this shape:

```text
{
  "measured_at": "UTC timestamp",
  "available": true,
  "cpu": {"status": "measured", "percent": 0.0},
  "memory": {"status": "measured", "total_bytes": 0, "used_bytes": 0, "percent": 0.0},
  "disk": {"status": "measured", "target": "working_directory", "total_bytes": 0, "used_bytes": 0, "free_bytes": 0, "percent": 0.0},
  "network": {"status": "measured", "bytes_sent": 0, "bytes_received": 0},
  "processes": {"status": "measured", "items": [{"name": "python", "cpu_percent": 0.0, "memory_percent": 0.0}]},
  "recommendations": []
}
```

Each metric also includes `available` and `source`. A metric that cannot be
collected has `status: "unavailable"`, null metric values (or an empty process
list), and a fixed safe reason; the service never substitutes zeroes. The
snapshot-level `available` flag is true only when at least one psutil-backed
metric was measured successfully. Disk usage is collected with
`shutil.disk_usage` against the application-configured `ARRAX_SYSTEM_DISK_PATH`
or the current working directory, but only the labels `configured_path` and
`working_directory` are returned. The configured path is never echoed.

CPU collection uses psutil's non-blocking `interval=0.0` call. Process
collection asks only for name, CPU percentage, and memory percentage, scans at
most 50 processes by default, and returns at most 5 summaries. Process names
are reduced to bounded basenames. No process IDs, command lines, executable
paths, user names, or arbitrary file contents are read or returned. The
limits can be reduced through the application configuration keys
`ARRAX_SYSTEM_PROCESS_LIMIT` and `ARRAX_SYSTEM_PROCESS_SCAN_LIMIT`; the service
enforces hard maximums of 20 and 100 respectively.

Recommendations are observations rather than alerts. They are emitted only
when a measured CPU, memory, or disk percentage is at least 90%, and include
the measured value and the `>= 90` threshold. No recommendation is emitted
for unavailable data. If the optional `psutil` import is unavailable, CPU,
memory, network, and process capabilities are explicitly unavailable while
safe standard-library disk usage may still be reported. The pinned dependency
is installed by the normal lockfile path, but the runtime fallback remains
for embedders that do not install optional dependencies. The service is
injectable through `create_app(system_service=...)` and is safe to use on
Linux/Docker and Windows.

### Dashboard sections and browser voice fallback

The dashboard navigation scrolls to nine functional sections:

- **Command Center** creates a normal deterministic task from the selected scenario.
- **Command Center** also shows measured aggregate task state from persisted records: active tasks, verified completions, failures/partial completions, recovery events, and total runs.
- After an explicit successful provider check, the composer enables **Verified Gemini goal**. Its bounded text becomes a provider planning request; Gemini can propose only a structured plan using registered tools, while ARRAX still owns approval, risk, timeout, cancellation, recovery, and verification decisions. Without a verified provider, the option remains disabled.
- **Automation Studio** loads persisted workflows, saves the current structured scenario goal, and provides explicit run and confirmed delete controls. Saving never starts a task. A workflow run is shown in the existing Live Console task inspector and uses the normal task/event polling endpoints.
- **Developer Intelligence** reports the actual registered workspace tools and provides a quick action into the approved workspace-readiness workflow. It clearly marks broader diff analysis, test execution, and unrestricted shell control as unavailable rather than presenting disconnected controls.
- **Live Console** shows the selected task's persisted plan and event history.
- **System Intelligence** loads measurement capabilities once and fetches a single measured snapshot only when the user presses the button. CPU, memory, disk, network, and bounded process summaries are rendered only from measured API fields. Unavailable metrics remain labelled unavailable, and API-provided threshold recommendations are shown with their measured timestamp.
- **Memory Center** loads actual records, saves bounded text values, supports confirmed record deletion, and exposes the explicit confirmed reset operation. Credential-like values are rejected by the API; the browser clears a rejected submission and does not repeat it in a status message.
- **Evaluation Lab**, **Security & Capabilities**, and **Runbook / How it works** retain their existing API-backed behavior.

Security & Capabilities includes an explicit **Verify live provider** action.
It calls `GET /api/provider/status?verify=1`; normal dashboard startup does not
contact Gemini. A verified result is shown only after the backend has completed
the configured model-list and canary checks, and missing credentials remain an
explicit deterministic fallback state.

The scenario composer includes a push-to-talk fallback. When the browser exposes
`SpeechRecognition` or `webkitSpeechRecognition`, pressing the button starts one
recognition session and places its bounded transcript in the scenario textarea.
The browser requests microphone permission only after that user action; there is
no always-on recording. A separate read-last-status control uses
`speechSynthesis` when available. Browsers without either API show an explicit
unsupported/unavailable state and the dashboard does not claim that voice is
working. Text entry and all non-voice controls remain available.

### Offline Evaluation Lab

The Evaluation Lab is a deterministic regression evaluation, not a production
performance benchmark. It does not call Gemini, use the configured workspace,
or read the application's SQLite database. The catalog endpoint reports the
actual bounded scenarios and their expected outcomes:

```text
GET /api/evaluation/scenarios
```

Run all scenarios with `{}` or run one catalog name. The route is deliberately
synchronous because the five fixture scenarios are small and bounded:

```powershell
$env:PYTHONPATH = "src"
python -c "from arrax.evaluation import run_evaluation; print(run_evaluation())"

# HTTP form, with the local app already running:
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5000/api/evaluation/run `
  -ContentType "application/json" -Body '{}'
```

The report includes each measured elapsed duration, final status, expected
checks, verification evidence, recovery event count, approval behavior, error
category, and summary counts/rates. Its measured durations are local regression
observations only; the report makes no production throughput or latency claim.
The dashboard's **Evaluation Lab** section uses these same endpoints and shows
loading, empty, error, catalog, and actual report states without simulated
values.

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

The `.env.example` file is present as a safe template. Its empty
`GEMINI_API_KEY` keeps the default provider path deterministic. Never put a real
key in Git or expose it to browser code.

The documented provider variables are:

| Variable | Meaning |
| --- | --- |
| `ARRAX_AI_MODE` | `deterministic`, `auto`, or strict `gemini`; defaults to `auto`. |
| `GEMINI_API_KEY` | Optional server-side key. Blank means deterministic fallback in `auto`. |
| `GEMINI_MODEL` | Exact model name checked at runtime; the example is account-dependent. |
| `ARRAX_WORKSPACE_ROOT` | Optional approved workspace root, intentionally blank by default. |
| `ARRAX_SYSTEM_DISK_PATH` | Optional application-owned disk target; responses expose only a safe label. Blank uses the current working directory. |

Real API keys, passwords, tokens, personal data, and machine-specific paths
must remain outside Git.

## Database and memory

The local SQLite boundary uses only the stdlib `sqlite3` module. Pass a
filesystem path to `AgentCore` for persistence or use `":memory:"` for isolated
tests. Task, step, event, workflow, and memory repositories share the store's
transaction and lock boundary, and workflow/memory services validate before
calling their repositories.

The workflow and memory storage contracts:

- Workflows use a stable generated ID, unique name, description, validated
  version, structured goal JSON, and created/updated UTC timestamps.
- Memory uses a stable generated ID, unique upsert key, JSON-safe value,
  category, and created/updated UTC timestamps.
- Workflow goals are limited to the existing deterministic planner contract or
  currently registered allowlisted tools. Shell, network, and arbitrary action
  names are not persisted.
- Workflow goals are limited to 16 KiB and 12 nesting levels. Memory values are
  limited to 16 KiB and 8 nesting levels. Names, descriptions, keys, and
  categories have separate input bounds.
- Memory rejects credential-like fields and values before SQLite writes them.
- No startup path resets workflows or memory. Only the explicit workflow DELETE
  and memory reset endpoints remove user-controlled records.

The project does not claim MySQL compatibility; SQLite is the deliberate local
persistence boundary for this milestone.

## Running the application

The current milestone adds the opt-in workspace report flow, provider status,
and the offline Evaluation Lab on top of the local dashboard. Use the
application factory shown above, configure an approved root when the real
report demo is desired, then run the offline test suite.

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

For a Docker judge-style run, build the pinned-Python image and execute its
safe test default with network access disabled:

```text
docker build --pull=false --tag arrax-offline .
docker run --rm --network=none arrax-offline
```

Docker validation is run only when the `docker` command is available in the
validation environment. If Docker is unavailable, report that fact and do not
install it as part of the offline checks.

Provider tests use fake transports and fake providers only. The automated test
suite never performs a live Gemini request.

The target is one offline command that exercises the complete safe test suite, including:

- Request validation.
- Task-state transitions.
- Tool-schema validation.
- Permission and approval decisions.
- Timeout and cancellation behavior.
- Result verification.
- System snapshot schema, unavailable capability fallback, bounded process output, and path-redaction behavior.
- Workflow schema persistence/reload, deterministic-goal validation, safe run lifecycle, and structured API errors.
- Memory CRUD/upsert/reset lifecycle, bounded JSON validation, and credential-like data rejection without echoing values.
- persistence and event-history reloads.
- Workspace path containment, symlink rejection, bounded text I/O, safe report
  writes, actual-file verification, risk floors, and opt-in API capabilities.

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
- Keep external AI optional, server-side, verified, bounded, and mockable.
- Validate reusable workflows against the deterministic planner and allowlisted
  registry before persistence; saving a workflow never executes it.
- Reject credential-like memory keys and values before persistence, and return
  only bounded JSON-safe memory documents.

The workspace boundary adds these controls:

- No workspace tool is registered without an explicit root.
- Absolute paths, drive-qualified paths, parent traversal, missing parents,
  symlinks, symlink directories, non-regular files, and paths outside the
  resolved root are rejected.
- Reads and searches have strict input, file, file-count, result-count, and
  byte limits. Text reads require valid bounded UTF-8 and reject binary-like
  content.
- Search and list results contain relative paths only. Reports contain a
  deterministic snapshot of relative entries and sizes, never the absolute
  root.
- Report writes use a same-directory temporary file and atomic destination
  creation/replacement. Existing files are not overwritten by default, and
  overwrite requires both an explicit input and an approved sensitive plan.
- `workspace.verify_report` and `workspace.verify_snapshot` recompute evidence
  from the actual workspace instead of trusting a caller's `verified` field.
- Registry risk metadata is authoritative: a structured plan cannot relabel a
  sensitive workspace writer as `read_only`, and verifiers must be read-only.

This project will not claim that a tool is successful until its completion can be verified. A safety policy is not a replacement for operating-system security, authentication, backups, or user review.

## Known issues and limitations

The current known limitations are:

- No production deployment is supported.
- No live provider access is assumed or required. A configured Gemini account is
  checked only when explicitly verified or when `auto` first needs a provider
  plan.
- The dashboard preserves deterministic scenario inputs; it does not claim
  arbitrary natural-language execution.
- No desktop automation adapter is available.
- Workspace access is opt-in and local-only; it does not provide arbitrary
  filesystem access, recursive unbounded reads, shell execution, network
  access, or a general-purpose file server.
- Workspace snapshots are bounded. Very large trees, oversized files, binary
  files, symlinks, and unsupported filesystem entries fail safely instead of
  being partially trusted.
- The readiness report is deterministic for a stable workspace, but it is not
  a backup, security scan, or proof that files are safe to execute.
- No authentication or production deployment is supported.
- System snapshots are point-in-time observations only. CPU utilization is a
  non-blocking sample, process CPU values depend on the operating-system
  backend's sampling state, and no historical trend is retained.
- System metric scope is the local host or container visible to the process;
  containers may report container-specific limits and counters. Missing OS
  permissions or an absent optional `psutil` installation produce explicit
  unavailable metrics rather than estimated values.
- The final supported platform has not yet been validated.
- Docker validation is environment-dependent; the documented judge-style build
  and network-disabled test run is not claimed as executed when Docker is not
  available.

These items will be removed from this list only after implementation and verification.

## Roadmap

1. Establish the project configuration and reproducible test command. **Complete.**
2. Implement a minimal task lifecycle with deterministic test tools. **Complete.**
3. Add the API boundary and safe execution-status responses. **Complete.**
4. Add a small dashboard for task input, plan review, approval, and results. **Complete.**
5. Add cancellation, timeouts, and result verification. **Complete.**
6. Add an opt-in bounded workspace report flow with actual verifiers. **Complete.**
7. Add optional AI planning behind strict schema and permission validation. **Complete.**
8. Add an offline deterministic Evaluation Lab and reproducibility foundation. **Complete.**
9. Add optional platform adapters only after the core workflow is tested.

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
