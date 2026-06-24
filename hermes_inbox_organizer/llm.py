"""Minimal OpenAI-compatible LLM client for classification (the cheap local path).

Defaults to OpenRouter but works with **any OpenAI-compatible endpoint**: set
``INBOX_CLASSIFIER_BASE_URL`` and ``INBOX_CLASSIFIER_API_KEY`` (the key falls back
to ``OPENROUTER_API_KEY`` for back-compat). The model defaults to a small, cheap,
instruction-following model and is overridable via ``INBOX_CLASSIFIER_MODEL``.
All three resolve through :mod:`.config`. The ``openai`` SDK is imported lazily so
the package loads without it.
"""

from __future__ import annotations

import json

from .config import get_config


def _client():
    """Build an OpenAI client for the configured (OpenAI-compatible) endpoint."""
    from openai import OpenAI

    cfg = get_config()
    if not cfg.classifier_api_key:
        raise RuntimeError(
            "no classifier API key — set INBOX_CLASSIFIER_API_KEY (or OPENROUTER_API_KEY)"
        )
    return OpenAI(api_key=cfg.classifier_api_key, base_url=cfg.classifier_base_url)


def classify_json(system: str, user: str, *, model: str | None = None) -> dict:
    """Call the model with a JSON-object response format; return the parsed dict."""
    resp = _client().chat.completions.create(
        model=model or get_config().classifier_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(resp.choices[0].message.content or "{}")


def summarize(system: str, user: str, *, model: str | None = None) -> str:
    """Plain-text completion (mirrors :func:`classify_json` but returns the raw string).

    Used by the sender-profile backfill to distil the owner's voice from their own
    sent prose. Temperature 0 for stable, terse output.
    """
    resp = _client().chat.completions.create(
        model=model or get_config().classifier_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
    )
    return (resp.choices[0].message.content or "").strip()
