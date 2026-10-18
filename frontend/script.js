(() => {
  "use strict";

  const TERMINAL_STATUSES = new Set([
    "completed",
    "partially_completed",
    "failed",
    "cancelled",
  ]);
  const POLL_INTERVAL_MS = 650;

  const MODE_DETAILS = {
    success: {
      help: "Runs fixture.success and requires fixture.verify evidence before completion.",
      fallback: "verified local action",
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
    } catch (_error) {
      throw new ApiRequestError("The API could not be reached. Start the local Flask app and retry.");
    }

    let data = null;
    try {
      data = await response.json();
    } catch (_error) {
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

  function renderProviderLabels(status) {
    const label = providerDisplay(status);
    elements.providerMode.textContent = `Provider: ${label}`;
    elements.capabilityProvider.textContent = label;
    elements.modeChipLabel.textContent = label.toUpperCase();
    elements.footerProviderStatus.textContent = `Provider status: ${label}`;
  }

  function renderHealth(health) {
    const ready = health && health.ready === true && health.status === "ready";
    const providerStatus = health?.provider_status || {};
    state.healthReady = ready;
    elements.healthStatus.textContent = ready ? "Ready" : "Unavailable";
    elements.healthDot.classList.toggle("is-unavailable", !ready);
    elements.executionMode.textContent = `Execution: ${displayValue(health?.execution_mode, "unavailable")}`;
    renderProviderLabels(providerStatus);
    elements.capabilityProviderModel.textContent = displayValue(providerStatus.model, "unavailable");
    elements.capabilityProviderVerification.textContent = providerStatus.verified === true
      ? "Verified by backend"
      : displayValue(providerStatus.message, "Not verified");
    if (ready) {
      setApiNotice("");
    }
  }

  function renderHealthFailure(error) {
    state.healthReady = false;
    elements.healthStatus.textContent = "Unavailable";
    elements.healthDot.classList.add("is-unavailable");
    elements.executionMode.textContent = "Execution: unavailable";
    renderProviderLabels(null);
    elements.capabilityProviderModel.textContent = "unavailable";
    elements.capabilityProviderVerification.textContent = "unavailable";
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
    elements.toolList.replaceChildren();
    elements.toolCount.textContent = `${tools.length} registered`;
    elements.capabilityExecution.textContent = displayValue(capabilities?.execution_mode, "unavailable");
    const providerStatus = capabilities?.provider_status || {};
    renderProviderLabels(providerStatus);
    elements.capabilityProviderModel.textContent = displayValue(
      capabilities?.configured_model || providerStatus.model,
      "unavailable",
    );
    elements.capabilityProviderVerification.textContent = providerStatus.verified === true
      ? "Verified by backend"
      : displayValue(providerStatus.message, "Not verified");
    if (!tools.length) {
      addText(elements.toolList, "li", "tool-empty", "No registered tools reported by the API.");
      return;
    }
    tools.forEach((tool) => {
      addText(elements.toolList, "li", "", tool);
    });
  }

  function renderCapabilitiesFailure(error) {
    state.workspaceAvailable = false;
    const workspaceOption = elements.scenarioMode.querySelector('option[value="workspace_report"]');
    if (workspaceOption) {
      workspaceOption.disabled = true;
    }
    elements.toolList.replaceChildren();
    addText(elements.toolList, "li", "tool-empty", "Capabilities unavailable.");
    elements.toolCount.textContent = "Unavailable";
    elements.capabilityExecution.textContent = "unavailable";
    renderProviderLabels(null);
    elements.capabilityProviderModel.textContent = "unavailable";
    elements.capabilityProviderVerification.textContent = "unavailable";
    if (!state.healthReady) {
      setApiNotice(error.message || "The API is unavailable.");
    }
  }

  async function loadSystemData() {
    const results = await Promise.allSettled([
      requestJson("/api/health"),
      requestJson("/api/capabilities"),
      requestJson("/api/provider/status"),
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
  }

  function buildGoal() {
    const mode = elements.scenarioMode.value;
    const detail = elements.scenarioContext.value.trim() || MODE_DETAILS[mode].fallback;
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

  function updateModeHelp() {
    const mode = elements.scenarioMode.value;
    const details = MODE_DETAILS[mode] || MODE_DETAILS.success;
    elements.scenarioHelp.textContent = details.help;
  }

  function setFormStatus(message) {
    elements.formStatus.textContent = message || "";
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
    elements.emptyState.hidden = true;
    elements.taskWorkspace.hidden = false;
    elements.taskId.textContent = displayValue(task.task_id);
    renderTaskGoal(task);
    setStatusBadge(elements.taskStatus, task.status);
    elements.taskStatusDescription.textContent = statusDescription(task.status);
    elements.cancelTask.hidden = isTerminal(task.status);
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
    try {
      while (state.polling === pollRun) {
        const [task, eventDocument] = await Promise.all([
          requestJson(`/api/tasks/${encodeURIComponent(taskId)}`, { signal: controller.signal }),
          requestJson(`/api/tasks/${encodeURIComponent(taskId)}/events`, { signal: controller.signal }),
        ]);
        if (state.polling !== pollRun) {
          return;
        }
        state.events = Array.isArray(eventDocument?.events) ? eventDocument.events : [];
        renderTask(task);
        renderTimeline(state.events);
        if (isTerminal(task.status)) {
          return;
        }
        await delay(POLL_INTERVAL_MS, controller.signal);
      }
    } catch (error) {
      if (!isAbortError(error) && state.polling === pollRun) {
        elements.syncLabel.textContent = "Polling stopped";
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
        const section = byId(button.dataset.sectionTarget);
        if (!section) {
          return;
        }
        section.scrollIntoView({ behavior: isReducedMotion() ? "auto" : "smooth", block: "start" });
        document.querySelectorAll("[data-section-target]").forEach((item) => {
          const active = item === button;
          item.classList.toggle("is-active", active);
          if (active) {
            item.setAttribute("aria-current", "page");
          } else {
            item.removeAttribute("aria-current");
          }
        });
      });
    });
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
    elements.cancelTask = byId("cancel-task");
    elements.planList = byId("plan-list");
    elements.planCount = byId("plan-count");
    elements.timelineList = byId("timeline-list");
    elements.eventCount = byId("event-count");
    elements.toolList = byId("tool-list");
    elements.toolCount = byId("tool-count");
    elements.capabilityExecution = byId("capability-execution");
    elements.capabilityProvider = byId("capability-provider");
    elements.capabilityProviderModel = byId("capability-provider-model");
    elements.capabilityProviderVerification = byId("capability-provider-verification");
  }

  function init() {
    cacheElements();
    connectNavigation();
    elements.scenarioMode.addEventListener("change", updateModeHelp);
    elements.goalForm.addEventListener("submit", submitTask);
    elements.cancelTask.addEventListener("click", cancelTask);
    updateModeHelp();
    renderEmptyState();
    renderCapabilities({
      tools: [],
      execution_mode: "loading",
      provider_status: { message: "Checking backend provider status." },
    });
    void loadSystemData();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
