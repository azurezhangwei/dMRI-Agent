"""Read official provider catalogs without changing credentials or model choices."""
from __future__ import annotations

from typing import Any

import httpx

from backend.model_config import ModelConfigError, validate_model_name


CATALOG_URLS = {
    "deepseek": "https://api.deepseek.com/models",
    "openai": "https://api.openai.com/v1/models",
    "anthropic": "https://api.anthropic.com/v1/models",
    "dashscope": "https://dashscope.aliyuncs.com/api/v1/models",
}


class ModelCatalogError(RuntimeError):
    def __init__(self, message: str, *, code: str = "model_catalog_failed", status: int = 502):
        super().__init__(message)
        self.code = code
        self.status = status


async def fetch_model_catalog(provider: str, api_key: str) -> list[dict[str, str]]:
    """Fetch all pages from a fixed official host; never forward a key on redirects."""
    if provider not in CATALOG_URLS:
        raise ModelCatalogError("Model lists are available for the four official providers only.", code="model_catalog_unsupported", status=400)
    headers = {"Authorization": f"Bearer {api_key}"}
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    params: dict[str, Any] = {}
    if provider == "anthropic":
        params["limit"] = 1000
    elif provider == "dashscope":
        params = {"page_no": 1, "page_size": 100, "providers": "qwen"}
    models: dict[str, dict[str, str]] = {}
    cursors: set[str] = set()
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        for _ in range(25):
            try:
                response = await client.get(CATALOG_URLS[provider], headers=headers, params=params)
            except httpx.TimeoutException:
                raise ModelCatalogError("Fetching models timed out. Try again or enter a model name manually.", code="model_catalog_timeout", status=504) from None
            except httpx.HTTPError:
                raise ModelCatalogError("Could not reach the provider. Check the network or enter a model name manually.", code="model_catalog_network_error") from None
            if response.status_code in {401, 403}:
                raise ModelCatalogError("The provider rejected the model-list request. Check the API key and its permissions.", code="model_catalog_access_denied", status=400)
            if response.status_code == 429:
                raise ModelCatalogError("The provider rate limit was reached. Try again later.", code="model_catalog_rate_limit", status=429)
            if response.status_code in {404, 405} and provider == "dashscope":
                raise ModelCatalogError("Qwen's Beijing gateway did not provide a model list for this key. Enter the model name manually.", code="model_catalog_unavailable")
            if response.status_code != 200:
                raise ModelCatalogError("The provider could not return a model list. Try again or enter a model name manually.")
            try:
                if len(response.content) > 2 * 1024 * 1024:
                    raise ValueError("oversized catalog")
                payload = response.json()
                output = payload.get("output", {}) if provider == "dashscope" else payload
                entries = output.get("models" if provider == "dashscope" else "data")
                if not isinstance(entries, list):
                    raise ValueError("missing models")
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    raw_id = entry.get("model" if provider == "dashscope" else "id")
                    if not isinstance(raw_id, str):
                        continue
                    try:
                        model_id = validate_model_name(raw_id)
                    except ModelConfigError:
                        continue
                    # Only return model labels, never the upstream response or diagnostics.
                    if api_key in model_id:
                        continue
                    name = entry.get("display_name") or entry.get("name") or model_id
                    name = str(name).replace(api_key, "[redacted]")[:256]
                    models[model_id] = {"id": model_id, "name": name}
                if provider == "anthropic" and payload.get("has_more"):
                    cursor = payload.get("last_id")
                    if not isinstance(cursor, str) or not cursor or cursor in cursors:
                        raise ValueError("invalid pagination")
                    cursors.add(cursor)
                    params["after_id"] = cursor
                elif provider == "dashscope" and int(output.get("total", 0)) > params["page_no"] * params["page_size"]:
                    params["page_no"] += 1
                else:
                    break
            except (ValueError, TypeError, AttributeError):
                raise ModelCatalogError("The provider returned an invalid model list. Enter a model name manually or try again later.", code="model_catalog_invalid_response") from None
        else:
            raise ModelCatalogError("The provider's model list could not be fully retrieved. Enter a model name manually.", code="model_catalog_incomplete")
    if not models:
        raise ModelCatalogError("No models were returned for this key. You can still enter a model name manually.", code="model_catalog_empty")
    return sorted(models.values(), key=lambda model: model["id"].casefold())
