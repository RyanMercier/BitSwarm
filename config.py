import os

# Load .env file if present (so you don't need to export the key every session)
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# --- Anthropic (default coordinator + SDK miner backend) -----------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Optional base-URL override. Set to e.g. ``http://localhost:8090`` to
# route every Anthropic client call through a local mock server for
# end-to-end tests that don't spend tokens. Empty string = use the
# SDK default (api.anthropic.com).
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "") or None
COORDINATOR_MODEL = os.environ.get("COORDINATOR_MODEL", "claude-sonnet-4-20250514")
MINER_MODEL = os.environ.get("MINER_MODEL", "claude-sonnet-4-20250514")
MAX_MINER_ITERATIONS = int(os.environ.get("MAX_MINER_ITERATIONS", "10"))
MAX_COORDINATOR_RETRIES = int(os.environ.get("MAX_COORDINATOR_RETRIES", "3"))
SUBTASK_TIMEOUT_SECONDS = int(os.environ.get("SUBTASK_TIMEOUT_SECONDS", "300"))

# Languages whose parsers are wired up for Phase 1.5 validation.
# Add new entries here as parsers come online (java, csharp, c, cpp, rust).
SUPPORTED_LANGUAGES = ("python", "typescript", "java", "csharp", "c", "cpp", "rust")

# --- Backend selection for the agent loop --------------------------------
# Three options:
#   "sdk"          - Anthropic Python SDK with metered API tokens. The
#                    default. Requires ANTHROPIC_API_KEY.
#   "claude_code"  - Shell out to the ``claude`` CLI in print mode.
#                    Uses the user's Claude subscription auth (Max /
#                    Pro / Team), no per-token API spend. Requires
#                    ``@anthropic-ai/claude-code`` installed in the
#                    miner / validator runtime. Best for local
#                    development / smoke tests when you don't want to
#                    spend API tokens.
#   "openai"       - OpenAI-compatible Chat Completions endpoint.
#                    Works with OpenAI, DeepSeek, Together, OpenRouter,
#                    Groq, Fireworks, vLLM, llama.cpp, Ollama, etc.
#                    For miners running in production: pick whichever
#                    provider+model gives them the best score-per-dollar.
#
# Miner and coordinator have independent switches so you can mix and
# match (e.g. SDK coordinator + claude_code miner, or claude_code
# coordinator + openai miner with a cheap local model).
MINER_BACKEND = os.environ.get("MINER_BACKEND", "sdk").strip().lower()
COORDINATOR_BACKEND = os.environ.get("COORDINATOR_BACKEND", "sdk").strip().lower()

# --- OpenAI-compatible miner config (only used when MINER_BACKEND=openai) -
# A miner running in production picks their provider by setting these.
# We accept the standard OPENAI_* env vars too so a plug-and-play
# OpenAI-shaped client (or a local llama server) works without further
# config:
#
#   MINER_OPENAI_API_KEY  > OPENAI_API_KEY                      (default: empty)
#   MINER_OPENAI_BASE_URL > OPENAI_BASE_URL                     (default: OpenAI)
#   MINER_OPENAI_MODEL    > OPENAI_MODEL > "gpt-4o-mini"        (default model)
#
# Examples:
#   DeepSeek:      MINER_OPENAI_BASE_URL=https://api.deepseek.com
#                  MINER_OPENAI_MODEL=deepseek-chat
#   OpenRouter:    MINER_OPENAI_BASE_URL=https://openrouter.ai/api/v1
#                  MINER_OPENAI_MODEL=meta-llama/llama-3.3-70b-instruct
#   Local vLLM:    MINER_OPENAI_BASE_URL=http://localhost:8000/v1
#                  MINER_OPENAI_API_KEY=sk-local
#                  MINER_OPENAI_MODEL=Qwen/Qwen2.5-Coder-32B-Instruct
#   Ollama:        MINER_OPENAI_BASE_URL=http://localhost:11434/v1
#                  MINER_OPENAI_API_KEY=sk-local
#                  MINER_OPENAI_MODEL=qwen2.5-coder:32b
OPENAI_API_KEY = (
    os.environ.get("MINER_OPENAI_API_KEY", "")
    or os.environ.get("OPENAI_API_KEY", "")
)
OPENAI_BASE_URL = (
    os.environ.get("MINER_OPENAI_BASE_URL", "")
    or os.environ.get("OPENAI_BASE_URL", "")
) or None
OPENAI_MODEL = (
    os.environ.get("MINER_OPENAI_MODEL", "")
    or os.environ.get("OPENAI_MODEL", "")
    or "gpt-4o-mini"
)
