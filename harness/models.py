"""Model adapters return actions; only the harness executes tools."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Reply:
    action: dict
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None


def parse_action(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    action = json.loads(text)
    if not isinstance(action, dict) or action.get("type") not in (
        "read",
        "search",
        "patch",
        "test",
        "finish",
        "review",
    ):
        raise ValueError("model must return a supported action object")
    required = {"read": "path", "search": "query", "patch": "patch"}
    key = required.get(action["type"])
    if key and (not isinstance(action.get(key), str) or not action[key]):
        raise ValueError(f"{action['type']} requires nonempty string {key}")
    return action


class FixtureModel:
    """Offline deterministic actions, indexed by persisted role turn."""

    def __init__(self, path: str):
        self.script = json.loads(Path(path).read_text())

    def complete(self, prompt: str, role: str, turn: int, timeout: int) -> Reply:
        actions = self.script.get(role, [])
        if turn >= len(actions):
            raise ValueError(f"fixture exhausted for {role} at turn {turn}")
        return Reply(actions[turn], {"fixture": True}, 0.0)


class ClaudeCLI:
    def __init__(self, binary="claude", model=None, call_budget=0.5):
        self.binary, self.model, self.call_budget = binary, model, call_budget

    def complete(self, prompt: str, role: str, turn: int, timeout: int) -> Reply:
        env = dict(os.environ)
        # Never silently charge an inherited API key when subscription auth
        # was requested. The CLI owns its credential handling.
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            env.pop(key, None)
        cmd = [
            self.binary,
            "-p",
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "",
            "--settings",
            '{"disableAllHooks":true}',
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--max-budget-usd",
            str(self.call_budget),
        ]
        if self.model:
            cmd += ["--model", self.model]
        # No project cwd or config: repository context is explicit input.
        with tempfile.TemporaryDirectory(prefix="bitswarm-model-") as tmp:
            proc = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                cwd=tmp,
                env=env,
                timeout=timeout,
                check=False,
            )
        if proc.returncode:
            detail = (proc.stderr or proc.stdout or "").strip()[:500]
            raise RuntimeError(f"Claude CLI exited {proc.returncode}: {detail}")
        data = json.loads(proc.stdout)
        if data.get("is_error"):
            raise RuntimeError(str(data.get("result", "Claude CLI failed"))[:500])
        return Reply(
            parse_action(data["result"]),
            data.get("usage", {}),
            data.get("total_cost_usd"),
        )


class OpenAICompatible:
    """Explicit HTTP endpoint, JSON actions; no SDK-owned agent loop."""

    def __init__(self, endpoint: str, model: str, key_env="OPENAI_API_KEY"):
        from urllib.parse import urlparse

        parsed = urlparse(endpoint)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http"
            and parsed.hostname in ("localhost", "127.0.0.1", "::1")
        ):
            raise ValueError("endpoint requires HTTPS, except localhost")
        self.endpoint, self.model, self.key_env = endpoint.rstrip("/"), model, key_env

    def complete(self, prompt: str, role: str, turn: int, timeout: int) -> Reply:
        key = os.environ.get(self.key_env, "")
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 4096,
            }
        ).encode()
        request = urllib.request.Request(
            self.endpoint + "/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": "Bearer " + key} if key else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read(2_000_000))
        choice = data["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise RuntimeError(f"incomplete response: {choice.get('finish_reason')}")
        # Tokens are recorded; dollars stay unknown without a pricing policy.
        return Reply(parse_action(choice["message"]["content"]), data.get("usage", {}))


def model_from(config: dict):
    kind = config.get("kind")
    if kind == "fixture":
        return FixtureModel(config["path"])
    if kind == "claude-cli":
        return ClaudeCLI(
            config.get("binary", "claude"),
            config.get("model"),
            config.get("call_budget", 0.5),
        )
    if kind == "openai-compatible":
        return OpenAICompatible(
            config["endpoint"], config["model"], config.get("key_env", "OPENAI_API_KEY")
        )
    raise ValueError(f"unknown adapter: {kind}")
