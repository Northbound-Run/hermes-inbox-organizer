"""Minimal OpenRouter LLM client for classification (the cheap local path).

Uses the OpenAI SDK pointed at OpenRouter and reads OPENROUTER_API_KEY from the
environment (provided by the Hermes container). Lazy import so the
package loads without the openai lib. The model defaults to a small, cheap,
instruction-following model and is overridable via INBOX_CLASSIFIER_MODEL.
"""

from __future__ import annotations

import json
import os

DEFAULT_MODEL = os.environ.get("INBOX_CLASSIFIER_MODEL", "google/gemini-2.5-flash-lite")


def classify_json(system: str, user: str, *, model: "str | None" = None) -> dict:
    """Call the model with a JSON-object response format; return the parsed dict."""
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url="https://openrouter.ai/api/v1",
    )
    resp = client.chat.completions.create(
        model=model or DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(resp.choices[0].message.content or "{}")


def summarize(system: str, user: str, *, model: "str | None" = None) -> str:
    """Plain-text completion (mirrors :func:`classify_json` but returns the raw string).

    Used by the sender-profile backfill to distil the owner's voice from their own
    sent prose. Temperature 0 for stable, terse output.
    """
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url="https://openrouter.ai/api/v1",
    )
    resp = client.chat.completions.create(
        model=model or DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
    )
    return (resp.choices[0].message.content or "").strip()
