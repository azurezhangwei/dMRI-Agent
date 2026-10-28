from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, MutableMapping
from urllib.parse import urlparse
from backend.platform import config_home as platform_config_home, secure_path

from backend.project_paths import DEFAULT_MODEL_CONFIG, resolve_project_path


SUPPORTED_API_STYLES = {"openai_compatible", "anthropic_native"}
SUPPORTED_CACHE_STRATEGIES = {
    "off",
    "provider_automatic",
    "session",
    "anthropic_auto",
    "anthropic_explicit",
}

DEFAULT_CONTEXT_WINDOW_TOKENS = 256 * 1024

PROFILE_UPDATE_FIELDS = {
    "label",
    "provider",
    "api_style",
    "base_url",
    "model",
    "api_key_env",
    "temperature",
    "max_tokens",
    "context_window_tokens",
    "reasoning_effort",
    "anthropic_thinking",
    "cache_strategy",
    "cache_ttl",
    "use_responses_api",
    "use_previous_response_id",
    "extra_body",
    "default_headers",
}


class ModelConfigError(ValueError):
    pass


def validate_model_name(value: str) -> str:
    model = value.strip()
    invalid_character = any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in model
    )
    if not model or len(model) > 256 or invalid_character:
        raise ModelConfigError(
            "Enter a model name without spaces or control characters (up to 256 characters)"
        )
    return model


def default_secrets_path() -> Path:
    configured = (os.environ.get("DMRI_SECRETS_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (platform_config_home() / "secrets.json").resolve()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_mapping(value: Any, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ModelConfigError(f"{field_name} must be an object")
    return {str(key): str(item) for key, item in value.items()}


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    label: str
    provider: str
    api_style: str
    base_url: str
    model: str
    api_key_env: str
    temperature: float | None = None
    max_tokens: int | None = None
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    reasoning_effort: str | None = None
    anthropic_thinking: str | None = None
    cache_strategy: str = "off"
    cache_ttl: str | None = None
    use_responses_api: bool = False
    use_previous_response_id: bool = False
    extra_body: dict[str, Any] = field(default_factory=dict)
    default_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, profile_id: str, value: Mapping[str, Any]) -> "ModelProfile":
        required = ("label", "provider", "api_style", "base_url", "model", "api_key_env")
        missing = [name for name in required if not _optional_string(value.get(name))]
        if missing:
            raise ModelConfigError(
                f"profile {profile_id!r} is missing required fields: {', '.join(missing)}"
            )
        temperature_value = value.get("temperature")
        temperature = None if temperature_value is None else float(temperature_value)
        max_tokens_value = value.get("max_tokens")
        max_tokens = None if max_tokens_value is None else int(max_tokens_value)
        context_window_value = value.get(
            "context_window_tokens",
            DEFAULT_CONTEXT_WINDOW_TOKENS,
        )
        context_window_tokens = int(
            DEFAULT_CONTEXT_WINDOW_TOKENS
            if context_window_value is None
            else context_window_value
        )
        extra_body = value.get("extra_body") or {}
        if not isinstance(extra_body, Mapping):
            raise ModelConfigError(f"profile {profile_id!r} extra_body must be an object")
        profile = cls(
            profile_id=profile_id,
            label=str(value["label"]).strip(),
            provider=str(value["provider"]).strip().lower(),
            api_style=str(value["api_style"]).strip().lower(),
            base_url=str(value["base_url"]).strip().rstrip("/"),
            model=str(value["model"]).strip(),
            api_key_env=str(value["api_key_env"]).strip(),
            temperature=temperature,
            max_tokens=max_tokens,
            context_window_tokens=context_window_tokens,
            reasoning_effort=_optional_string(value.get("reasoning_effort")),
            anthropic_thinking=_optional_string(value.get("anthropic_thinking")),
            cache_strategy=str(value.get("cache_strategy") or "off").strip().lower(),
            cache_ttl=_optional_string(value.get("cache_ttl")),
            use_responses_api=bool(value.get("use_responses_api", False)),
            use_previous_response_id=bool(value.get("use_previous_response_id", False)),
            extra_body=dict(extra_body),
            default_headers=_string_mapping(value.get("default_headers"), "default_headers"),
        )
        profile.validate()
        return profile

    def validate(self) -> None:
        if self.api_style not in SUPPORTED_API_STYLES:
            raise ModelConfigError(
                f"profile {self.profile_id!r} has unsupported api_style {self.api_style!r}"
            )
        if self.cache_strategy not in SUPPORTED_CACHE_STRATEGIES:
            raise ModelConfigError(
                f"profile {self.profile_id!r} has unsupported cache_strategy "
                f"{self.cache_strategy!r}"
            )
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ModelConfigError(
                f"profile {self.profile_id!r} has invalid base_url {self.base_url!r}"
            )
        if not self.api_key_env.replace("_", "").isalnum():
            raise ModelConfigError(
                f"profile {self.profile_id!r} has invalid api_key_env {self.api_key_env!r}"
            )
        if self.context_window_tokens <= 0:
            raise ModelConfigError(
                f"profile {self.profile_id!r} context_window_tokens must be positive"
            )

    def to_environment(self, api_key: str | None) -> dict[str, str]:
        if self.cache_strategy == "anthropic_auto":
            cache_mode = "anthropic_auto"
        elif self.cache_strategy == "anthropic_explicit":
            cache_mode = "explicit_ephemeral"
        else:
            cache_mode = "off"
        values = {
            "DMRI_MODEL_PROFILE": self.profile_id,
            "LLM_MODEL": self.model,
            "LLM_BASE_URL": self.base_url,
            "LLM_API_STYLE": self.api_style,
            "LLM_TEMPERATURE": "default" if self.temperature is None else str(self.temperature),
            "LLM_MAX_TOKENS": "" if self.max_tokens is None else str(self.max_tokens),
            "LLM_CONTEXT_WINDOW_TOKENS": str(self.context_window_tokens),
            "LLM_REASONING_EFFORT": self.reasoning_effort or "",
            "LLM_EXTRA_BODY_JSON": (
                json.dumps(self.extra_body, ensure_ascii=True, separators=(",", ":"))
                if self.extra_body
                else ""
            ),
            "LLM_DEFAULT_HEADERS_JSON": (
                json.dumps(self.default_headers, ensure_ascii=True, separators=(",", ":"))
                if self.default_headers
                else ""
            ),
            "LLM_USE_RESPONSES_API": "1" if self.use_responses_api else "0",
            "LLM_USE_PREVIOUS_RESPONSE_ID": "1" if self.use_previous_response_id else "0",
            "LLM_CACHE_MODE": cache_mode,
            "LLM_ANTHROPIC_CACHE_MODE": cache_mode,
            "LLM_ANTHROPIC_CACHE_TTL": self.cache_ttl or "",
            "LLM_ANTHROPIC_THINKING": self.anthropic_thinking or "",
        }
        if api_key:
            values["LLM_API_KEY"] = api_key
            # Keep provider SDK fallbacks consistent with the credential that
            # the Agent selected.  This also prevents a stale inherited
            # provider variable from defeating a key saved in the plugin.
            values[self.api_key_env] = api_key
        return values

    def public_dict(self, configured: bool) -> dict[str, Any]:
        return {
            "id": self.profile_id,
            "label": self.label,
            "provider": self.provider,
            "api_style": self.api_style,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "context_window_tokens": self.context_window_tokens,
            "reasoning_effort": self.reasoning_effort,
            "anthropic_thinking": self.anthropic_thinking,
            "cache_strategy": self.cache_strategy,
            "cache_ttl": self.cache_ttl,
            "use_responses_api": self.use_responses_api,
            "use_previous_response_id": self.use_previous_response_id,
            "extra_body": self.extra_body,
            "default_headers": self.default_headers,
            "api_key_configured": configured,
        }


@dataclass(frozen=True)
class ModelConfig:
    path: Path
    version: int
    active_profile: str
    profiles: dict[str, ModelProfile]
    overrides_path: Path | None = None
    default_models: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None, *, overrides_path: Path | None = None) -> "ModelConfig":
        configured_path = path or resolve_project_path(
            os.environ.get("DMRI_MODEL_CONFIG"), DEFAULT_MODEL_CONFIG
        )
        if not configured_path.is_file():
            raise ModelConfigError(f"model config does not exist: {configured_path}")
        try:
            payload = json.loads(configured_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ModelConfigError(f"invalid model config JSON: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ModelConfigError("model config root must be an object")
        profiles_payload = payload.get("profiles")
        if not isinstance(profiles_payload, Mapping) or not profiles_payload:
            raise ModelConfigError("model config must contain a non-empty profiles object")
        profiles = {
            str(profile_id): ModelProfile.from_mapping(str(profile_id), value)
            for profile_id, value in profiles_payload.items()
            if isinstance(value, Mapping)
        }
        if len(profiles) != len(profiles_payload):
            raise ModelConfigError("every profile value must be an object")
        default_models = {profile_id: profile.model for profile_id, profile in profiles.items()}
        if overrides_path is None and configured_path.resolve() == DEFAULT_MODEL_CONFIG.resolve():
            configured_overrides = (os.environ.get("DMRI_MODEL_OVERRIDES_FILE") or "").strip()
            overrides_path = (
                Path(configured_overrides).expanduser().resolve()
                if configured_overrides else default_secrets_path().with_name("model-overrides.json")
            )
        if overrides_path is not None and overrides_path.is_file():
            saved = cls._load_overrides(overrides_path)
            for profile_id, choice in saved.items():
                profile = profiles.get(profile_id)
                if profile is None or not isinstance(choice, Mapping):
                    continue
                # A saved model name cannot change a provider or its endpoint.
                if choice.get("provider") != profile.provider or choice.get("base_url") != profile.base_url:
                    continue
                profiles[profile_id] = replace(profile, model=validate_model_name(str(choice.get("model") or "")))
        active = str(payload.get("active_profile") or "").strip()
        if active not in profiles:
            raise ModelConfigError(f"active_profile {active!r} does not exist")
        return cls(
            path=configured_path.resolve(),
            version=int(payload.get("version") or 1),
            active_profile=active,
            profiles=profiles,
            overrides_path=overrides_path,
            default_models=default_models,
        )

    @staticmethod
    def _load_overrides(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ModelConfigError("Saved model choices could not be read") from exc
        profiles = payload.get("profiles") if isinstance(payload, Mapping) else None
        if not isinstance(profiles, dict):
            raise ModelConfigError("Saved model choices must contain a profiles object")
        return profiles

    def save_model_choice(self, profile_id: str, model: str) -> None:
        profile = self.select(profile_id)
        model = validate_model_name(model)
        if self.overrides_path is None:
            self.update_profile(profile_id, {"model": model})
            return
        choices = self._load_overrides(self.overrides_path) if self.overrides_path.is_file() else {}
        choices[profile_id] = {"model": model, "provider": profile.provider, "base_url": profile.base_url}
        _atomic_write_json(self.overrides_path, {"version": 1, "profiles": choices}, mode=0o600)

    def select(self, profile_id: str | None = None) -> ModelProfile:
        selected = (profile_id or self.active_profile).strip()
        try:
            return self.profiles[selected]
        except KeyError as exc:
            raise ModelConfigError(f"unknown model profile: {selected}") from exc

    def set_active_profile(self, profile_id: str) -> None:
        self.select(profile_id)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["active_profile"] = profile_id
        _atomic_write_json(self.path, payload, mode=None)

    def update_profile(self, profile_id: str, updates: Mapping[str, Any]) -> ModelProfile:
        self.select(profile_id)
        updates = dict(updates)
        if "model" in updates:
            updates["model"] = validate_model_name(str(updates["model"] or ""))
        unknown = set(updates) - PROFILE_UPDATE_FIELDS
        if unknown:
            raise ModelConfigError(
                f"unsupported profile fields: {', '.join(sorted(unknown))}"
            )
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        profile_payload = payload["profiles"][profile_id]
        profile_payload.update(dict(updates))
        ModelProfile.from_mapping(profile_id, profile_payload)
        _atomic_write_json(self.path, payload, mode=None)
        if "model" in updates and self.overrides_path is not None:
            self.save_model_choice(profile_id, str(updates["model"]))
        return ModelConfig.load(self.path, overrides_path=self.overrides_path).select(profile_id)


class SecretStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or default_secrets_path()).expanduser().resolve()

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ModelConfigError(f"invalid secrets JSON: {exc}") from exc
        values = payload.get("api_keys", {}) if isinstance(payload, Mapping) else {}
        if not isinstance(values, Mapping):
            raise ModelConfigError("secrets api_keys must be an object")
        return {str(key): str(value) for key, value in values.items() if str(value).strip()}

    def get(self, profile_id: str) -> str | None:
        return self.load().get(profile_id)

    def set(self, profile_id: str, api_key: str) -> None:
        key = api_key.strip()
        if not key:
            raise ModelConfigError("api_key cannot be empty")
        values = self.load()
        values[profile_id] = key
        _atomic_write_json(self.path, {"version": 1, "api_keys": values}, mode=0o600)

    def delete(self, profile_id: str) -> None:
        values = self.load()
        if profile_id not in values:
            return
        del values[profile_id]
        _atomic_write_json(self.path, {"version": 1, "api_keys": values}, mode=0o600)


def _atomic_write_json(path: Path, payload: Any, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        if mode is not None:
            secure_path(temp_path, mode)
        temp_path.replace(path)
        if mode is not None:
            secure_path(path, mode)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def resolve_profile_api_key(
    profile: ModelProfile,
    environ: Mapping[str, str] | None = None,
    secrets: SecretStore | None = None,
) -> str | None:
    values = os.environ if environ is None else environ
    # A credential explicitly saved through the application is authoritative.
    # Provider variables remain a compatibility fallback for users who have
    # not configured the plugin yet, but must not silently replace a newer key.
    secret_key = (secrets or SecretStore()).get(profile.profile_id)
    if secret_key:
        return secret_key
    provider_key = (values.get(profile.api_key_env) or "").strip()
    if provider_key:
        return provider_key
    legacy_key = (values.get("LLM_API_KEY") or "").strip()
    return legacy_key or None


def profile_environment(
    profile_id: str | None = None,
    *,
    config: ModelConfig | None = None,
    environ: Mapping[str, str] | None = None,
    secrets: SecretStore | None = None,
) -> dict[str, str]:
    loaded = config or ModelConfig.load()
    profile = loaded.select(profile_id or os.environ.get("DMRI_MODEL_PROFILE"))
    api_key = resolve_profile_api_key(profile, environ=environ, secrets=secrets)
    values = profile.to_environment(api_key)
    source = environ if environ is not None else os.environ
    explicit_context_window = (source.get("LLM_CONTEXT_WINDOW_TOKENS") or "").strip()
    if explicit_context_window:
        values["LLM_CONTEXT_WINDOW_TOKENS"] = explicit_context_window
    return values


def apply_active_profile_env(
    environ: MutableMapping[str, str] | None = None,
    *,
    overwrite: bool = False,
) -> ModelProfile:
    target = environ if environ is not None else os.environ
    config = ModelConfig.load()
    profile_id = (target.get("DMRI_MODEL_PROFILE") or config.active_profile).strip()
    profile = config.select(profile_id)
    values = profile_environment(profile_id, config=config, environ=target)
    for name, value in values.items():
        if overwrite or not (target.get(name) or "").strip():
            target[name] = value
    return profile
