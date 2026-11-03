"""Model registry + resolver (SB-1518): defaults, overrides, fail-fast checks, describe()."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sb_models import ModelConfigError, Registry, registry

KEYS = {"ANTHROPIC_API_KEY": "k", "OPENROUTER_API_KEY": "k", "OLLAMA_BASE_URL": "http://x"}
AGENTS = ("docs-agent", "docs-agent-judge", "sb-support", "mt-agent", "adk-docs-client")


def test_shipped_registry_loads_and_every_default_resolves() -> None:
    reg = registry()
    for agent in AGENTS:
        spec = reg.resolve(agent, env=KEYS)
        assert spec.ref in reg.models
    # Defaults unchanged by the registry: Haiku 5.5 for the agents.
    assert reg.resolve("docs-agent", env=KEYS).ref == "anthropic:claude-haiku-5-5"


def test_every_model_has_its_provider_and_metadata() -> None:
    for spec in registry().models.values():
        assert spec.context_window > 0
        assert spec.credential
        assert spec.local == (spec.provider == "ollama")


@pytest.mark.parametrize(
    "ref", ["openrouter:deepseek/deepseek-v4.1-flash", "ollama:gemma4:12b-16k"]
)
def test_env_var_switches_a_pydantic_ai_agent_with_no_code_change(ref: str) -> None:
    spec = registry().resolve("docs-agent", env={**KEYS, "DOCS_AGENT_MODEL": ref})
    assert spec.ref == ref


def test_argument_beats_env_beats_default() -> None:
    env = {**KEYS, "SB_SUPPORT_MODEL": "openrouter:z-ai/glm-5.3-flash"}
    reg = registry()
    assert reg.resolve("sb-support", env=env).ref == "openrouter:z-ai/glm-5.3-flash"
    assert reg.resolve("sb-support", "anthropic:claude-sonnet-5-5", env=env).name == (
        "claude-sonnet-5-5"
    )


def test_bare_name_resolves_when_unambiguous() -> None:
    assert registry().resolve("mt-agent", "claude-haiku-5-5", env=KEYS).provider == "anthropic"


def test_unregistered_model_fails_with_the_fix() -> None:
    with pytest.raises(ModelConfigError, match="not registered.*models.toml"):
        registry().resolve("docs-agent", "openrouter:some/new-model", env=KEYS)


def test_missing_credential_names_the_variable() -> None:
    env = {"DOCS_AGENT_MODEL": "openrouter:deepseek/deepseek-v4.1-flash"}
    with pytest.raises(ModelConfigError, match=r"\$OPENROUTER_API_KEY"):
        registry().resolve("docs-agent", env=env)
    # Builders skip the credential check; startup (main) does it.
    assert registry().resolve("docs-agent", env=env, check_credentials=False)


def test_too_small_context_fails_instead_of_truncating_silently() -> None:
    # gemma4:12b at Ollama's default 4k context scored 31% (silent truncation).
    with pytest.raises(ModelConfigError, match="4,096-token context.*16,384"):
        registry().resolve("docs-agent", "ollama:gemma4:12b", env=KEYS)


def test_provider_restriction_carries_its_reason() -> None:
    with pytest.raises(ModelConfigError, match="Claude-only.*LiteLLM"):
        registry().resolve("mt-agent", "openrouter:z-ai/glm-5.3-flash", env=KEYS)


def test_missing_capability_fails() -> None:
    reg = Registry.from_dict(
        {
            "providers": {"p": {"credential": "K"}},
            "agents": {"a": {"model": "p:plain", "env": "A_MODEL", "needs": {"tools": True}}},
            "models": {"p:plain": {"context_window": 1000}},
        }
    )
    with pytest.raises(ModelConfigError, match="no tool calling"):
        reg.resolve("a", env={"K": "x"})


def test_unknown_agent_and_bad_model_key() -> None:
    with pytest.raises(ModelConfigError, match="unknown agent"):
        registry().resolve("nope", env=KEYS)
    with pytest.raises(ModelConfigError, match="<provider>:<name>"):
        Registry.from_dict({"providers": {}, "models": {"x:y": {"context_window": 1}}})


def test_eval_result_recorded() -> None:
    ev = registry().models["openrouter:deepseek/deepseek-v4.1-flash"].eval
    assert ev is not None and ev.passed == 13 and round(ev.pass_rate, 2) == 0.93


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("anthropic:claude-haiku-5-5", ("anthropic", "claude-haiku-5-5", False)),
        ("claude-haiku-5-5", ("anthropic", "claude-haiku-5-5", False)),  # ADK bare name
        ("ollama:gemma4:12b-16k", ("ollama", "gemma4:12b-16k", True)),
        ("ollama:unregistered", ("ollama", "unregistered", True)),
        ("mystery", (None, "mystery", None)),
        (
            SimpleNamespace(system="openrouter", model_name="deepseek/deepseek-v4.1-flash"),
            ("openrouter", "deepseek/deepseek-v4.1-flash", False),
        ),
        (SimpleNamespace(model="claude-sonnet-5-5"), ("anthropic", "claude-sonnet-5-5", False)),
        (SimpleNamespace(system="test", model_name="test"), ("test", "test", None)),
    ],
)
def test_describe(model: object, expected: tuple) -> None:  # type: ignore[type-arg]
    info = registry().describe(model)
    assert (info.provider, info.name, info.local) == expected
