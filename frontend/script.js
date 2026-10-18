(() => {
  "use strict";

  const TERMINAL_STATUSES = new Set([
    "completed",
    "partially_completed",
    "failed",
    "cancelled",
  ]);
  const POLL_INTERVAL_MS = 650;
  const POLL_RETRY_LIMIT = 4;
  const POLL_RETRY_BASE_MS = 450;
  const MAX_RECENT_RUNS = 8;
  const CURRENT_TASK_STORAGE_KEY = "arrax.currentTaskId";
  const SECTION_IDS = [
    "command-center",
    "automation-studio",
    "developer-intelligence",
    "live-console",
    "system-intelligence",
    "memory-center",
    "evaluation-lab",
    "security-capabilities",
    "runbook",
  ];

  const MODE_DETAILS = {
    success: {
      help: "Runs fixture.success and requires fixture.verify evidence before completion.",
      fallback: "verified local action",
    },
    ai_goal: {
      help: "Sends the bounded natural-language goal to a verified Gemini planner. The backend still enforces registered tools, approvals, timeouts, and verification.",
      fallback: "Describe the controlled outcome you want ARRAX to plan",
    },
    failure_recovery: {
      help: "Runs fixture.fail_once, records the injected failure, and retries within the plan limit.",
      fallback: "recovery-demo",
    },
    approval: {
      help: "Pauses fixture.approval at an explicit approval gate before execution.",
      fallback: "approval-gated local action",
    },
    timeout: {
      help: "Runs a 200 ms bounded delay against a 50 ms tool budget to expose the timeout boundary.",
      fallback: "bounded timeout demo",
    },
    workspace_report: {
      help: "Requires an API configured with an explicit ARRAX_WORKSPACE_ROOT. Lists the approved workspace, pauses for approval, writes a bounded report, and verifies the real file.",
      fallback: "approved workspace readiness report",
    },
  };

  const WORKFLOW_TOOL_CATALOG = {
    "fixture.success": {
      label: "Verified action",
      risk: "read_only",
      approval: false,
      maxAttempts: 1,
      timeoutSeconds: 1,
      inputKind: "message",
      fallback: "verified workflow step",
    },
    "fixture.fail_once": {
      label: "Recoverable action",
      risk: "reversible",
      approval: false,
      maxAttempts: 2,
      timeoutSeconds: 1,
      inputKind: "key",
      fallback: "workflow-recovery",
    },
    "fixture.approval": {
      label: "Approval-gated action",
      risk: "sensitive",
      approval: true,
      maxAttempts: 1,
      timeoutSeconds: 1,
      inputKind: "message",
      fallback: "approved workflow step",
    },
    "fixture.delay": {
      label: "Bounded delay",
      risk: "read_only",
      approval: false,
      maxAttempts: 1,
      timeoutSeconds: 1,
      inputKind: "seconds",
      fallback: "0.01",
    },
  };

  const WORKFLOW_TOOL_NAMES = Object.keys(WORKFLOW_TOOL_CATALOG);

  const STATUS_DETAILS = {
    queued: ["Queued", "The task is persisted and waiting for a local worker."],
    planning: ["Planning", "The deterministic planner is creating the execution plan."],
    awaiting_approval: ["Awaiting approval", "A sensitive step is paused until you approve it."],
    running: ["Running", "An allowlisted fixture tool is executing locally."],
    recovering: ["Recovering", "The core recorded a bounded failure and is attempting recovery."],
    verifying: ["Verifying", "The verifier is inspecting the actual action output."],
    completed: ["Completed", "The task completed with verification evidence."],
    partially_completed: ["Partially completed", "Some plan steps completed while another step failed."],
    failed: ["Failed", "The task ended without a verified successful outcome."],
    cancelled: ["Cancelled", "Cancellation was requested and persisted by the core."],
  };

  const EVENT_DETAILS = {
    "task.created": ["Task created", "neutral"],
    "task.planning": ["Planning started", "neutral"],
    "plan.created": ["Plan created", "positive"],
    "task.running": ["Task running", "positive"],
    "task.status": ["Task status updated", "neutral"],
    "approval.required": ["Approval required", "recovery"],
    "approval.granted": ["Approval granted", "positive"],
    "step.running": ["Step running", "positive"],
    "tool.started": ["Tool started", "positive"],
    "tool.observed": ["Tool observation", "recovery"],
    "step.recovering": ["Recovery started", "recovery"],
    "step.verifying": ["Verification started", "verification"],
    "verification.completed": ["Verification completed", "verification"],
    "verification.failed": ["Verification failed", "warning"],
    "verification.rejected": ["Verification rejected", "warning"],
    "tool.rejected": ["Tool rejected", "warning"],
    "step.failed": ["Step failed", "warning"],
    "task.completed": ["Task completed", "positive"],
    "task.partially_completed": ["Partial completion", "recovery"],
    "task.failed": ["Task failed", "warning"],
    "task.cancel_requested": ["Cancellation requested", "warning"],
    "task.cancelled": ["Task cancelled", "warning"],
  };

  const state = {
    task: null,
    events: [],
    polling: null,
    healthReady: false,
    submitting: false,
    actionPending: false,
    workspaceAvailable: false,
    evaluationCatalog: [],
    evaluationLoading: false,
    workflows: [],
    workflowBusy: null,
    workflowSaving: false,
    workflowSource: "scenario",
    workflowSteps: [],
    memory: [],
    memoryBusy: null,
    memorySaving: false,
    recentRuns: [],
    recentRunsLoading: false,
    overviewLoading: false,
    providerVerifying: false,
    providerVerified: false,
    systemSnapshotLoading: false,
    voice: {
      recognition: null,
      listening: false,
      supported: false,
      speechSupported: false,
    },
    lastStatusMessage: "No status to read yet.",
    controllers: {},
  };

  const elements = {};

  class ApiRequestError extends Error {
    constructor(message, status = 0, code = "request_failed") {
      super(message);
      this.name = "ApiRequestError";
      this.status = status;
      this.code = code;
    }
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function displayValue(value, fallback = "—") {
    if (value === null || value === undefined || value === "") {
      return fallback;
    }
    return String(value);
  }

  function formatData(value) {
    if (value === null || value === undefined) {
      return "—";
    }
    if (typeof value === "string") {
      return value;
    }
    try {
      const serialized = JSON.stringify(value, null, 2);
      return serialized === undefined ? String(value) : serialized;
    } catch (_error) {
      return "[unavailable]";
    }
  }

  function addText(parent, tagName, className, value) {
    const node = document.createElement(tagName);
    if (className) {
      node.className = className;
    }
    node.textContent = displayValue(value);
    parent.append(node);
    return node;
  }

  function addDefinition(parent, label, value, asCode = false) {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    if (asCode) {
      const code = document.createElement("code");
      code.textContent = displayValue(value);
      detail.append(code);
    } else {
      detail.textContent = displayValue(value);
    }
    row.className = "step-fact";
    row.append(term, detail);
    parent.append(row);
  }

  function addOutputDefinition(parent, label, value) {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    const output = document.createElement("pre");
    output.className = "step-output";
    output.textContent = formatData(value);
    detail.append(output);
    row.className = "step-fact";
    row.append(term, detail);
    parent.append(row);
  }

  function setStatusBadge(node, status) {
    const classes = {
      completed: "status-completed",
      partially_completed: "status-partial",
      failed: "status-failed",
      cancelled: "status-cancelled",
      awaiting_approval: "status-awaiting",
      recovering: "status-recovering",
      running: "status-running",
      verifying: "status-verifying",
    };
    node.className = `status-badge ${classes[status] || "status-neutral"}`;
    node.textContent = STATUS_DETAILS[status]?.[0] || displayValue(status, "Unknown");
  }

  function statusDescription(status) {
    return STATUS_DETAILS[status]?.[1] || "The API returned an unrecognized task state.";
  }

  function isTerminal(status) {
    return TERMINAL_STATUSES.has(status);
  }

  function isReducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  async function requestJson(path, options = {}) {
    let response;
    try {
      const headers = new Headers(options.headers || {});
      if (options.body && !headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
      }
      response = await fetch(path, { ...options, headers });
    } catch (error) {
      if (isAbortError(error)) {
        throw error;
      }
      throw new ApiRequestError("The API could not be reached. Start the local Flask app and retry.");
    }

    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      if (isAbortError(error)) {
        throw error;
      }
      data = null;
    }
    if (!response.ok) {
      const serverError = data && data.error;
      const message = serverError && typeof serverError.message === "string"
        ? serverError.message
        : `The API returned HTTP ${response.status}.`;
      throw new ApiRequestError(message, response.status, serverError?.code || "request_failed");
    }
    return data;
  }

  function postJson(path, body) {
    return requestJson(path, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  function deleteJson(path) {
    return requestJson(path, { method: "DELETE" });
  }

  function replaceController(name) {
    state.controllers[name]?.abort();
    const controller = new AbortController();
    state.controllers[name] = controller;
    return controller;
  }

  function setApiNotice(message) {
    elements.apiNotice.hidden = !message;
    elements.apiNoticeText.textContent = message || "";
  }

  function providerDisplay(status) {
    if (status && status.verified === true && (status.mode === "gemini" || status.last_mode === "gemini")) {
      return "Gemini verified";
    }
    if (status && status.configured_mode === "gemini") {
      return "Gemini unverified";
    }
    if (status && status.configured === true && status.configured_mode === "auto") {
      return "Auto / unverified";
    }
    return "Deterministic fallback";
  }

  function executionDisplay(mode) {
    const normalized = String(mode || "").trim().toLowerCase();
    const labels = {
      deterministic_local: "Deterministic local",
      deterministic: "Deterministic",
      unavailable: "Unavailable",
    };
    if (labels[normalized]) {
      return labels[normalized];
    }
    return normalized
      ? normalized.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase())
      : "Unavailable";
  }

  function healthDisplay(status, ready) {
    if (ready) {
      return "Ready";
    }
    const normalized = typeof status === "string" ? status.trim().toLowerCase() : "";
    if (!normalized) {
      return "Unavailable";
    }
    return normalized.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function renderExecutionLabels(mode) {
    const label = executionDisplay(mode);
    elements.executionMode.textContent = `Execution: ${label}`;
    elements.modeChipLabel.textContent = label.toUpperCase();
    elements.capabilityExecution.textContent = label;
  }

  function renderProviderLabels(status) {
    const label = providerDisplay(status);
    elements.providerMode.textContent = `Provider: ${label}`;
    elements.capabilityProvider.textContent = label;
    elements.footerProviderStatus.textContent = `Provider status: ${label}`;
  }

  function renderProviderVerification(status) {
    const providerStatus = status || {};
    state.providerVerified = providerStatus.verified === true;
    const aiGoalOption = elements.scenarioMode.querySelector('option[value="ai_goal"]');
    if (aiGoalOption) {
      aiGoalOption.disabled = !state.providerVerified;
      aiGoalOption.title = state.providerVerified
        ? "Uses the verified server-side Gemini planner; execution remains core-enforced."
        : "Verify the provider in Security & Capabilities before using this mode.";
      if (!state.providerVerified && elements.scenarioMode.value === "ai_goal") {
        elements.scenarioMode.value = "success";
      }
    }
    elements.capabilityProviderModel.textContent = displayValue(providerStatus.model, "unavailable");
    elements.capabilityProviderVerification.textContent = providerStatus.verified === true
      ? "Verified by backend"
      : displayValue(providerStatus.message, "Not verified");
    elements.providerActionStatus.textContent = providerStatus.verified === true
      ? "Live provider verified by the backend canary."
      : displayValue(providerStatus.message, "Live provider is not verified.");
  }

  function renderHealth(health) {
    const ready = health && health.ready === true && health.status === "ready";
    const providerStatus = health?.provider_status || {};
    state.healthReady = ready;
    elements.healthStatus.textContent = healthDisplay(health?.status, ready);
    elements.healthDot.classList.remove("is-checking");
    elements.healthDot.classList.toggle("is-ready", ready);
    elements.healthDot.classList.toggle("is-unavailable", !ready);
    renderExecutionLabels(health?.execution_mode);
    renderProviderLabels(providerStatus);
    renderProviderVerification(providerStatus);
    if (ready) {
      setApiNotice("");
    }
  }

  function renderHealthFailure(error) {
    if (isAbortError(error)) {
      return;
    }
    state.healthReady = false;
    elements.healthStatus.textContent = "Unavailable";
    elements.healthDot.classList.remove("is-checking", "is-ready");
    elements.healthDot.classList.add("is-unavailable");
    renderExecutionLabels("unavailable");
    renderProviderLabels(null);
    renderProviderVerification(null);
    setApiNotice(error.message || "The API is unavailable.");
  }

  function renderCapabilities(capabilities) {
    const tools = Array.isArray(capabilities?.tools) ? capabilities.tools : [];
    state.workspaceAvailable = tools.includes("workspace.list")
      && tools.includes("workspace.create_report")
      && tools.includes("workspace.verify_report");
    const workspaceOption = elements.scenarioMode.querySelector('option[value="workspace_report"]');
    if (workspaceOption) {
      workspaceOption.disabled = !state.workspaceAvailable;
      workspaceOption.title = state.workspaceAvailable
        ? "Uses the explicitly configured approved workspace root."
        : "Configure ARRAX_WORKSPACE_ROOT and restart the API to enable this mode.";
      if (!state.workspaceAvailable && elements.scenarioMode.value === "workspace_report") {
        elements.scenarioMode.value = "success";
      }
    }
    const workspaceTools = tools.filter((tool) => typeof tool === "string" && tool.startsWith("workspace."));
    elements.developerToolList.replaceChildren();
    elements.developerToolCount.textContent = workspaceTools.length
      ? `${workspaceTools.length} registered`
      : "Unavailable";
    elements.developerWorkspaceStatus.textContent = state.workspaceAvailable
      ? "Approved workspace available"
      : "Approved workspace unavailable";
    elements.openWorkspaceReport.disabled = !state.workspaceAvailable;
    elements.developerActionStatus.textContent = state.workspaceAvailable
      ? "Ready to compose the bounded report workflow."
      : "Configure ARRAX_WORKSPACE_ROOT and restart the API to enable this flow.";
    if (!workspaceTools.length) {
      addText(elements.developerToolList, "li", "tool-empty", "No workspace tools registered by the API.");
    } else {
      workspaceTools.forEach((tool) => addText(elements.developerToolList, "li", "", tool));
    }
    elements.toolList.replaceChildren();
    elements.toolCount.textContent = `${tools.length} registered`;
    renderExecutionLabels(capabilities?.execution_mode);
    const providerStatus = capabilities?.provider_status || {};
    renderProviderLabels(providerStatus);
    renderProviderVerification({
      ...providerStatus,
      model: capabilities?.configured_model || providerStatus.model,
    });
    if (!tools.length) {
      addText(elements.toolList, "li", "tool-empty", "No registered tools reported by the API.");
      return;
    }
    tools.forEach((tool) => {
      addText(elements.toolList, "li", "", tool);
    });
  }

  function renderCapabilitiesFailure(error) {
    if (isAbortError(error)) {
      return;
    }
    state.workspaceAvailable = false;
    const workspaceOption = elements.scenarioMode.querySelector('option[value="workspace_report"]');
    if (workspaceOption) {
      workspaceOption.disabled = true;
    }
    elements.toolList.replaceChildren();
    addText(elements.toolList, "li", "tool-empty", "Capabilities unavailable.");
    elements.toolCount.textContent = "Unavailable";
    elements.developerToolList.replaceChildren();
    elements.developerToolCount.textContent = "Unavailable";
    elements.developerWorkspaceStatus.textContent = "Workspace capability unavailable";
    elements.openWorkspaceReport.disabled = true;
    elements.developerActionStatus.textContent = "The capabilities endpoint could not be reached.";
    renderExecutionLabels("unavailable");
    renderProviderLabels(null);
    renderProviderVerification(null);
    if (!state.healthReady) {
      setApiNotice(error.message || "The API is unavailable.");
    }
  }

  function renderTaskOverview(documentValue) {
    const overview = documentValue?.overview && typeof documentValue.overview === "object"
      ? documentValue.overview
      : documentValue;
    const number = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
    const active = number(overview?.active);
    const completed = number(overview?.completed);
    const failed = number(overview?.failed);
    const partial = number(overview?.partially_completed);
    const recovery = number(overview?.recovery_event_count);
    const total = number(overview?.total);
    elements.overviewActive.textContent = String(active);
    elements.overviewCompleted.textContent = String(completed);
    elements.overviewFailed.textContent = String(failed + partial);
    elements.overviewRecovery.textContent = String(recovery);
    elements.overviewTotal.textContent = String(total);
    elements.overviewActiveDetail.textContent = number(overview?.awaiting_approval) > 0
      ? `${number(overview.awaiting_approval)} awaiting approval`
      : "Queued and executing task records";
    elements.overviewFailedDetail.textContent = partial > 0
      ? `${failed} failed · ${partial} partially completed`
      : `${failed} failed · no partial completions`;
    elements.overviewLastUpdated.textContent = overview?.last_updated_at
      ? `Updated ${overview.last_updated_at}`
      : "No persisted task updates yet";
    elements.overviewSyncLabel.textContent = `${total} persisted ${total === 1 ? "run" : "runs"} measured`;
  }

  function renderTaskOverviewFailure(error) {
    if (isAbortError(error)) {
      return;
    }
    [elements.overviewActive, elements.overviewCompleted, elements.overviewFailed, elements.overviewRecovery, elements.overviewTotal]
      .forEach((node) => { node.textContent = "—"; });
    elements.overviewActiveDetail.textContent = "Task aggregates unavailable";
    elements.overviewFailedDetail.textContent = "Task aggregates unavailable";
    elements.overviewLastUpdated.textContent = "Last update unavailable";
    elements.overviewSyncLabel.textContent = error?.message || "Task aggregates unavailable";
  }

  async function verifyProvider() {
    if (state.providerVerifying) {
      return;
    }
    state.providerVerifying = true;
    elements.verifyProvider.disabled = true;
    elements.verifyProviderLabel.textContent = "Verifying…";
    elements.providerActionStatus.textContent = "Requesting an explicit backend provider verification…";
    setApiNotice("");
    try {
      const status = await requestJson("/api/provider/status?verify=1");
      renderProviderLabels(status);
      renderProviderVerification(status);
      if (status?.verified === true) {
        setApiNotice("");
      }
    } catch (error) {
      if (!isAbortError(error)) {
        elements.providerActionStatus.textContent = error?.message || "Provider verification failed.";
        setApiNotice(error?.message || "Provider verification failed.");
      }
    } finally {
      state.providerVerifying = false;
      elements.verifyProvider.disabled = false;
      elements.verifyProviderLabel.textContent = "Verify live provider";
    }
  }

  function openWorkspaceReport() {
    if (!state.workspaceAvailable) {
      elements.developerActionStatus.textContent = "Workspace reporting is unavailable until the backend exposes approved workspace tools.";
      return;
    }
    elements.scenarioMode.value = "workspace_report";
    updateModeHelp();
    renderWorkflowGoalPreview();
    setFormStatus("Workspace report mode selected. Review the bounded plan, then start the controlled run.");
    scrollToSection("command-center");
    elements.scenarioContext.focus();
  }

  function setRecentRunsState(stateName) {
    elements.recentRunsLoading.hidden = stateName !== "loading";
    elements.recentRunsEmpty.hidden = stateName !== "empty";
    elements.recentRunsUnavailable.hidden = stateName !== "unavailable";
    elements.recentRunsList.hidden = stateName !== "ready";
  }

  function recentTasksFromDocument(documentValue) {
    if (Array.isArray(documentValue)) {
      return documentValue;
    }
    if (Array.isArray(documentValue?.tasks)) {
      return documentValue.tasks;
    }
    if (Array.isArray(documentValue?.items)) {
      return documentValue.items;
    }
    return [];
  }

  function recentRunSummary(task) {
    const goal = task?.goal;
    if (goal && typeof goal === "object") {
      return displayValue(goal.message || goal.key || goal.type, "Structured task");
    }
    return "Structured task";
  }

  function renderRecentRuns(documentValue) {
    const tasks = recentTasksFromDocument(documentValue)
      .filter((task) => task && typeof task.task_id === "string" && task.task_id.trim())
      .slice(0, MAX_RECENT_RUNS);
    state.recentRuns = tasks;
    elements.recentRunsList.replaceChildren();
    elements.recentRunsCount.textContent = `${tasks.length} ${tasks.length === 1 ? "run" : "runs"}`;
    if (!tasks.length) {
      setRecentRunsState("empty");
      return;
    }

    tasks.forEach((task) => {
      const item = document.createElement("li");
      const open = document.createElement("button");
      open.type = "button";
      open.className = "recent-run-item";
      open.setAttribute("aria-label", `Open run ${task.task_id}`);
      const main = document.createElement("span");
      main.className = "recent-run-main";
      addText(main, "strong", "recent-run-summary", recentRunSummary(task));
      addText(main, "span", "recent-run-id", task.task_id);
      addText(main, "time", "recent-run-time", task.updated_at || task.created_at);
      const status = document.createElement("span");
      setStatusBadge(status, task.status);
      open.append(main, status);
      open.addEventListener("click", () => {
        void restoreTask(task.task_id, { scroll: true, source: "recent-runs" });
      });
      item.append(open);
      elements.recentRunsList.append(item);
    });
    setRecentRunsState("ready");
  }

  function renderRecentRunsUnavailable(message) {
    state.recentRuns = [];
    elements.recentRunsList.replaceChildren();
    elements.recentRunsCount.textContent = "Unavailable";
    elements.recentRunsUnavailableMessage.textContent = message || "The task index is not available in this API version.";
    setRecentRunsState("unavailable");
  }

  async function loadRecentRuns() {
    if (state.recentRunsLoading) {
      return;
    }
    state.recentRunsLoading = true;
    const controller = replaceController("recentRuns");
    elements.recentRunsCount.textContent = "Loading";
    setRecentRunsState("loading");
    try {
      let documentValue;
      try {
        documentValue = await requestJson("/api/tasks/history?limit=8", { signal: controller.signal });
      } catch (error) {
        const historyUnavailable = error?.status === 404 || error?.status === 405 || error?.status === 501;
        if (!historyUnavailable) {
          throw error;
        }
        documentValue = await requestJson("/api/tasks?limit=8", { signal: controller.signal });
      }
      if (state.controllers.recentRuns !== controller) {
        return;
      }
      renderRecentRuns(documentValue);
    } catch (error) {
      if (isAbortError(error) || state.controllers.recentRuns !== controller) {
        return;
      }
      const unavailable = error?.status === 404 || error?.status === 405 || error?.status === 501;
      renderRecentRunsUnavailable(unavailable
        ? "Recent runs are not available from this API version yet."
        : (error?.message || "Recent runs could not be loaded."));
    } finally {
      if (state.controllers.recentRuns === controller) {
        state.recentRunsLoading = false;
      }
    }
  }

  function setEvaluationState(stateName) {
    elements.evaluationEmptyState.hidden = stateName !== "empty";
    elements.evaluationLoadingState.hidden = stateName !== "loading";
    elements.evaluationErrorState.hidden = stateName !== "error";
    elements.evaluationResults.hidden = stateName !== "results";
  }

  function evaluationOutcomeText(outcome) {
    if (!outcome || typeof outcome !== "object") {
      return "Expected outcome unavailable.";
    }
    const details = [`status: ${displayValue(outcome.final_status, "unknown")}`];
    if (outcome.requires_verification === true) {
      details.push("verification evidence required");
    }
    if (outcome.requires_recovery_event === true) {
      details.push("recovery event required");
    }
    if (outcome.requires_approval_pause === true) {
      details.push("approval pause required");
    }
    if (typeof outcome.error_category === "string") {
      details.push(`error: ${outcome.error_category}`);
    }
    return details.join(" · ");
  }

  function renderEvaluationCatalog(catalogDocument) {
    const scenarios = Array.isArray(catalogDocument?.scenarios) ? catalogDocument.scenarios : [];
    state.evaluationCatalog = scenarios;
    elements.evaluationScenarioSelect.replaceChildren();
    const allOption = document.createElement("option");
    allOption.value = "";
    allOption.textContent = "All catalog scenarios";
    elements.evaluationScenarioSelect.append(allOption);
    elements.evaluationCatalogList.replaceChildren();
    scenarios.forEach((scenario) => {
      const option = document.createElement("option");
      option.value = displayValue(scenario?.name, "");
      option.textContent = displayValue(scenario?.name, "Unnamed scenario");
      elements.evaluationScenarioSelect.append(option);

      const item = document.createElement("li");
      item.className = "evaluation-catalog-item";
      const heading = document.createElement("div");
      heading.className = "evaluation-catalog-heading";
      addText(heading, "strong", "evaluation-scenario-name", scenario?.name);
      addText(heading, "span", "evaluation-expected", evaluationOutcomeText(scenario?.expected_outcome));
      addText(item, "p", "evaluation-description", scenario?.description);
      item.prepend(heading);
      elements.evaluationCatalogList.append(item);
    });
    elements.evaluationScenarioSelect.disabled = scenarios.length === 0;
    elements.runEvaluation.disabled = scenarios.length === 0 || state.evaluationLoading;
    elements.evaluationModeLabel.textContent = catalogDocument?.offline === true && catalogDocument?.deterministic === true
      ? "Offline mode: deterministic"
      : "Offline mode: unavailable";
    elements.evaluationCatalogCount.textContent = `${scenarios.length} ${scenarios.length === 1 ? "scenario" : "scenarios"}`;
    if (scenarios.length) {
      setEvaluationState("empty");
    } else {
      showEvaluationError("The API returned an empty evaluation catalog.");
    }
  }

  function showEvaluationError(message) {
    elements.evaluationErrorMessage.textContent = message || "The evaluation report could not be loaded.";
    setEvaluationState("error");
  }

  function renderEvaluationCatalogFailure(error) {
    if (isAbortError(error)) {
      return;
    }
    state.evaluationCatalog = [];
    elements.evaluationScenarioSelect.replaceChildren();
    elements.evaluationScenarioSelect.disabled = true;
    elements.runEvaluation.disabled = true;
    elements.evaluationCatalogList.replaceChildren();
    addText(elements.evaluationCatalogList, "li", "evaluation-empty", "Evaluation catalog unavailable.");
    elements.evaluationCatalogCount.textContent = "Unavailable";
    elements.evaluationModeLabel.textContent = "Offline mode: unavailable";
    showEvaluationError(error?.message || "The evaluation catalog could not be loaded.");
  }

  function formatEvaluationRate(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      return "—";
    }
    return `${(value * 100).toFixed(1)}%`;
  }

  function renderEvaluationReport(report) {
    const results = Array.isArray(report?.scenario_results) ? report.scenario_results : [];
    const summary = report?.summary && typeof report.summary === "object" ? report.summary : {};
    elements.evaluationRunTimestamp.textContent = displayValue(report?.run_timestamp);
    elements.evaluationCompletionRate.textContent = formatEvaluationRate(summary.completion_rate);
    elements.evaluationRecoveryRate.textContent = formatEvaluationRate(summary.recovery_rate);
    elements.evaluationVerificationRate.textContent = formatEvaluationRate(summary.verification_rate);
    elements.evaluationResultCount.textContent = `${displayValue(summary.passed_count, "0")} passed / ${results.length} ${results.length === 1 ? "scenario" : "scenarios"}`;
    elements.evaluationResultList.replaceChildren();
    if (!results.length) {
      addText(elements.evaluationResultList, "p", "evaluation-empty", "The API returned no scenario results.");
    }
    results.forEach((result) => {
      const card = document.createElement("article");
      card.className = `evaluation-result-card ${result?.passed === true ? "is-passed" : "is-failed"}`;
      const header = document.createElement("div");
      header.className = "evaluation-result-header";
      const title = document.createElement("div");
      addText(title, "h4", "", result?.name);
      addText(title, "p", "evaluation-result-description", result?.description);
      const stateLabel = result?.passed === true ? "Passed" : "Failed";
      addText(header, "span", `evaluation-result-status ${result?.passed === true ? "is-passed" : "is-failed"}`, stateLabel);
      header.prepend(title);
      card.append(header);

      const facts = document.createElement("dl");
      facts.className = "evaluation-facts";
      const elapsed = typeof result?.elapsed_seconds === "number" && Number.isFinite(result.elapsed_seconds)
        ? `${result.elapsed_seconds.toFixed(6)} s`
        : "—";
      addDefinition(facts, "Elapsed", elapsed);
      addDefinition(facts, "Final status", result?.final_status);
      addDefinition(facts, "Error category", result?.error_category);
      addDefinition(facts, "Recovery events", result?.recovery_event_count);
      addDefinition(facts, "Completion events", result?.completion_event_count);
      addOutputDefinition(facts, "Approval behavior", result?.approval_behavior);
      addOutputDefinition(facts, "Expected checks", result?.expected_checks);
      addOutputDefinition(facts, "Verification evidence", result?.verification_evidence);
      card.append(facts);
      elements.evaluationResultList.append(card);
    });
    elements.evaluationNote.textContent = displayValue(report?.notes, "");
    setEvaluationState("results");
  }

  async function runEvaluation() {
    if (state.evaluationLoading || !state.evaluationCatalog.length) {
      return;
    }
    state.evaluationLoading = true;
    elements.runEvaluation.disabled = true;
    elements.evaluationScenarioSelect.disabled = true;
    elements.runEvaluationLabel.textContent = "Running…";
    setEvaluationState("loading");
    setApiNotice("");
    const selected = elements.evaluationScenarioSelect.value;
    try {
      const report = await postJson("/api/evaluation/run", selected ? { scenario: selected } : {});
      renderEvaluationReport(report);
    } catch (error) {
      showEvaluationError(error?.message || "The evaluation could not be completed.");
      setApiNotice(error?.message || "The evaluation could not be completed.");
    } finally {
      state.evaluationLoading = false;
      elements.evaluationScenarioSelect.disabled = state.evaluationCatalog.length === 0;
      elements.runEvaluation.disabled = state.evaluationCatalog.length === 0;
      elements.runEvaluationLabel.textContent = "Run evaluation";
    }
  }

  async function loadSystemData() {
    const results = await Promise.allSettled([
      requestJson("/api/health"),
      requestJson("/api/capabilities"),
      requestJson("/api/provider/status"),
      requestJson("/api/evaluation/scenarios"),
      requestJson("/api/system/capabilities"),
      requestJson("/api/workflows"),
      requestJson("/api/memory"),
      requestJson("/api/tasks/overview"),
    ]);
    if (results[0].status === "fulfilled") {
      renderHealth(results[0].value);
    } else {
      renderHealthFailure(results[0].reason);
    }
    if (results[1].status === "fulfilled") {
      renderCapabilities(results[1].value);
    } else {
      renderCapabilitiesFailure(results[1].reason);
    }
    if (results[2].status === "fulfilled") {
      const providerStatus = results[2].value || {};
      renderProviderLabels(providerStatus);
      elements.capabilityProviderModel.textContent = displayValue(providerStatus.model, "unavailable");
      elements.capabilityProviderVerification.textContent = providerStatus.verified === true
        ? "Verified by backend"
        : displayValue(providerStatus.message, "Not verified");
    }
    if (results[3].status === "fulfilled") {
      renderEvaluationCatalog(results[3].value);
    } else {
      renderEvaluationCatalogFailure(results[3].reason);
    }
    if (results[4].status === "fulfilled") {
      renderSystemCapabilities(results[4].value);
    } else {
      renderSystemCapabilitiesFailure(results[4].reason);
    }
    if (results[5].status === "fulfilled") {
      renderWorkflows(results[5].value);
    } else {
      renderWorkflowFailure(results[5].reason);
    }
    if (results[6].status === "fulfilled") {
      renderMemory(results[6].value);
    } else {
      renderMemoryFailure(results[6].reason);
    }
    if (results[7].status === "fulfilled") {
      renderTaskOverview(results[7].value);
    } else {
      renderTaskOverviewFailure(results[7].reason);
    }
  }

  function setWorkflowState(stateName) {
    elements.workflowLoadingState.hidden = stateName !== "loading";
    elements.workflowEmptyState.hidden = stateName !== "empty";
    elements.workflowErrorState.hidden = stateName !== "error";
  }

  function renderWorkflowFailure(error) {
    if (isAbortError(error)) {
      return;
    }
    state.workflows = [];
    elements.workflowList.replaceChildren();
    elements.workflowErrorMessage.textContent = error?.message || "Workflows are unavailable.";
    setWorkflowState("error");
    elements.workflowSyncLabel.textContent = "Workflow API unavailable";
  }

  function renderWorkflows(workflowDocument) {
    const workflows = Array.isArray(workflowDocument?.workflows) ? workflowDocument.workflows : [];
    state.workflows = workflows;
    elements.workflowList.replaceChildren();
    elements.workflowSyncLabel.textContent = `${workflows.length} ${workflows.length === 1 ? "workflow" : "workflows"} loaded`;
    if (!workflows.length) {
      setWorkflowState("empty");
      return;
    }
    setWorkflowState("ready");
    workflows.forEach((workflow) => {
      const card = document.createElement("article");
      card.className = "workflow-card";
      const header = document.createElement("div");
      header.className = "workflow-card-header";
      const title = document.createElement("div");
      addText(title, "h4", "", workflow?.name);
      addText(title, "p", "workflow-description", workflow?.description);
      addText(header, "span", "workflow-version", `v${displayValue(workflow?.version, "1")}`);
      header.prepend(title);
      card.append(header);

      const facts = document.createElement("dl");
      facts.className = "workflow-facts";
      addDefinition(facts, "Updated", workflow?.updated_at || workflow?.created_at);
      addDefinition(facts, "Goal", formatData(workflow?.goal));
      card.append(facts);

      const actions = document.createElement("div");
      actions.className = "workflow-actions";
      const run = document.createElement("button");
      run.type = "button";
      run.className = "secondary-button";
      run.textContent = state.workflowBusy === workflow?.id ? "Running…" : "Run workflow";
      run.disabled = Boolean(state.workflowBusy);
      run.addEventListener("click", () => runWorkflow(workflow));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary-button danger-button";
      remove.textContent = state.workflowBusy === workflow?.id ? "Updating…" : "Delete";
      remove.disabled = Boolean(state.workflowBusy);
      remove.addEventListener("click", () => deleteWorkflow(workflow));
      actions.append(run, remove);
      card.append(actions);
      elements.workflowList.append(card);
    });
  }

  async function loadWorkflows() {
    const controller = replaceController("workflows");
    elements.workflowSyncLabel.textContent = "Loading workflows";
    setWorkflowState("loading");
    try {
      const workflowDocument = await requestJson("/api/workflows", { signal: controller.signal });
      if (state.controllers.workflows !== controller) {
        return;
      }
      renderWorkflows(workflowDocument);
    } catch (error) {
      if (!isAbortError(error)) {
        renderWorkflowFailure(error);
      }
    }
  }

  function renderSystemCapabilities(capabilityDocument) {
    const capabilities = capabilityDocument?.capabilities && typeof capabilityDocument.capabilities === "object"
      ? capabilityDocument.capabilities
      : {};
    const availableCount = Object.values(capabilities).filter((metric) => metric?.available === true).length;
    elements.systemCapabilitySummary.textContent = capabilityDocument?.available === true
      ? `${availableCount} measurement capabilities available`
      : "The optional measurement backend is unavailable; disk remains independently reported when measurable.";
    elements.systemCapabilityList.replaceChildren();
    ["cpu", "memory", "disk", "network", "processes"].forEach((name) => {
      const metric = capabilities[name] || {};
      const value = metric.available === true
        ? `Available · ${displayValue(metric.source, "source reported")}`
        : `Unavailable · ${displayValue(metric.reason, "not reported")}`;
      addDefinition(elements.systemCapabilityList, name, value);
    });
  }

  function renderSystemCapabilitiesFailure(error) {
    if (isAbortError(error)) {
      return;
    }
    elements.systemCapabilitySummary.textContent = "Measurement capabilities unavailable.";
    elements.systemCapabilityList.replaceChildren();
    addText(elements.systemCapabilityList, "dd", "metric-unavailable", error?.message || "The capability endpoint could not be reached.");
  }

  function metricIsMeasured(metric, requiredFields = []) {
    return Boolean(
      metric
      && metric.available === true
      && metric.status === "measured"
      && requiredFields.every((field) => metric[field] !== null && metric[field] !== undefined),
    );
  }

  function formatBytes(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
      return null;
    }
    if (value < 1024) {
      return `${value} B`;
    }
    const units = ["KiB", "MiB", "GiB", "TiB"];
    let scaled = value;
    let unitIndex = -1;
    while (scaled >= 1024 && unitIndex < units.length - 1) {
      scaled /= 1024;
      unitIndex += 1;
    }
    return `${scaled.toFixed(scaled >= 10 ? 1 : 2)} ${units[unitIndex]}`;
  }

  function startMetricCard(container, title, metric, requiredFields = []) {
    container.replaceChildren();
    addText(container, "p", "eyebrow", title);
    addText(container, "h3", "", metricIsMeasured(metric, requiredFields) ? "Measured" : "Unavailable");
    if (!metricIsMeasured(metric, requiredFields)) {
      addText(container, "p", "metric-unavailable", displayValue(metric?.reason, "The API did not report this metric."));
      return null;
    }
    return container;
  }

  function addMetricFact(parent, label, value) {
    if (value === null || value === undefined) {
      return;
    }
    addDefinition(parent, label, value);
  }

  function renderSystemMetricCards(snapshot) {
    const cpu = startMetricCard(elements.systemCpu, "CPU", snapshot?.cpu, ["percent"]);
    if (cpu) {
      const facts = document.createElement("dl");
      facts.className = "metric-facts";
      addMetricFact(facts, "Utilization", typeof snapshot.cpu.percent === "number" ? `${snapshot.cpu.percent.toFixed(1)}%` : null);
      addMetricFact(facts, "Source", snapshot.cpu.source);
      cpu.append(facts);
    }

    const memory = startMetricCard(elements.systemMemory, "Memory", snapshot?.memory, ["total_bytes", "used_bytes", "percent"]);
    if (memory) {
      const facts = document.createElement("dl");
      facts.className = "metric-facts";
      addMetricFact(facts, "Used", formatBytes(snapshot.memory.used_bytes));
      addMetricFact(facts, "Total", formatBytes(snapshot.memory.total_bytes));
      addMetricFact(facts, "Utilization", typeof snapshot.memory.percent === "number" ? `${snapshot.memory.percent.toFixed(1)}%` : null);
      memory.append(facts);
    }

    const disk = startMetricCard(elements.systemDisk, "Disk", snapshot?.disk, ["total_bytes", "used_bytes", "free_bytes", "percent"]);
    if (disk) {
      const facts = document.createElement("dl");
      facts.className = "metric-facts";
      addMetricFact(facts, "Target", snapshot.disk.target);
      addMetricFact(facts, "Used", formatBytes(snapshot.disk.used_bytes));
      addMetricFact(facts, "Free", formatBytes(snapshot.disk.free_bytes));
      addMetricFact(facts, "Utilization", typeof snapshot.disk.percent === "number" ? `${snapshot.disk.percent.toFixed(1)}%` : null);
      disk.append(facts);
    }

    const network = startMetricCard(elements.systemNetwork, "Network", snapshot?.network, ["bytes_sent", "bytes_received"]);
    if (network) {
      const facts = document.createElement("dl");
      facts.className = "metric-facts";
      addMetricFact(facts, "Sent", formatBytes(snapshot.network.bytes_sent));
      addMetricFact(facts, "Received", formatBytes(snapshot.network.bytes_received));
      network.append(facts);
    }

    const processes = startMetricCard(elements.systemProcesses, "Process summaries", snapshot?.processes);
    if (processes) {
      const items = Array.isArray(snapshot.processes.items) ? snapshot.processes.items : [];
      if (!items.length) {
        addText(processes, "p", "metric-unavailable", "Measured, but no process summaries were returned.");
      } else {
        const list = document.createElement("ul");
        list.className = "system-process-list";
        items.forEach((item) => {
          const row = document.createElement("li");
          addText(row, "strong", "", item?.name);
          addText(row, "span", "", `CPU ${typeof item?.cpu_percent === "number" ? item.cpu_percent.toFixed(1) : "—"}% · memory ${typeof item?.memory_percent === "number" ? item.memory_percent.toFixed(1) : "—"}%`);
          list.append(row);
        });
        processes.append(list);
      }
      if (typeof snapshot.processes.sampled === "number" && typeof snapshot.processes.limit === "number") {
        addText(processes, "p", "metric-note", `Sampled ${snapshot.processes.sampled}; output bounded to ${snapshot.processes.limit}.`);
      }
    }
  }

  function renderSystemRecommendations(recommendations) {
    elements.systemRecommendations.replaceChildren();
    if (!Array.isArray(recommendations) || !recommendations.length) {
      addText(elements.systemRecommendations, "li", "recommendation-empty", "No threshold recommendations were returned by the API.");
      return;
    }
    recommendations.forEach((recommendation) => {
      const item = document.createElement("li");
      addText(item, "strong", "", recommendation?.message);
      const threshold = recommendation?.threshold;
      const thresholdText = threshold && typeof threshold === "object"
        ? `${displayValue(recommendation?.metric, "metric")} ${displayValue(threshold.operator, "at")} ${displayValue(threshold.value, "threshold")} · observed ${displayValue(recommendation?.observed)}`
        : displayValue(recommendation?.metric, "Measured threshold");
      addText(item, "span", "recommendation-detail", thresholdText);
      elements.systemRecommendations.append(item);
    });
  }

  function renderSystemSnapshot(snapshot) {
    elements.systemSnapshotEmpty.hidden = true;
    elements.systemSnapshotError.hidden = true;
    elements.systemSnapshotResults.hidden = false;
    elements.systemMeasuredAt.textContent = displayValue(snapshot?.measured_at, "unavailable");
    elements.systemSnapshotStatus.textContent = snapshot?.available === true
      ? "Snapshot includes measured psutil-backed data"
      : "No psutil-backed metric was measured; unavailable values remain explicit";
    renderSystemMetricCards(snapshot || {});
    renderSystemRecommendations(snapshot?.recommendations);
  }

  async function loadSystemSnapshot() {
    if (state.systemSnapshotLoading) {
      return;
    }
    state.systemSnapshotLoading = true;
    const controller = replaceController("systemSnapshot");
    elements.measureSystem.disabled = true;
    elements.measureSystemLabel.textContent = "Measuring…";
    elements.systemSnapshotStatus.textContent = "Requesting one measured snapshot";
    setApiNotice("");
    try {
      const snapshot = await requestJson("/api/system/snapshot", { signal: controller.signal });
      renderSystemSnapshot(snapshot);
    } catch (error) {
      if (!isAbortError(error)) {
        elements.systemSnapshotResults.hidden = true;
        elements.systemSnapshotEmpty.hidden = true;
        elements.systemSnapshotError.hidden = false;
        elements.systemSnapshotErrorMessage.textContent = error?.message || "The system snapshot could not be loaded.";
        setApiNotice(error?.message || "The system snapshot could not be loaded.");
      }
    } finally {
      state.systemSnapshotLoading = false;
      elements.measureSystem.disabled = false;
      elements.measureSystemLabel.textContent = "Measure snapshot";
    }
  }

  function setMemoryState(stateName) {
    elements.memoryLoadingState.hidden = stateName !== "loading";
    elements.memoryEmptyState.hidden = stateName !== "empty";
    elements.memoryErrorState.hidden = stateName !== "error";
  }

  function renderMemoryFailure(error) {
    if (isAbortError(error)) {
      return;
    }
    state.memory = [];
    elements.memoryList.replaceChildren();
    elements.memoryErrorMessage.textContent = error?.message || "Memory is unavailable.";
    elements.memorySyncLabel.textContent = "Memory API unavailable";
    setMemoryState("error");
  }

  function renderMemory(memoryDocument) {
    const records = Array.isArray(memoryDocument?.memory) ? memoryDocument.memory : [];
    state.memory = records;
    elements.memoryList.replaceChildren();
    elements.memorySyncLabel.textContent = `${records.length} ${records.length === 1 ? "record" : "records"} loaded`;
    if (!records.length) {
      setMemoryState("empty");
      return;
    }
    setMemoryState("ready");
    records.forEach((record) => {
      const card = document.createElement("article");
      card.className = "memory-card";
      const header = document.createElement("div");
      header.className = "memory-card-header";
      addText(header, "h4", "", record?.key);
      addText(header, "span", "memory-category", record?.category);
      card.append(header);
      const value = document.createElement("pre");
      value.className = "memory-value";
      value.textContent = formatData(record?.value);
      card.append(value);
      const footer = document.createElement("div");
      footer.className = "memory-card-footer";
      addText(footer, "time", "memory-updated", record?.updated_at || record?.created_at);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary-button danger-button compact-button";
      remove.textContent = state.memoryBusy === record?.id ? "Deleting…" : "Delete";
      remove.disabled = Boolean(state.memoryBusy);
      remove.addEventListener("click", () => deleteMemory(record));
      footer.append(remove);
      card.append(footer);
      elements.memoryList.append(card);
    });
  }

  async function loadMemory() {
    const controller = replaceController("memory");
    elements.memorySyncLabel.textContent = "Loading memory";
    setMemoryState("loading");
    try {
      const memoryDocument = await requestJson("/api/memory", { signal: controller.signal });
      if (state.controllers.memory !== controller) {
        return;
      }
      renderMemory(memoryDocument);
    } catch (error) {
      if (!isAbortError(error)) {
        renderMemoryFailure(error);
      }
    }
  }

  function buildGoal() {
    const mode = elements.scenarioMode.value;
    const details = MODE_DETAILS[mode] || MODE_DETAILS.success;
    const detail = elements.scenarioContext.value.trim() || details.fallback;
    if (mode === "failure_recovery") {
      return { type: mode, key: detail };
    }
    if (mode === "timeout") {
      return { type: mode, message: detail, seconds: 0.2, timeout_seconds: 0.05 };
    }
    if (mode === "workspace_report") {
      return { type: mode, message: detail, overwrite: false };
    }
    return { type: mode, message: detail };
  }

  function newWorkflowStep(index, dependency = "") {
    return {
      id: `workflow-step-${index + 1}`,
      name: `Step ${index + 1}`,
      tool: "fixture.success",
      value: "verified workflow step",
      dependency,
    };
  }

  function syncWorkflowStepsFromDom() {
    if (state.workflowSource !== "ordered_plan" || !elements.workflowStepList) {
      return;
    }
    state.workflowSteps = Array.from(elements.workflowStepList.querySelectorAll("[data-workflow-step-id]")).map((card, index) => ({
      id: card.dataset.workflowStepId || `workflow-step-${index + 1}`,
      name: card.querySelector(".workflow-step-name")?.value || `Step ${index + 1}`,
      tool: card.querySelector(".workflow-step-tool")?.value || "fixture.success",
      value: card.querySelector(".workflow-step-value")?.value || "",
      dependency: card.querySelector(".workflow-step-dependency")?.value || "",
    }));
  }

  function workflowValueConfig(step) {
    return WORKFLOW_TOOL_CATALOG[step.tool] || WORKFLOW_TOOL_CATALOG["fixture.success"];
  }

  function renderWorkflowStepBuilder() {
    const visible = state.workflowSource === "ordered_plan";
    elements.workflowStepBuilder.hidden = !visible;
    if (!visible) {
      return;
    }
    elements.workflowStepList.replaceChildren();
    state.workflowSteps.forEach((step, index) => {
      const config = workflowValueConfig(step);
      const card = document.createElement("article");
      card.className = "workflow-step-card";
      card.dataset.workflowStepId = step.id;

      const header = document.createElement("div");
      header.className = "workflow-step-card-header";
      const title = document.createElement("div");
      addText(title, "span", "step-number", `STEP ${String(index + 1).padStart(2, "0")}`);
      addText(title, "strong", "", config.label);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary-button danger-button compact-button";
      remove.textContent = "Remove";
      remove.disabled = state.workflowSteps.length <= 1;
      remove.addEventListener("click", () => removeWorkflowStep(index));
      header.append(title, remove);
      card.append(header);

      const fields = document.createElement("div");
      fields.className = "workflow-step-fields";

      const nameLabel = document.createElement("label");
      nameLabel.textContent = "Step name";
      const name = document.createElement("input");
      name.className = "workflow-step-name";
      name.type = "text";
      name.maxLength = 100;
      name.value = step.name;
      nameLabel.append(name);
      fields.append(nameLabel);

      const toolLabel = document.createElement("label");
      toolLabel.textContent = "Allowlisted tool";
      const tool = document.createElement("select");
      tool.className = "workflow-step-tool";
      WORKFLOW_TOOL_NAMES.forEach((toolName) => {
        const option = document.createElement("option");
        option.value = toolName;
        option.textContent = `${WORKFLOW_TOOL_CATALOG[toolName].label} · ${toolName}`;
        option.selected = toolName === step.tool;
        tool.append(option);
      });
      toolLabel.append(tool);
      fields.append(toolLabel);

      const valueLabel = document.createElement("label");
      valueLabel.textContent = config.inputKind === "seconds" ? "Delay seconds" : config.inputKind === "key" ? "Recovery key" : "Action message";
      const value = document.createElement("input");
      value.className = "workflow-step-value";
      value.type = config.inputKind === "seconds" ? "number" : "text";
      value.maxLength = 500;
      if (config.inputKind === "seconds") {
        value.min = "0";
        value.max = "5";
        value.step = "0.01";
      }
      value.value = step.value || config.fallback;
      valueLabel.append(value);
      fields.append(valueLabel);

      const dependencyLabel = document.createElement("label");
      dependencyLabel.textContent = "Depends on";
      const dependency = document.createElement("select");
      dependency.className = "workflow-step-dependency";
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "No dependency";
      dependency.append(none);
      state.workflowSteps.slice(0, index).forEach((previous, previousIndex) => {
        const option = document.createElement("option");
        option.value = previous.id;
        option.textContent = `Step ${String(previousIndex + 1).padStart(2, "0")} · ${previous.id}`;
        option.selected = previous.id === step.dependency;
        dependency.append(option);
      });
      if (!state.workflowSteps.slice(0, index).some((previous) => previous.id === step.dependency)) {
        dependency.value = "";
      }
      dependencyLabel.append(dependency);
      fields.append(dependencyLabel);
      card.append(fields);

      [name, value, dependency].forEach((control) => {
        control.addEventListener("input", () => {
          syncWorkflowStepsFromDom();
          renderWorkflowGoalPreview();
        });
        control.addEventListener("change", () => {
          syncWorkflowStepsFromDom();
          if (control === tool) {
            renderWorkflowStepBuilder();
          }
          renderWorkflowGoalPreview();
        });
      });
      elements.workflowStepList.append(card);
    });
    elements.addWorkflowStep.disabled = state.workflowSteps.length >= 4;
    elements.workflowBuilderStatus.textContent = `${state.workflowSteps.length} ordered ${state.workflowSteps.length === 1 ? "step" : "steps"}; dependencies are optional and API-validated.`;
  }

  function buildStructuredWorkflowGoal() {
    syncWorkflowStepsFromDom();
    return {
      type: "structured_plan",
      steps: state.workflowSteps.map((step, index) => {
        const config = workflowValueConfig(step);
        const rawValue = String(step.value || config.fallback).trim() || config.fallback;
        const input = config.inputKind === "key"
          ? { key: rawValue }
          : config.inputKind === "seconds"
            ? { seconds: Math.min(5, Math.max(0, Number.parseFloat(rawValue) || 0.01)) }
            : { message: rawValue };
        return {
          step_id: step.id || `workflow-step-${index + 1}`,
          name: String(step.name || `Step ${index + 1}`).trim() || `Step ${index + 1}`,
          tool_name: step.tool,
          input,
          dependencies: step.dependency ? [step.dependency] : [],
          risk: config.risk,
          approval_required: config.approval,
          max_attempts: config.maxAttempts,
          timeout_seconds: config.timeoutSeconds,
        };
      }),
    };
  }

  function resetWorkflowBuilder() {
    state.workflowSource = "scenario";
    state.workflowSteps = [newWorkflowStep(0)];
    elements.workflowSource.value = "scenario";
    renderWorkflowStepBuilder();
  }

  function addWorkflowStep() {
    syncWorkflowStepsFromDom();
    if (state.workflowSteps.length >= 4) {
      return;
    }
    const dependency = state.workflowSteps[state.workflowSteps.length - 1]?.id || "";
    state.workflowSteps.push(newWorkflowStep(state.workflowSteps.length, dependency));
    renderWorkflowStepBuilder();
    renderWorkflowGoalPreview();
  }

  function removeWorkflowStep(index) {
    syncWorkflowStepsFromDom();
    if (state.workflowSteps.length <= 1) {
      return;
    }
    const oldSteps = state.workflowSteps.slice();
    const removed = oldSteps[index];
    const remaining = oldSteps.filter((_step, stepIndex) => stepIndex !== index);
    const idMap = new Map();
    remaining.forEach((step, stepIndex) => idMap.set(step.id, `workflow-step-${stepIndex + 1}`));
    state.workflowSteps = remaining.map((step, stepIndex) => ({
      ...step,
      id: `workflow-step-${stepIndex + 1}`,
      dependency: step.dependency === removed.id ? "" : (idMap.get(step.dependency) || ""),
    }));
    renderWorkflowStepBuilder();
    renderWorkflowGoalPreview();
  }

  function renderWorkflowGoalPreview() {
    const goal = state.workflowSource === "ordered_plan" ? buildStructuredWorkflowGoal() : buildGoal();
    elements.workflowGoalPreview.textContent = formatData(goal);
  }

  function setWorkflowStatus(message) {
    elements.workflowSyncLabel.textContent = message || "";
  }

  async function saveWorkflow(event) {
    event.preventDefault();
    if (state.workflowSaving) {
      return;
    }
    if (elements.scenarioMode.value === "ai_goal") {
      setWorkflowFormStatus("Provider-directed goals are run-only. Choose a deterministic scenario before saving a reusable workflow.");
      return;
    }
    const name = elements.workflowName.value.trim();
    const description = elements.workflowDescription.value.trim();
    if (!name || !description) {
      setWorkflowFormStatus("Workflow name and description are required.");
      return;
    }
    state.workflowSaving = true;
    elements.saveWorkflow.disabled = true;
    elements.saveWorkflowLabel.textContent = "Saving…";
    setWorkflowFormStatus("Validating and saving the workflow; it will not execute automatically.");
    setApiNotice("");
    try {
      await postJson("/api/workflows", {
        name,
        description,
        goal: state.workflowSource === "ordered_plan" ? buildStructuredWorkflowGoal() : buildGoal(),
      });
      elements.workflowForm.reset();
      resetWorkflowBuilder();
      setWorkflowFormStatus("Workflow saved. Run it explicitly from the persisted library.");
      await loadWorkflows();
    } catch (error) {
      setWorkflowFormStatus(error?.message || "The workflow could not be saved.");
      setApiNotice(error?.message || "The workflow could not be saved.");
    } finally {
      state.workflowSaving = false;
      elements.saveWorkflow.disabled = false;
      elements.saveWorkflowLabel.textContent = "Save workflow";
      renderWorkflowGoalPreview();
    }
  }

  function setActiveNavigation(sectionId) {
    document.querySelectorAll("[data-section-target]").forEach((item) => {
      const active = item.dataset.sectionTarget === sectionId;
      item.classList.toggle("is-active", active);
      if (active) {
        item.setAttribute("aria-current", "page");
      } else {
        item.removeAttribute("aria-current");
      }
    });
  }

  function sectionFromHash() {
    const candidate = window.location.hash.replace(/^#/, "");
    return SECTION_IDS.includes(candidate) ? candidate : null;
  }

  function updateSectionLocation(sectionId, replace = false) {
    if (!window.history?.replaceState) {
      return;
    }
    const url = new URL(window.location.href);
    url.hash = sectionId;
    const method = replace ? "replaceState" : "pushState";
    window.history[method]({}, "", url);
  }

  function scrollToSection(sectionId, { updateLocation = true, replaceLocation = false, behavior } = {}) {
    const section = byId(sectionId);
    if (!section) {
      return;
    }
    section.scrollIntoView({
      behavior: behavior || (isReducedMotion() ? "auto" : "smooth"),
      block: "start",
    });
    setActiveNavigation(sectionId);
    if (updateLocation) {
      updateSectionLocation(sectionId, replaceLocation);
    }
  }

  async function runWorkflow(workflow) {
    if (!workflow?.id || state.workflowBusy) {
      return;
    }
    if (state.task && !isTerminal(state.task.status)) {
      setWorkflowFormStatus("Finish or cancel the monitored task before starting another run.");
      return;
    }
    state.workflowBusy = workflow.id;
    renderWorkflows({ workflows: state.workflows });
    setWorkflowStatus(`Running ${displayValue(workflow.name, "workflow")}…`);
    setApiNotice("");
    try {
      const task = await postJson(`/api/workflows/${encodeURIComponent(workflow.id)}/run`, {});
      state.events = [];
      renderTask(task);
      renderTimeline([]);
      setWorkflowFormStatus("Workflow run created. The Live Console is monitoring the persisted task.");
      scrollToSection("live-console");
      void loadRecentRuns();
      void pollTask(task.task_id);
    } catch (error) {
      setWorkflowFormStatus(error?.message || "The workflow could not be run.");
      setApiNotice(error?.message || "The workflow could not be run.");
    } finally {
      state.workflowBusy = null;
      renderWorkflows({ workflows: state.workflows });
    }
  }

  async function deleteWorkflow(workflow) {
    if (!workflow?.id || state.workflowBusy) {
      return;
    }
    const name = displayValue(workflow.name, "this workflow");
    if (!window.confirm(`Delete ${name}? This removes the persisted workflow but does not affect existing tasks.`)) {
      return;
    }
    state.workflowBusy = workflow.id;
    renderWorkflows({ workflows: state.workflows });
    setWorkflowStatus(`Deleting ${name}…`);
    try {
      await deleteJson(`/api/workflows/${encodeURIComponent(workflow.id)}`);
      setWorkflowFormStatus("Workflow deleted after confirmation.");
      await loadWorkflows();
    } catch (error) {
      setWorkflowFormStatus(error?.message || "The workflow could not be deleted.");
      setApiNotice(error?.message || "The workflow could not be deleted.");
    } finally {
      state.workflowBusy = null;
      if (state.workflows.length) {
        renderWorkflows({ workflows: state.workflows });
      }
    }
  }

  function setWorkflowFormStatus(message) {
    elements.workflowFormStatus.textContent = message || "";
    if (message) {
      state.lastStatusMessage = message;
      elements.speakStatus.disabled = !state.voice.speechSupported;
    }
  }

  function setMemoryStatus(message) {
    elements.memoryFormStatus.textContent = message || "";
    if (message) {
      state.lastStatusMessage = message;
      elements.speakStatus.disabled = !state.voice.speechSupported;
    }
  }

  async function saveMemory(event) {
    event.preventDefault();
    if (state.memorySaving) {
      return;
    }
    const key = elements.memoryKey.value.trim();
    const category = elements.memoryCategory.value.trim();
    const value = elements.memoryValue.value.trim();
    if (!key || !category || !value) {
      setMemoryStatus("Memory key, category, and bounded text value are required.");
      return;
    }
    state.memorySaving = true;
    elements.saveMemory.disabled = true;
    elements.saveMemoryLabel.textContent = "Saving…";
    setMemoryStatus("Validating and saving memory through the API…");
    setApiNotice("");
    try {
      await postJson("/api/memory", { key, value, category });
      elements.memoryValue.value = "";
      setMemoryStatus("Memory saved. The persisted record list was refreshed.");
      await loadMemory();
    } catch (error) {
      if (error?.code === "memory_secret_rejected") {
        elements.memoryKey.value = "";
        elements.memoryValue.value = "";
        setMemoryStatus("Memory rejected: credential-like data is not stored, and the submitted value was cleared.");
      } else {
        setMemoryStatus(error?.message || "The memory record could not be saved.");
      }
      setApiNotice(error?.code === "memory_secret_rejected"
        ? "The memory API rejected credential-like data."
        : (error?.message || "The memory record could not be saved."));
    } finally {
      state.memorySaving = false;
      elements.saveMemory.disabled = false;
      elements.saveMemoryLabel.textContent = "Save memory";
    }
  }

  async function deleteMemory(record) {
    if (!record?.id || state.memoryBusy) {
      return;
    }
    const key = displayValue(record.key, "this record");
    if (!window.confirm(`Delete memory record ${key}?`)) {
      return;
    }
    state.memoryBusy = record.id;
    renderMemory({ memory: state.memory });
    setMemoryStatus(`Deleting ${key}…`);
    try {
      await deleteJson(`/api/memory/${encodeURIComponent(record.id)}`);
      setMemoryStatus("Memory record deleted after confirmation.");
      await loadMemory();
    } catch (error) {
      setMemoryStatus(error?.message || "The memory record could not be deleted.");
      setApiNotice(error?.message || "The memory record could not be deleted.");
    } finally {
      state.memoryBusy = null;
      renderMemory({ memory: state.memory });
    }
  }

  async function resetMemory() {
    if (!window.confirm("Reset all persisted memory records? This cannot be undone.")) {
      return;
    }
    elements.resetMemory.disabled = true;
    setMemoryStatus("Resetting memory after confirmation…");
    try {
      const result = await postJson("/api/memory/reset", {});
      setMemoryStatus(`Memory reset completed. ${displayValue(result?.deleted, "0")} records deleted.`);
      await loadMemory();
    } catch (error) {
      setMemoryStatus(error?.message || "Memory reset could not be completed.");
      setApiNotice(error?.message || "Memory reset could not be completed.");
    } finally {
      elements.resetMemory.disabled = false;
    }
  }

  function setVoiceStatus(message) {
    elements.voiceStatus.textContent = message;
    if (message) {
      state.lastStatusMessage = message;
      elements.speakStatus.disabled = !state.voice.speechSupported;
    }
  }

  function configureVoice() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (typeof Recognition === "function") {
      state.voice.supported = true;
      const recognition = new Recognition();
      recognition.continuous = false;
      recognition.interimResults = true;
      recognition.lang = document.documentElement.lang || "en-US";
      recognition.onstart = () => {
        state.voice.listening = true;
        elements.voiceInputLabel.textContent = "Stop listening";
        setVoiceStatus("Listening… release when the transcript is complete.");
      };
      recognition.onresult = (event) => {
        const transcript = Array.from(event.results || [])
          .map((result) => result[0]?.transcript || "")
          .join(" ")
          .trim();
        if (transcript) {
          elements.scenarioContext.value = transcript.slice(0, 500);
          renderWorkflowGoalPreview();
          setVoiceStatus("Processing transcript…");
        }
      };
      recognition.onerror = (event) => {
        state.voice.listening = false;
        elements.voiceInputLabel.textContent = "Push to talk";
        const message = event?.error === "not-allowed"
          ? "Voice unavailable: microphone permission was not granted."
          : "Voice unavailable: the browser could not complete speech recognition.";
        setVoiceStatus(message);
      };
      recognition.onend = () => {
        state.voice.listening = false;
        elements.voiceInputLabel.textContent = "Push to talk";
        if (elements.voiceStatus.textContent === "Processing transcript…") {
          setVoiceStatus("Transcript ready.");
        } else if (!elements.voiceStatus.textContent.startsWith("Voice unavailable")) {
          setVoiceStatus("Voice input ended.");
        }
      };
      state.voice.recognition = recognition;
      elements.voiceInput.disabled = false;
      elements.voiceInputLabel.textContent = "Push to talk";
      setVoiceStatus("Ready when you press the button.");
    } else {
      setVoiceStatus("Voice unavailable: this browser does not support speech recognition.");
    }

    state.voice.speechSupported = "speechSynthesis" in window
      && typeof window.SpeechSynthesisUtterance === "function";
    elements.speakStatus.disabled = !state.voice.speechSupported;
    elements.speakStatusLabel.textContent = state.voice.speechSupported ? "Read last status" : "Read status unavailable";
  }

  function toggleVoiceInput() {
    if (!state.voice.recognition) {
      return;
    }
    if (state.voice.listening) {
      state.voice.recognition.stop();
      setVoiceStatus("Processing transcript…");
      return;
    }
    try {
      state.voice.recognition.start();
      setVoiceStatus("Starting microphone…");
    } catch (_error) {
      setVoiceStatus("Voice unavailable: the browser could not start speech recognition.");
    }
  }

  function readLastStatus() {
    if (!state.voice.speechSupported) {
      setVoiceStatus("Read status unavailable: this browser does not support speech synthesis.");
      return;
    }
    window.speechSynthesis.cancel();
    const utterance = new window.SpeechSynthesisUtterance(state.lastStatusMessage || "No status to read yet.");
    utterance.onstart = () => setVoiceStatus("Speaking last status…");
    utterance.onend = () => setVoiceStatus("Last status spoken.");
    utterance.onerror = () => setVoiceStatus("Read status unavailable: speech synthesis could not complete.");
    window.speechSynthesis.speak(utterance);
  }

  function updateModeHelp() {
    const mode = elements.scenarioMode.value;
    const details = MODE_DETAILS[mode] || MODE_DETAILS.success;
    elements.scenarioHelp.textContent = details.help;
  }

  function setFormStatus(message) {
    elements.formStatus.textContent = message || "";
    if (message) {
      state.lastStatusMessage = message;
      elements.speakStatus.disabled = !state.voice.speechSupported;
    }
  }

  function normalizeTaskId(value) {
    if (value === null || value === undefined) {
      return null;
    }
    let candidate = String(value).trim();
    try {
      candidate = decodeURIComponent(candidate);
    } catch (_error) {
      return null;
    }
    return candidate && candidate.length <= 256 ? candidate : null;
  }

  function readStoredTaskId() {
    try {
      return normalizeTaskId(window.localStorage.getItem(CURRENT_TASK_STORAGE_KEY));
    } catch (_error) {
      return null;
    }
  }

  function storedTaskId(taskId) {
    try {
      if (taskId) {
        window.localStorage.setItem(CURRENT_TASK_STORAGE_KEY, taskId);
      } else {
        window.localStorage.removeItem(CURRENT_TASK_STORAGE_KEY);
      }
    } catch (_error) {
      // Browser storage can be disabled; the API-backed console still works.
    }
  }

  function taskIdFromHash() {
    const hash = window.location.hash.replace(/^#/, "");
    if (!hash) {
      return null;
    }
    const queryStart = hash.indexOf("?");
    if (queryStart >= 0) {
      const hashParams = new URLSearchParams(hash.slice(queryStart + 1));
      for (const name of ["task", "task_id", "taskId"]) {
        const value = normalizeTaskId(hashParams.get(name));
        if (value) {
          return value;
        }
      }
    }
    const match = hash.match(/^task(?:[_-]?id)?(?:=|\/|:)(.+)$/i);
    return normalizeTaskId(match ? match[1] : null);
  }

  function taskIdFromLocation() {
    const params = new URLSearchParams(window.location.search);
    for (const name of ["task", "task_id", "taskId"]) {
      const value = normalizeTaskId(params.get(name));
      if (value) {
        return value;
      }
    }
    return taskIdFromHash();
  }

  function updateTaskLocation(taskId) {
    if (!window.history?.replaceState) {
      return;
    }
    const url = new URL(window.location.href);
    url.searchParams.delete("task_id");
    url.searchParams.delete("taskId");
    url.searchParams.set("task", taskId);
    if (taskIdFromHash()) {
      url.hash = "live-console";
    }
    window.history.replaceState({}, "", url);
  }

  function clearTaskLocation(taskId) {
    if (!window.history?.replaceState) {
      return;
    }
    const url = new URL(window.location.href);
    const current = taskIdFromLocation();
    if (taskId && current && current !== taskId) {
      return;
    }
    url.searchParams.delete("task");
    url.searchParams.delete("task_id");
    url.searchParams.delete("taskId");
    if (taskIdFromHash()) {
      url.hash = "live-console";
    }
    window.history.replaceState({}, "", url);
  }

  async function restoreTask(taskId, { scroll = false, source = "saved state" } = {}) {
    const normalizedTaskId = normalizeTaskId(taskId);
    if (!normalizedTaskId) {
      return null;
    }
    stopPolling();
    const controller = replaceController("taskRestore");
    elements.syncLabel.textContent = "Restoring persisted task";
    try {
      const [task, eventDocument] = await Promise.all([
        requestJson(`/api/tasks/${encodeURIComponent(normalizedTaskId)}`, { signal: controller.signal }),
        requestJson(`/api/tasks/${encodeURIComponent(normalizedTaskId)}/events`, { signal: controller.signal }),
      ]);
      if (state.controllers.taskRestore !== controller) {
        return null;
      }
      state.events = Array.isArray(eventDocument?.events) ? eventDocument.events : [];
      storedTaskId(task.task_id || normalizedTaskId);
      updateTaskLocation(task.task_id || normalizedTaskId);
      renderTask(task);
      renderTimeline(state.events);
      setFormStatus(`Restored task ${displayValue(task.task_id || normalizedTaskId)} from ${source}.`);
      if (scroll) {
        scrollToSection("live-console");
      }
      if (!isTerminal(task.status)) {
        void pollTask(task.task_id || normalizedTaskId);
      }
      return task;
    } catch (error) {
      if (isAbortError(error) || state.controllers.taskRestore !== controller) {
        return null;
      }
      if (error?.code === "task_not_found") {
        storedTaskId(null);
        clearTaskLocation(normalizedTaskId);
        if (!state.task || state.task.task_id === normalizedTaskId) {
          state.task = null;
          state.events = [];
          renderEmptyState();
          renderTimeline([]);
        }
        elements.syncLabel.textContent = "Saved task unavailable";
        setFormStatus("The saved task is no longer available in the API.");
      } else {
        elements.syncLabel.textContent = "Task restore failed";
        setFormStatus(error?.message || "The saved task could not be restored.");
        setApiNotice(error?.message || "The saved task could not be restored.");
      }
      return null;
    }
  }

  function renderEmptyState() {
    elements.emptyState.hidden = false;
    elements.taskWorkspace.hidden = true;
    elements.syncLabel.textContent = "Awaiting a task";
  }

  function renderTaskGoal(task) {
    elements.taskGoal.textContent = `Goal / ${formatData(task.goal)}`;
  }

  function renderPlan(task) {
    const plan = Array.isArray(task.plan) ? task.plan : [];
    elements.planList.replaceChildren();
    elements.planCount.textContent = `${plan.length} ${plan.length === 1 ? "step" : "steps"}`;
    if (!plan.length) {
      addText(elements.planList, "p", "timeline-empty", "The API has not returned a plan for this task.");
      return;
    }

    plan.forEach((step, index) => {
      const card = document.createElement("article");
      card.className = "plan-card";
      const cardHeader = document.createElement("div");
      cardHeader.className = "plan-card-header";
      const titleBlock = document.createElement("div");
      addText(titleBlock, "span", "step-number", `STEP ${String(index + 1).padStart(2, "0")}`);
      addText(titleBlock, "h4", "", step.name);
      const stateClass = step.status === "completed"
        ? "is-complete"
        : step.status === "awaiting_approval"
          ? "is-waiting"
          : step.status === "failed" || step.status === "cancelled"
            ? "is-failed"
            : "";
      addText(cardHeader, "span", `step-state ${stateClass}`, step.status);
      cardHeader.prepend(titleBlock);
      card.append(cardHeader);

      const facts = document.createElement("dl");
      facts.className = "step-facts";
      addDefinition(facts, "Name", step.name);
      addDefinition(facts, "Tool", step.tool_name, true);
      addDefinition(facts, "Risk", step.risk);
      const approvalState = step.approval_required
        ? step.approved ? "Approved" : "Awaiting approval"
        : "Not required";
      addDefinition(facts, "Approval", approvalState);
      addDefinition(facts, "Attempts", `${displayValue(step.attempts, "0")} / ${displayValue(step.max_attempts, "—")}`);
      addOutputDefinition(facts, "Result", step.result);
      addOutputDefinition(facts, "Evidence", step.verification_evidence);
      card.append(facts);

      const canApprove = task.status === "awaiting_approval"
        && step.status === "awaiting_approval"
        && step.approval_required
        && step.approved !== true;
      if (canApprove) {
        const actions = document.createElement("div");
        actions.className = "step-actions";
        const approve = document.createElement("button");
        approve.type = "button";
        approve.className = "secondary-button";
        approve.textContent = state.actionPending ? "Approval pending…" : "Approve this step";
        approve.disabled = state.actionPending;
        approve.addEventListener("click", () => approveStep(step.step_id));
        actions.append(approve);
        card.append(actions);
      }
      elements.planList.append(card);
    });
  }

  function eventDetails(eventType) {
    return EVENT_DETAILS[eventType] || [eventType.replaceAll("_", " "), "neutral"];
  }

  function renderTimeline(events) {
    const safeEvents = Array.isArray(events) ? events : [];
    elements.timelineList.replaceChildren();
    elements.eventCount.textContent = `${safeEvents.length} ${safeEvents.length === 1 ? "event" : "events"}`;
    if (!safeEvents.length) {
      addText(elements.timelineList, "li", "timeline-empty", "Persisted events will appear here as the task moves.");
      return;
    }

    safeEvents.forEach((event) => {
      const item = document.createElement("li");
      const [title, tone] = eventDetails(displayValue(event.event_type, "event"));
      item.className = `timeline-item is-${tone}`;
      const marker = document.createElement("span");
      marker.className = "timeline-marker";
      marker.setAttribute("aria-hidden", "true");
      const content = document.createElement("div");
      content.className = "timeline-content";
      const meta = document.createElement("div");
      meta.className = "timeline-meta";
      addText(meta, "p", "timeline-title", title);
      addText(meta, "time", "timeline-time", event.created_at);
      content.append(meta);
      const detail = event.step_id
        ? `Step ${event.step_id} · ${formatData(event.payload)}`
        : formatData(event.payload);
      addText(content, "p", "timeline-detail", detail);
      item.append(marker, content);
      elements.timelineList.append(item);
    });
  }

  function renderTask(task) {
    if (!task) {
      renderEmptyState();
      return;
    }
    state.task = task;
    if (task.task_id) {
      storedTaskId(task.task_id);
      updateTaskLocation(task.task_id);
    }
    elements.emptyState.hidden = true;
    elements.taskWorkspace.hidden = false;
    elements.taskId.textContent = displayValue(task.task_id);
    renderTaskGoal(task);
    setStatusBadge(elements.taskStatus, task.status);
    elements.taskStatusDescription.textContent = statusDescription(task.status);
    elements.cancelTask.hidden = isTerminal(task.status);
    elements.refreshTask.hidden = isTerminal(task.status);
    elements.cancelTask.disabled = state.actionPending;
    elements.cancelTask.textContent = state.actionPending ? "Updating task…" : "Cancel task";
    elements.syncLabel.textContent = isTerminal(task.status) ? "Terminal state" : "Polling API state";
    renderPlan(task);
  }

  function stopPolling() {
    if (state.polling) {
      state.polling.controller.abort();
      state.polling = null;
    }
  }

  function delay(milliseconds, signal) {
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(resolve, milliseconds);
      signal.addEventListener("abort", () => {
        window.clearTimeout(timer);
        reject(new DOMException("Polling stopped", "AbortError"));
      }, { once: true });
    });
  }

  function isAbortError(error) {
    return error && (error.name === "AbortError" || error.code === 20);
  }

  async function pollTask(taskId) {
    stopPolling();
    const controller = new AbortController();
    const pollRun = { taskId, controller };
    state.polling = pollRun;
    let consecutiveFailures = 0;
    try {
      while (state.polling === pollRun) {
        try {
          const [task, eventDocument] = await Promise.all([
            requestJson(`/api/tasks/${encodeURIComponent(taskId)}`, { signal: controller.signal }),
            requestJson(`/api/tasks/${encodeURIComponent(taskId)}/events`, { signal: controller.signal }),
          ]);
          if (state.polling !== pollRun) {
            return;
          }
          consecutiveFailures = 0;
          state.events = Array.isArray(eventDocument?.events) ? eventDocument.events : [];
          renderTask(task);
          renderTimeline(state.events);
          if (isTerminal(task.status)) {
            void loadRecentRuns();
            return;
          }
          await delay(POLL_INTERVAL_MS, controller.signal);
        } catch (error) {
          if (isAbortError(error)) {
            throw error;
          }
          consecutiveFailures += 1;
          if (consecutiveFailures > POLL_RETRY_LIMIT) {
            throw error;
          }
          elements.syncLabel.textContent = `Connection interrupted · retry ${consecutiveFailures}/${POLL_RETRY_LIMIT}`;
          await delay(POLL_RETRY_BASE_MS * consecutiveFailures, controller.signal);
        }
      }
    } catch (error) {
      if (!isAbortError(error) && state.polling === pollRun) {
        elements.syncLabel.textContent = "Polling stopped";
        elements.refreshTask.hidden = false;
        setApiNotice(error.message || "The task could not be refreshed.");
      }
    } finally {
      if (state.polling === pollRun) {
        state.polling = null;
      }
    }
  }

  async function submitTask(event) {
    event.preventDefault();
    if (state.submitting) {
      return;
    }
    if (state.task && !isTerminal(state.task.status)) {
      setFormStatus("Cancel or finish the monitored task before starting another run.");
      return;
    }
    if (elements.scenarioMode.value === "workspace_report" && !state.workspaceAvailable) {
      setFormStatus("Workspace reporting requires an API configured with ARRAX_WORKSPACE_ROOT.");
      return;
    }
    if (elements.scenarioMode.value === "ai_goal" && !state.providerVerified) {
      setFormStatus("Verify the Gemini provider before submitting a provider-directed goal.");
      return;
    }

    state.submitting = true;
    stopPolling();
    elements.submitTask.disabled = true;
    elements.submitLabel.textContent = "Creating task…";
    setFormStatus("Submitting to the local API…");
    setApiNotice("");
    try {
      const task = await postJson("/api/tasks", { goal: buildGoal(), start: true });
      state.events = [];
      renderTask(task);
      renderTimeline([]);
      setFormStatus(`Task ${displayValue(task.task_id)} created.`);
      void loadRecentRuns();
      void pollTask(task.task_id);
    } catch (error) {
      setFormStatus(error.message || "The task could not be created.");
      if (error instanceof ApiRequestError) {
        setApiNotice(error.message);
      }
    } finally {
      state.submitting = false;
      elements.submitTask.disabled = false;
      elements.submitLabel.textContent = "Start controlled run";
    }
  }

  async function approveStep(stepId) {
    if (!state.task || state.actionPending || isTerminal(state.task.status)) {
      return;
    }
    state.actionPending = true;
    renderTask(state.task);
    setFormStatus("Sending explicit approval to the API…");
    try {
      const task = await postJson(`/api/tasks/${encodeURIComponent(state.task.task_id)}/approve`, { step_id: stepId });
      renderTask(task);
      setFormStatus("Approval recorded. The local worker will continue the task.");
    } catch (error) {
      setFormStatus(error.message || "Approval could not be recorded.");
      setApiNotice(error.message || "Approval could not be recorded.");
    } finally {
      state.actionPending = false;
      if (state.task) {
        renderTask(state.task);
      }
    }
  }

  async function cancelTask() {
    if (!state.task || state.actionPending || isTerminal(state.task.status)) {
      return;
    }
    state.actionPending = true;
    renderTask(state.task);
    setFormStatus("Sending cancellation request to the API…");
    try {
      const task = await postJson(`/api/tasks/${encodeURIComponent(state.task.task_id)}/cancel`, {});
      renderTask(task);
      setFormStatus("Cancellation request recorded.");
    } catch (error) {
      setFormStatus(error.message || "Cancellation could not be recorded.");
      setApiNotice(error.message || "Cancellation could not be recorded.");
    } finally {
      state.actionPending = false;
      if (state.task) {
        renderTask(state.task);
      }
    }
  }

  function connectNavigation() {
    document.querySelectorAll("[data-section-target]").forEach((button) => {
      button.addEventListener("click", () => {
        scrollToSection(button.dataset.sectionTarget);
      });
    });

    const sections = SECTION_IDS.map((id) => byId(id)).filter(Boolean);
    if (typeof window.IntersectionObserver === "function") {
      const observer = new window.IntersectionObserver((entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((left, right) => left.boundingClientRect.top - right.boundingClientRect.top);
        const section = visible[0]?.target;
        if (section?.id) {
          setActiveNavigation(section.id);
          updateSectionLocation(section.id, true);
        }
      }, {
        rootMargin: "-18% 0px -68% 0px",
        threshold: [0, 0.2],
      });
      sections.forEach((section) => observer.observe(section));
    }

    const handleLocationChange = () => {
      const sectionId = sectionFromHash();
      if (sectionId) {
        scrollToSection(sectionId, { updateLocation: false, behavior: "auto" });
      }
      const taskIdFromUrl = taskIdFromLocation();
      const taskId = taskIdFromUrl || readStoredTaskId();
      if (taskId && taskId !== state.task?.task_id) {
        void restoreTask(taskId, {
          scroll: !sectionId,
          source: taskIdFromUrl ? "URL state" : "local browser state",
        });
      }
    };
    window.addEventListener("hashchange", handleLocationChange);
    window.addEventListener("popstate", handleLocationChange);
    handleLocationChange();
  }

  function cacheElements() {
    elements.apiNotice = byId("api-notice");
    elements.apiNoticeText = byId("api-notice-text");
    elements.healthDot = byId("health-dot");
    elements.healthStatus = byId("health-status");
    elements.executionMode = byId("execution-mode");
    elements.providerMode = byId("provider-mode");
    elements.modeChipLabel = byId("mode-chip-label");
    elements.footerProviderStatus = byId("footer-provider-status");
    elements.scenarioMode = byId("scenario-mode");
    elements.scenarioHelp = byId("scenario-help");
    elements.scenarioContext = byId("scenario-context");
    elements.voiceInput = byId("voice-input");
    elements.voiceInputLabel = byId("voice-input-label");
    elements.speakStatus = byId("speak-status");
    elements.speakStatusLabel = byId("speak-status-label");
    elements.voiceStatus = byId("voice-status");
    elements.goalForm = byId("goal-form");
    elements.formStatus = byId("form-status");
    elements.submitTask = byId("submit-task");
    elements.submitLabel = byId("submit-label");
    elements.emptyState = byId("empty-state");
    elements.taskWorkspace = byId("task-workspace");
    elements.emptyTitle = byId("empty-title");
    elements.emptyDescription = byId("empty-description");
    elements.syncLabel = byId("sync-label");
    elements.taskId = byId("task-id");
    elements.taskGoal = byId("task-goal");
    elements.taskStatus = byId("task-status");
    elements.taskStatusDescription = byId("task-status-description");
    elements.refreshTask = byId("refresh-task");
    elements.cancelTask = byId("cancel-task");
    elements.recentRunsCount = byId("recent-runs-count");
    elements.refreshRecentRuns = byId("refresh-recent-runs");
    elements.recentRunsLoading = byId("recent-runs-loading");
    elements.recentRunsEmpty = byId("recent-runs-empty");
    elements.recentRunsUnavailable = byId("recent-runs-unavailable");
    elements.recentRunsUnavailableMessage = byId("recent-runs-unavailable-message");
    elements.recentRunsList = byId("recent-runs-list");
    elements.planList = byId("plan-list");
    elements.planCount = byId("plan-count");
    elements.timelineList = byId("timeline-list");
    elements.eventCount = byId("event-count");
    elements.workflowForm = byId("workflow-form");
    elements.workflowName = byId("workflow-name");
    elements.workflowDescription = byId("workflow-description");
    elements.workflowSource = byId("workflow-source");
    elements.workflowStepBuilder = byId("workflow-step-builder");
    elements.workflowStepList = byId("workflow-step-list");
    elements.addWorkflowStep = byId("add-workflow-step");
    elements.workflowBuilderStatus = byId("workflow-builder-status");
    elements.workflowGoalPreview = byId("workflow-goal-preview");
    elements.workflowFormStatus = byId("workflow-form-status");
    elements.saveWorkflow = byId("save-workflow");
    elements.saveWorkflowLabel = byId("save-workflow-label");
    elements.refreshWorkflows = byId("refresh-workflows");
    elements.workflowSyncLabel = byId("workflow-sync-label");
    elements.workflowLoadingState = byId("workflow-loading-state");
    elements.workflowEmptyState = byId("workflow-empty-state");
    elements.workflowErrorState = byId("workflow-error-state");
    elements.workflowErrorMessage = byId("workflow-error-message");
    elements.workflowList = byId("workflow-list");
    elements.developerWorkspaceStatus = byId("developer-workspace-status");
    elements.developerToolCount = byId("developer-tool-count");
    elements.developerToolList = byId("developer-tool-list");
    elements.openWorkspaceReport = byId("open-workspace-report");
    elements.developerActionStatus = byId("developer-action-status");
    elements.overviewSyncLabel = byId("overview-sync-label");
    elements.overviewActive = byId("overview-active");
    elements.overviewActiveDetail = byId("overview-active-detail");
    elements.overviewCompleted = byId("overview-completed");
    elements.overviewFailed = byId("overview-failed");
    elements.overviewFailedDetail = byId("overview-failed-detail");
    elements.overviewRecovery = byId("overview-recovery");
    elements.overviewTotal = byId("overview-total");
    elements.overviewLastUpdated = byId("overview-last-updated");
    elements.verifyProvider = byId("verify-provider");
    elements.verifyProviderLabel = byId("verify-provider-label");
    elements.providerActionStatus = byId("provider-action-status");
    elements.measureSystem = byId("measure-system");
    elements.measureSystemLabel = byId("measure-system-label");
    elements.systemCapabilitySummary = byId("system-capability-summary");
    elements.systemCapabilityList = byId("system-capability-list");
    elements.systemSnapshotEmpty = byId("system-snapshot-empty");
    elements.systemSnapshotError = byId("system-snapshot-error");
    elements.systemSnapshotErrorMessage = byId("system-snapshot-error-message");
    elements.systemSnapshotResults = byId("system-snapshot-results");
    elements.systemMeasuredAt = byId("system-measured-at");
    elements.systemSnapshotStatus = byId("system-snapshot-status");
    elements.systemCpu = byId("system-cpu");
    elements.systemMemory = byId("system-memory");
    elements.systemDisk = byId("system-disk");
    elements.systemNetwork = byId("system-network");
    elements.systemProcesses = byId("system-processes");
    elements.systemRecommendations = byId("system-recommendations");
    elements.memoryForm = byId("memory-form");
    elements.memoryKey = byId("memory-key");
    elements.memoryCategory = byId("memory-category");
    elements.memoryValue = byId("memory-value");
    elements.memoryFormStatus = byId("memory-form-status");
    elements.saveMemory = byId("save-memory");
    elements.saveMemoryLabel = byId("save-memory-label");
    elements.refreshMemory = byId("refresh-memory");
    elements.resetMemory = byId("reset-memory");
    elements.memorySyncLabel = byId("memory-sync-label");
    elements.memoryLoadingState = byId("memory-loading-state");
    elements.memoryEmptyState = byId("memory-empty-state");
    elements.memoryErrorState = byId("memory-error-state");
    elements.memoryErrorMessage = byId("memory-error-message");
    elements.memoryList = byId("memory-list");
    elements.toolList = byId("tool-list");
    elements.toolCount = byId("tool-count");
    elements.capabilityExecution = byId("capability-execution");
    elements.capabilityProvider = byId("capability-provider");
    elements.capabilityProviderModel = byId("capability-provider-model");
    elements.capabilityProviderVerification = byId("capability-provider-verification");
    elements.evaluationModeLabel = byId("evaluation-mode-label");
    elements.evaluationScenarioSelect = byId("evaluation-scenario-select");
    elements.runEvaluation = byId("run-evaluation");
    elements.runEvaluationLabel = byId("run-evaluation-label");
    elements.evaluationCatalogPanel = byId("evaluation-catalog-panel");
    elements.evaluationCatalogCount = byId("evaluation-catalog-count");
    elements.evaluationCatalogList = byId("evaluation-catalog-list");
    elements.evaluationEmptyState = byId("evaluation-empty-state");
    elements.evaluationLoadingState = byId("evaluation-loading-state");
    elements.evaluationErrorState = byId("evaluation-error-state");
    elements.evaluationErrorMessage = byId("evaluation-error-message");
    elements.evaluationResults = byId("evaluation-results");
    elements.evaluationRunTimestamp = byId("evaluation-run-timestamp");
    elements.evaluationCompletionRate = byId("evaluation-completion-rate");
    elements.evaluationRecoveryRate = byId("evaluation-recovery-rate");
    elements.evaluationVerificationRate = byId("evaluation-verification-rate");
    elements.evaluationResultCount = byId("evaluation-result-count");
    elements.evaluationResultList = byId("evaluation-result-list");
    elements.evaluationNote = byId("evaluation-note");
  }

  function init() {
    cacheElements();
    elements.scenarioMode.addEventListener("change", () => {
      updateModeHelp();
      renderWorkflowGoalPreview();
    });
    elements.scenarioContext.addEventListener("input", renderWorkflowGoalPreview);
    elements.goalForm.addEventListener("submit", submitTask);
    elements.refreshTask.addEventListener("click", () => {
      if (state.task?.task_id) {
        void restoreTask(state.task.task_id, { source: "manual reconnect" });
      }
    });
    elements.cancelTask.addEventListener("click", cancelTask);
    elements.runEvaluation.addEventListener("click", runEvaluation);
    elements.voiceInput.addEventListener("click", toggleVoiceInput);
    elements.speakStatus.addEventListener("click", readLastStatus);
    elements.workflowForm.addEventListener("submit", saveWorkflow);
    elements.workflowSource.addEventListener("change", () => {
      state.workflowSource = elements.workflowSource.value === "ordered_plan" ? "ordered_plan" : "scenario";
      renderWorkflowStepBuilder();
      renderWorkflowGoalPreview();
    });
    elements.addWorkflowStep.addEventListener("click", addWorkflowStep);
    elements.refreshWorkflows.addEventListener("click", loadWorkflows);
    elements.measureSystem.addEventListener("click", loadSystemSnapshot);
    elements.memoryForm.addEventListener("submit", saveMemory);
    elements.refreshMemory.addEventListener("click", loadMemory);
    elements.resetMemory.addEventListener("click", resetMemory);
    elements.verifyProvider.addEventListener("click", verifyProvider);
    elements.openWorkspaceReport.addEventListener("click", openWorkspaceReport);
    elements.refreshRecentRuns.addEventListener("click", loadRecentRuns);
    updateModeHelp();
    resetWorkflowBuilder();
    renderWorkflowGoalPreview();
    renderEmptyState();
    renderCapabilities({
      tools: [],
      execution_mode: "loading",
      provider_status: { message: "Checking backend provider status." },
    });
    setEvaluationState("empty");
    elements.evaluationModeLabel.textContent = "Offline mode: checking";
    elements.evaluationCatalogCount.textContent = "Loading";
    elements.evaluationCatalogList.replaceChildren();
    addText(elements.evaluationCatalogList, "li", "evaluation-empty", "Loading the actual catalog from the API.");
    setWorkflowState("loading");
    setMemoryState("loading");
    setRecentRunsState("loading");
    elements.recentRunsCount.textContent = "Checking";
    elements.systemSnapshotResults.hidden = true;
    elements.systemSnapshotError.hidden = true;
    configureVoice();
    connectNavigation();
    void loadSystemData();
    void loadRecentRuns();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
