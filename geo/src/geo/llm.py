"""Thin wrapper around the Claude API for the toolkit's own reasoning steps
(panel generation, answer judging, answer-card drafting). Engine probes live in measure/engines.py."""
from __future__ import annotations

import os
from typing import TypeVar

import anthropic
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def model() -> str:
    return os.environ.get("GEO_CLAUDE_MODEL", "claude-opus-5-5")


def effort() -> str:
    return os.environ.get("GEO_CLAUDE_EFFORT", "medium")


class Claude:
    def __init__(self, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic()

    def structured(self, system: str, user: str, schema: type[T], max_tokens: int = 16000) -> T:
        resp = self.client.beta.messages.parse(
            model=model(),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": effort()},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"Claude declined: {resp.stop_details}")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("Claude output truncated at max_tokens")
        if resp.parsed_output is None:
            raise RuntimeError("Claude returned no parsable output")
        return resp.parsed_output
