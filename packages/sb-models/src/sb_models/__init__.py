"""Which model each SB agent runs, and what that model can do (SB-1518).

The registry (`models.toml`) is data: providers, agents and models with their
capabilities and latest eval. `resolve(agent)` picks an agent's model (argument, then
the agent's env var, then its default) and checks it before the agent starts:
unknown model, missing credential, a provider the agent's framework can't drive, or a
missing capability all raise `ModelConfigError` with the fix in the message.

`describe(model)` is the telemetry side: provider, name and local/hosted for any model
reference or framework model object, never raising.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

__all__ = [
    "AgentConfig",
    "EvalResult",
    "ModelConfigError",
    "ModelInfo",
    "ModelSpec",
    "Needs",
    "Registry",
    "describe",
    "registry",
    "resolve",
]


class ModelConfigError(ValueError):
    """An agent's model can't run: say what's wrong and how to fix it."""


@dataclass(frozen=True)
class EvalResult:
    suite: str
    passed: int
    cases: int
    date: str

    @property
    def pass_rate(self) -> float:
        return self.passed / self.cases if self.cases else 0.0


@dataclass(frozen=True)
class ModelSpec:
    """One registered model. `ref` is the framework string, e.g. `anthropic:claude-haiku-5-5`."""

    ref: str
    provider: str
    name: str
    local: bool
    context_window: int
    tools: bool
    structured_output: bool
    credential: str | None
    eval: EvalResult | None = None


@dataclass(frozen=True)
class Needs:
    """What an agent requires of its model."""

    tools: bool = False
    structured_output: bool = False
    min_context: int = 0


@dataclass(frozen=True)
class AgentConfig:
    name: str
    model: str
    env: str
    needs: Needs = Needs()
    providers: tuple[str, ...] = ()  # empty = any provider
    providers_reason: str = ""


@dataclass(frozen=True)
class ModelInfo:
    """What telemetry records about a model. `local` is None when unknown."""

    provider: str | None
    name: str
    local: bool | None


@dataclass
class Registry:
    models: dict[str, ModelSpec]
    agents: dict[str, AgentConfig]
    local_providers: frozenset[str] = field(default_factory=frozenset)
    providers: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def load(cls, path: Path | None = None) -> Registry:
        if path is None:
            text = resources.files("sb_models").joinpath("models.toml").read_text()
        else:
            text = path.read_text()
        return cls.from_dict(tomllib.loads(text))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Registry:
        providers = data.get("providers", {})
        models: dict[str, ModelSpec] = {}
        for ref, raw in data.get("models", {}).items():
            provider, sep, name = ref.partition(":")
            if not sep or provider not in providers:
                raise ModelConfigError(
                    f"model {ref!r}: expected '<provider>:<name>', "
                    f"known providers {sorted(providers)}"
                )
            ev = raw.get("eval")
            models[ref] = ModelSpec(
                ref=ref,
                provider=provider,
                name=name,
                local=bool(providers[provider].get("local", False)),
                context_window=int(raw["context_window"]),
                tools=bool(raw.get("tools", False)),
                structured_output=bool(raw.get("structured_output", False)),
                credential=providers[provider].get("credential"),
                eval=EvalResult(**ev) if ev else None,
            )
        agents = {
            name: AgentConfig(
                name=name,
                model=raw["model"],
                env=raw["env"],
                needs=Needs(**raw.get("needs", {})),
                providers=tuple(raw.get("providers", ())),
                providers_reason=raw.get("providers_reason", ""),
            )
            for name, raw in data.get("agents", {}).items()
        }
        local = frozenset(p for p, raw in providers.items() if raw.get("local"))
        return cls(models, agents, local, frozenset(providers))

    def resolve(
        self,
        agent: str,
        override: str | None = None,
        *,
        env: Mapping[str, str] | None = None,
        check_credentials: bool = True,
    ) -> ModelSpec:
        """The model `agent` should run: `override`, else its env var, else its default."""
        env = os.environ if env is None else env
        if agent not in self.agents:
            raise ModelConfigError(f"unknown agent {agent!r}: add [agents.{agent}] to models.toml")
        config = self.agents[agent]
        ref = override or env.get(config.env, "").strip() or config.model
        where = (
            "argument"
            if override
            else (f"${config.env}" if env.get(config.env, "").strip() else "default")
        )
        spec = self.models.get(ref) or self._by_bare_name(ref)
        if spec is None:
            raise ModelConfigError(
                f"{agent}: model {ref!r} ({where}) is not registered. Add it to "
                f"sb_models/models.toml with its context window and capabilities, and eval it."
            )
        problems = self._unmet(config, spec)
        if problems:
            raise ModelConfigError(f"{agent}: model {ref!r} ({where}) " + "; ".join(problems))
        if check_credentials and spec.credential and not env.get(spec.credential, "").strip():
            raise ModelConfigError(
                f"{agent}: model {ref!r} needs ${spec.credential}, which is not set"
            )
        return spec

    def _by_bare_name(self, ref: str) -> ModelSpec | None:
        """`claude-haiku-5-5` -> its registered spec, if exactly one model has that name."""
        matches = [s for s in self.models.values() if s.name == ref]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _unmet(config: AgentConfig, spec: ModelSpec) -> list[str]:
        problems: list[str] = []
        if config.providers and spec.provider not in config.providers:
            reason = f" ({config.providers_reason})" if config.providers_reason else ""
            problems.append(
                f"uses provider {spec.provider!r}; "
                f"this agent supports {list(config.providers)}{reason}"
            )
        needs = config.needs
        if needs.tools and not spec.tools:
            problems.append("has no tool calling, which this agent needs")
        if needs.structured_output and not spec.structured_output:
            problems.append("has no structured output, which this agent needs")
        if spec.context_window < needs.min_context:
            problems.append(
                f"has a {spec.context_window:,}-token context; "
                f"this agent needs {needs.min_context:,}"
            )
        return problems

    def describe(self, model: Any) -> ModelInfo:
        """Provider/name/local for a ref string, a bare name, or a framework model object.

        PydanticAI models expose `.system` (provider) and `.model_name`; ADK's
        `AnthropicLlm` exposes `.model` (a bare name). Unknown models still get a name.
        """
        provider: str | None = None
        if isinstance(model, str):
            head, sep, tail = model.partition(":")
            provider, name = (head, tail) if sep and not head.startswith("http") else (None, model)
        else:
            provider = getattr(model, "system", None)
            name = getattr(model, "model_name", None) or getattr(model, "model", None)
            name = name if isinstance(name, str) else type(model).__name__
        spec = self.models.get(f"{provider}:{name}") if provider else None
        if spec is None and provider is None:
            matches = [s for s in self.models.values() if s.name == name]
            spec = matches[0] if len(matches) == 1 else None
        if spec is not None:
            return ModelInfo(spec.provider, spec.name, spec.local)
        # Local/hosted is known only for registered providers (not e.g. PydanticAI's `test`).
        local = (provider in self.local_providers) if provider in self.providers else None
        return ModelInfo(provider, name, local)


@cache
def registry() -> Registry:
    return Registry.load()


def resolve(agent: str, override: str | None = None, **kwargs: Any) -> ModelSpec:
    return registry().resolve(agent, override, **kwargs)


def describe(model: Any) -> ModelInfo:
    return registry().describe(model)
