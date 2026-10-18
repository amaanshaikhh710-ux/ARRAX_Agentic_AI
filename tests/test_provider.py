from __future__ import annotations

import json

import pytest

from arrax import (
    AgentCore,
    DeterministicPlanner,
    GeminiProvider,
    HybridPlanner,
    HttpResponse,
    PlanningError,
    ProviderConfig,
    ProviderConfigError,
    ProviderMode,
    ProviderStatus,
    StructuredProviderPlanner,
    TaskStatus,
    create_app,
    default_registry,
)


class QueueTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        if not self.responses:
            raise AssertionError("unexpected provider request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def model_list(name="test-model", methods=("generateContent",)):
    return HttpResponse(
        200,
        json.dumps({"models": [{"name": f"models/{name}", "supportedGenerationMethods": list(methods)}]}),
    )


def generation(text="OK"):
    return HttpResponse(200, json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}))


def provider_for(*responses, mode=ProviderMode.AUTO, model="test-model"):
    transport = QueueTransport(*responses)
    provider = GeminiProvider(
        ProviderConfig(mode=mode, api_key="server-test-key", model=model, max_attempts=2),
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    return provider, transport


def test_no_key_defaults_to_deterministic_without_network(monkeypatch):
    monkeypatch.delenv("ARRAX_AI_MODE", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    core = AgentCore()
    try:
        status = core.provider_status
        assert status["mode"] == "deterministic"
        assert status["last_mode"] == "deterministic"
        assert status["verified"] is False
        task = core.create_task({"type": "success", "message": "offline"})
        assert core.run_task(task.id).status.value == "completed"
    finally:
        core.close()


@pytest.mark.parametrize(
    "environment",
    [
        {"ARRAX_AI_MODE": "unknown"},
        {"ARRAX_AI_MODE": "gemini", "GEMINI_MODEL": "models/not/a/model"},
        {"GEMINI_API_KEY": "bad key"},
    ],
)
def test_provider_configuration_rejects_malformed_values(environment):
    with pytest.raises(ProviderConfigError):
        ProviderConfig.from_env(environment)


def test_model_verification_rejects_missing_or_unsupported_exact_model():
    missing, _ = provider_for(model_list("different"), mode=ProviderMode.GEMINI)
    status = missing.verify()
    assert status.verified is False
    assert status.last_error == "model_unavailable"

    unsupported, _ = provider_for(model_list("test-model", methods=("embedContent",)), mode=ProviderMode.GEMINI)
    status = unsupported.verify()
    assert status.verified is False
    assert status.last_error == "model_unavailable"


def test_fake_canary_success_verifies_without_exposing_key():
    provider, transport = provider_for(model_list(), generation(), mode=ProviderMode.GEMINI)
    status = provider.verify()
    assert status.verified is True
    assert status.model == "test-model"
    assert "server-test-key" not in repr(status)
    assert "server-test-key" not in status.message
    assert all("server-test-key" not in call[1] for call in transport.calls)
    # The key is present only in the server-side request header, never in a
    # URL, returned status, exception, task, event, or frontend document.
    assert transport.calls[0][0] == "GET"
    assert transport.calls[0][1] == "https://generativelanguage.googleapis.com/v1beta/models"
    assert transport.calls[0][2]["x-goog-api-key"] == "server-test-key"
    assert transport.calls[1][0] == "POST"
    assert "/v1beta/models/test-model:generateContent" in transport.calls[1][1]
    assert json.loads(transport.calls[1][3].decode("utf-8"))["contents"][0]["parts"][0]["text"]


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (TimeoutError("transport timeout"), "timeout"),
        (HttpResponse(429, "{}"), "rate_limit"),
        (HttpResponse(404, "{}"), "model_unavailable"),
        (HttpResponse(200, "not-json"), "invalid_response"),
    ],
)
def test_provider_failures_are_classified_and_bounded(response, category):
    if isinstance(response, HttpResponse) and response.status == 404:
        transport = QueueTransport(model_list(), response)
    elif isinstance(response, HttpResponse) and response.status == 200:
        transport = QueueTransport(model_list(), response)
    else:
        transport = QueueTransport(response, response)
    provider = GeminiProvider(
        ProviderConfig(mode=ProviderMode.GEMINI, api_key="server-test-key", model="test-model", max_attempts=2),
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    status = provider.verify()
    assert status.verified is False
    assert status.last_error == category
    assert len(transport.calls) <= 2


class FakePlannerProvider:
    def __init__(self, text, *, mode=ProviderMode.GEMINI, configured=True, verified=True):
        self.text = text
        self.config = ProviderConfig(mode=mode, api_key="server-test-key" if configured else None, model="test-model")
        self._status = ProviderStatus(
            mode=ProviderMode.GEMINI if verified else mode,
            configured=configured,
            verified=verified,
            model="test-model",
            message="fake provider",
            last_mode=ProviderMode.GEMINI if verified else None,
            configured_mode=mode,
        )
        self.generate_calls = 0
        self.verify_calls = 0

    def status(self):
        return self._status

    def verify(self):
        self.verify_calls += 1
        return self._status

    def generate_text(self, _prompt, *, max_output_chars):
        self.generate_calls += 1
        return self.text


def valid_plan_text():
    return json.dumps(
        {
            "steps": [
                {
                    "step_id": "local-success",
                    "name": "Run local success",
                    "tool_name": "fixture.success",
                    "input": {"message": "provider plan"},
                    "dependencies": [],
                    "risk": "read_only",
                    "approval_required": False,
                }
            ]
        }
    )


def test_api_verify_query_is_the_only_status_call_that_invokes_provider(tmp_path):
    fake = FakePlannerProvider(valid_plan_text(), verified=False)
    app = create_app(
        db_path=str(tmp_path / "verify.sqlite"),
        provider=fake,
        config={"TESTING": True},
    )
    try:
        client = app.test_client()
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/capabilities").status_code == 200
        assert client.get("/api/provider/status").status_code == 200
        assert fake.verify_calls == 0
        assert client.get("/api/provider/status?verify=1").status_code == 200
        assert fake.verify_calls == 1
    finally:
        app.extensions["arrax"]["shutdown"]()


def test_structured_provider_planner_accepts_valid_json_plan():
    planner = StructuredProviderPlanner(FakePlannerProvider(valid_plan_text()), default_registry())
    plan = planner.plan({"type": "success", "message": "goal"})
    assert plan[0].tool_name == "fixture.success"
    assert plan[0].risk.value == "read_only"


def test_verified_provider_can_plan_a_natural_language_goal():
    fake = FakePlannerProvider(valid_plan_text(), mode=ProviderMode.GEMINI)
    core = AgentCore(provider=fake)
    try:
        task = core.create_task({"type": "ai_goal", "message": "verify a local outcome"})
        finished = core.run_task(task.id)

        assert finished.status is TaskStatus.COMPLETED
        assert finished.plan[0].tool_name == "fixture.success"
        assert fake.generate_calls == 1
        assert core.provider_status["last_mode"] == "gemini"
    finally:
        core.close()


@pytest.mark.parametrize(
    "text",
    [
        '{"steps":[{"tool_name":"shell.execute","input":{}}]}',
        '{"steps":[{"tool_name":"fixture.success","input":{"message":"x"},"risk":"destructive-ish"}]}',
        '{"steps":[{"tool_name":"fixture.success","input":{"message":"x"},"approved":true}]}',
        "This is not JSON",
        '{"steps":[{"tool_name":"fixture.success","input":{"message":"x"},"dependencies":["missing"]}]}',
        '{"steps":[{"tool_name":"fixture.approval","input":{"message":"x"},"risk":"read_only"}]}',
        '{"steps":[{"tool_name":"fixture.success","input":{"message":"x","command":"cat secret"}}]}',
    ],
)
def test_structured_provider_planner_rejects_untrusted_output(text):
    planner = StructuredProviderPlanner(FakePlannerProvider(text), default_registry())
    with pytest.raises(PlanningError):
        planner.plan({"type": "success"})


def test_auto_fallback_is_visible_and_gemini_mode_fails_clearly():
    deterministic = DeterministicPlanner()
    auto_provider = FakePlannerProvider(valid_plan_text(), mode=ProviderMode.AUTO, configured=False, verified=False)
    auto = HybridPlanner(deterministic, auto_provider, registry=default_registry())
    plan = auto.plan({"type": "success", "message": "fallback"})
    assert plan[0].tool_name == "fixture.success"
    assert auto.last_mode is ProviderMode.DETERMINISTIC
    assert auto.status().to_dict()["mode"] == "deterministic"
    assert auto_provider.generate_calls == 0

    unavailable = FakePlannerProvider(valid_plan_text(), mode=ProviderMode.GEMINI, configured=False, verified=False)
    strict = HybridPlanner(deterministic, unavailable, registry=default_registry())
    with pytest.raises(PlanningError, match="GEMINI_API_KEY"):
        strict.plan({"type": "success"})


def test_api_provider_status_is_local_by_default_and_contains_no_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("ARRAX_AI_MODE", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    app = create_app(db_path=str(tmp_path / "provider.sqlite"), config={"TESTING": True})
    try:
        client = app.test_client()
        plain = client.get("/api/provider/status")
        assert plain.status_code == 200
        assert plain.get_json()["verification_requested"] is False
        assert plain.get_json()["mode"] == "deterministic"
        assert "GEMINI_API_KEY" not in plain.get_data(as_text=True)

        verified = client.get("/api/provider/status?verify=1")
        assert verified.status_code == 200
        assert verified.get_json()["verification_requested"] is True
        assert verified.get_json()["last_error"] == "missing_key"
        assert "GEMINI_API_KEY" not in verified.get_data(as_text=True)

        health = client.get("/api/health").get_json()
        capabilities = client.get("/api/capabilities").get_json()
        assert health["provider_status"]["verified"] is False
        assert capabilities["configured_model"] == health["configured_model"]
        assert "server-test-key" not in json.dumps({"health": health, "capabilities": capabilities})
    finally:
        app.extensions["arrax"]["shutdown"]()
