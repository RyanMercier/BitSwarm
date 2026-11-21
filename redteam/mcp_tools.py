"""Minimal MCP stdio server exposing the four BitSwarm miner tools.

Lets the public ``claude`` CLI drive a miner run with its built-in
tools disabled (``--tools ""``) and only these tools available, so that
Claude and the open model see the same tool surface and every bash
command runs in the Docker sandbox.

Run by the CLI from an ``--mcp-config`` file:
    python -m redteam.mcp_tools --config <run>/mcp_session.json

The config names the workspace, allowed files, mode, target stubs, the
event log, a state file, and a tool-call budget. Protocol: JSON-RPC 2.0,
one message per line on stdin/stdout. Anything else that would print
goes to stderr so stdout stays protocol-only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr  # keep library prints off the protocol stream

from miner.tools import TOOL_DEFINITIONS  # noqa: E402
from redteam.safe_tools import ToolSession  # noqa: E402


def send(msg: dict) -> None:
    PROTOCOL_OUT.write(json.dumps(msg) + "\n")
    PROTOCOL_OUT.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    cfg = json.load(open(args.config))
    os.environ.setdefault("BITSWARM_SANDBOX", "docker")
    session = ToolSession(cfg["repo_root"], cfg["allowed_files"], cfg["test_files"],
                          cfg["mode"], cfg.get("target_stubs"), cfg["log_path"],
                          max_calls=cfg.get("max_calls"))
    tools = [{"name": t["name"], "description": t["description"],
              "inputSchema": t["input_schema"]} for t in TOOL_DEFINITIONS]

    def write_state():
        tmp = cfg["state_path"] + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"tool_calls": dict(session.counts),
                       "blocked_actions": session.blocked,
                       "files_touched_via_bash": sorted(session.bash_touched),
                       "calls": session.calls}, f)
        os.replace(tmp, cfg["state_path"])

    write_state()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            continue  # notifications (initialized, cancelled) need no reply
        if method == "initialize":
            requested = (msg.get("params") or {}).get("protocolVersion", "2024-11-05")
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "bitswarm", "version": "1.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            name = p.get("name", "")
            arguments = p.get("arguments") or {}
            try:
                result = session.call(name, arguments)
            except Exception as exc:  # never kill the session on a tool bug
                result = {"success": False, "output": f"Tool execution error: {exc}"}
            write_state()
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": result["output"]}],
                "isError": not result["success"]}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
