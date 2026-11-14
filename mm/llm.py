"""The only place a model is called. Everything else in this project is plain code.

Backend: the Claude Code CLI in headless mode (`claude -p`), which uses the logged-in
Claude subscription, so no API key is needed. The model gets NO tools here: it is a
pure text-in, JSON-out function. Our Python code is the agent loop, not Claude Code.

Swap point: to run on the Anthropic API instead, replace the body of `_run` with one
`client.messages.create(...)` call. Nothing else changes.
"""
import json
import re
import subprocess
import tempfile
import time

from . import store

FAST = "haiku"     # extraction: high volume, simple reading task
SMART = "sonnet"   # answering: multi-step reasoning over evidence

SYSTEM = ("You are a careful function inside a meeting-memory pipeline. "
          "Reply with one JSON value only: no prose, no markdown fences.")


class LLMError(RuntimeError):
    pass


def _run(prompt: str, model: str, timeout: int) -> dict:
    cmd = ["claude", "-p", "--model", model, "--output-format", "json",
           "--system-prompt", SYSTEM,
           "--tools", "",                 # no tools: the model cannot touch files or the web
           "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
           "--setting-sources", "project"]   # ignore the user's personal CLAUDE.md, hooks and plugins
    # run from an empty temp dir so no project files or CLAUDE.md leak into the call
    with tempfile.TemporaryDirectory() as cwd:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise LLMError(f"claude CLI gave no JSON (exit {p.returncode}): {(p.stderr or p.stdout)[:300]}")
    if out.get("is_error"):
        raise LLMError(str(out.get("result"))[:300])
    return out


def _parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        end = max(text.rfind("}"), text.rfind("]"))
        if start < 0 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def call(prompt: str, model: str = FAST, stage: str = "", timeout: int = 600, retries: int = 2):
    """Send a prompt, get parsed JSON back. Logs tokens, cost and seconds for the cost report."""
    last = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            out = _run(prompt, model, timeout)
            data = _parse_json(out.get("result", ""))
            _log(stage, model, out, time.time() - t0, 1)
            return data
        except (LLMError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            last = e
            _log(stage, model, {}, time.time() - t0, 0)
            if "Not logged in" in str(e):
                raise LLMError("Claude CLI is not logged in. Run `claude auth login` once, then retry.")
            if "limit" in str(e).lower():      # usage cap: retrying only burns time. Ingest is resumable.
                raise LLMError(f"usage limit reached, rerun later to resume: {e}")
            time.sleep(2 * (attempt + 1))
    raise LLMError(f"{stage}: failed after {retries + 1} tries: {last}")


def _log(stage, model, out, seconds, ok):
    u = out.get("usage", {}) or {}
    tokens_in = sum(u.get(k, 0) or 0 for k in
                    ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO llm_log(stage, model, input_tokens, output_tokens, cost_usd, seconds, ok) VALUES(?,?,?,?,?,?,?)",
            (stage, model, tokens_in, u.get("output_tokens", 0) or 0, out.get("total_cost_usd", 0) or 0, seconds, ok))
        conn.commit()
    finally:
        conn.close()
