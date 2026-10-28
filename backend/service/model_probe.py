from __future__ import annotations

import json
import os

from langchain_core.messages import HumanMessage

from backend.env_loader import load_env
from backend.llm_factory import build_chat_llm


def main() -> None:
    load_env()
    api_key = (os.environ.get("LLM_API_KEY") or "").strip()
    model = (os.environ.get("LLM_MODEL") or "").strip()
    base_url = (os.environ.get("LLM_BASE_URL") or "").strip()
    if not api_key:
        raise SystemExit("API key is not configured")
    llm = build_chat_llm(
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=0.0,
        streaming=True,
        timeout_seconds=30.0,
    )
    # Exercise the same streaming endpoint used for normal Agent requests.
    parts = []
    for chunk in llm.stream([HumanMessage(content="Reply with exactly: OK")]):
        content = getattr(chunk, "content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(block.get("text", "") for block in content
                         if isinstance(block, dict) and isinstance(block.get("text"), str))
    content = "".join(parts).strip()
    print(json.dumps({"ok": bool(content), "model": model, "response": content[:120]}))


if __name__ == "__main__":
    main()
