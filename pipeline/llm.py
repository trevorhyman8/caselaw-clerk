"""LLM backend abstraction — two implementations behind one interface.

`claude_p` (default): shells out to `claude -p` (Claude Code headless), $0 on
a Claude Max plan, no API key. Used for ALL local dev/testing in this repo —
matches the same pattern as ~/dev/x-saves-puller/llm.py (the reference impl).

`anthropic_api`: the metered Anthropic SDK, needed for the RAILWAY-DEPLOYED
service specifically, because a deployed container can't hold an interactive
`claude login` session. This is the "rare case Claude Code genuinely can't
cover" carve-out — flagged explicitly here, not silently defaulted to.
Requires ANTHROPIC_API_KEY (set in Railway env vars per SETUP.md, never in
this repo).

Select via pipeline.settings.settings.llm_backend.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

from pipeline.settings import settings

CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def _complete_claude_p(prompt: str, model: str | None = None, timeout: int = 900, retries: int = 2) -> str:
    cmd = [CLAUDE_BIN, "-p"] + (["--model", model] if model else [])
    last = ""
    for attempt in range(retries + 1):
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            last = "timeout"
        else:
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
            last = f"rc={proc.returncode} empty={not proc.stdout.strip()} stderr={proc.stderr[:300]}"
        if attempt < retries:
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"claude -p failed after {retries + 1} tries: {last}")


def _complete_anthropic_api(prompt: str, model: str | None = None, timeout: int = 900, system: str | None = None) -> str:
    import anthropic

    if not settings.anthropic_api_key:
        raise RuntimeError(
            "llm_backend=anthropic_api but ANTHROPIC_API_KEY is not set. This backend "
            "is only for the deployed Railway service — see SETUP.md."
        )
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=model or "claude-sonnet-5",
        max_tokens=4096,
        system=system or "",
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()


def complete(prompt: str, model: str | None = None, timeout: int = 900, system: str | None = None) -> str:
    if settings.llm_backend == "anthropic_api":
        return _complete_anthropic_api(prompt, model, timeout, system)
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    return _complete_claude_p(full_prompt, model, timeout)


def _extract_json(raw: str):
    s = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch in "[{":
            try:
                val, _ = dec.raw_decode(s, i)
                return val
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Could not parse JSON from model output:\n{raw[:800]}")


def complete_json(prompt: str, model: str | None = None, timeout: int = 900, system: str | None = None):
    instruction = "\n\nReturn ONLY valid JSON. No markdown fences, no commentary before or after."
    try:
        return _extract_json(complete(prompt + instruction, model, timeout, system))
    except (ValueError, RuntimeError):
        pass
    strict = prompt + "\n\nIMPORTANT: Output must be a single valid JSON value and nothing else."
    try:
        return _extract_json(complete(strict, model, timeout, system))
    except (ValueError, RuntimeError) as e:
        raise ValueError(f"complete_json: both attempts failed: {e}") from e
