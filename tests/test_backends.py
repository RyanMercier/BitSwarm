"""
Tests for multi-LLM backend selection on the miner.

Verifies:
  - ``MINER_BACKEND`` routes ``miner.server._select_backend`` to the
    right ``execute_subtask`` callable for each supported value.
  - Unknown values raise a clear error.
  - The Anthropic -> OpenAI tool-schema translation in
    ``miner.agent_openai._to_openai_tools`` preserves names,
    descriptions, and JSON schema.

These tests never actually hit a remote LLM. They exercise the wiring
that lets a miner operator pick "sdk" / "claude_code" / "openai" via
env var.
"""
import importlib
import os
import sys

import pytest


def _reload_backend(monkeypatch, backend_value):
    """Set ``MINER_BACKEND`` and reload the modules that read it at
    import time. Returns the freshly imported ``miner.server`` module."""
    monkeypatch.setenv("MINER_BACKEND", backend_value)
    # config and miner.server cache the env var at import; nuke both.
    for mod in ("miner.server", "config"):
        sys.modules.pop(mod, None)
    return importlib.import_module("miner.server")


def test_backend_sdk_default(monkeypatch):
    server = _reload_backend(monkeypatch, "")
    # SDK path resolves to miner.agent.execute_subtask
    import miner.agent as anthropic_agent
    assert server.execute_subtask is anthropic_agent.execute_subtask


def test_backend_sdk_explicit(monkeypatch):
    server = _reload_backend(monkeypatch, "sdk")
    import miner.agent as anthropic_agent
    assert server.execute_subtask is anthropic_agent.execute_subtask


def test_backend_claude_code(monkeypatch):
    server = _reload_backend(monkeypatch, "claude_code")
    import miner.agent_cc as cc_agent
    assert server.execute_subtask is cc_agent.execute_subtask


def test_backend_openai(monkeypatch):
    # No real key needed for selection: _make_client() only runs when
    # execute_subtask is invoked. Selection just checks the import.
    monkeypatch.setenv("MINER_OPENAI_API_KEY", "sk-test-not-used")
    server = _reload_backend(monkeypatch, "openai")
    import miner.agent_openai as openai_agent
    assert server.execute_subtask is openai_agent.execute_subtask


def test_backend_unknown_raises(monkeypatch):
    with pytest.raises(RuntimeError, match="Unknown MINER_BACKEND"):
        _reload_backend(monkeypatch, "not_a_real_backend")


def test_openai_tool_translation_shape():
    """Anthropic-style TOOL_DEFINITIONS round-trip into OpenAI shape
    with names + descriptions intact and ``input_schema`` becoming
    ``function.parameters``."""
    from miner.agent_openai import _to_openai_tools
    from miner.tools import TOOL_DEFINITIONS

    out = _to_openai_tools(TOOL_DEFINITIONS)
    assert len(out) == len(TOOL_DEFINITIONS)

    for src, dst in zip(TOOL_DEFINITIONS, out):
        assert dst["type"] == "function"
        fn = dst["function"]
        assert fn["name"] == src["name"]
        assert fn["description"] == src["description"]
        # The exact schema is forwarded verbatim: properties + required
        # all preserved so any provider that validates schemas accepts it.
        assert fn["parameters"] == src["input_schema"]


def test_openai_tool_translation_covers_all_tools():
    """Every tool the miner uses (file_read, file_write, bash,
    list_files) makes it across the translation."""
    from miner.agent_openai import _to_openai_tools
    from miner.tools import TOOL_DEFINITIONS

    names = {t["function"]["name"] for t in _to_openai_tools(TOOL_DEFINITIONS)}
    assert names == {"file_read", "file_write", "bash", "list_files"}


def test_openai_make_client_requires_api_key(monkeypatch):
    """Selection succeeds without a key (so the server can start) but
    the first execute_subtask call must surface a clear error when no
    key is configured."""
    monkeypatch.delenv("MINER_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for mod in ("miner.agent_openai", "config"):
        sys.modules.pop(mod, None)

    import miner.agent_openai as agent_openai
    with pytest.raises(RuntimeError, match="MINER_OPENAI_API_KEY"):
        agent_openai._make_client()
