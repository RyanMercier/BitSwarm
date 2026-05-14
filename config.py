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

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Optional base-URL override. Set to e.g. ``http://localhost:8090`` to
# route every Anthropic client call through a local mock server for
# end-to-end tests that don't spend tokens. Empty string = use the
# SDK default (api.anthropic.com).
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "") or None
COORDINATOR_MODEL = "claude-sonnet-4-20250514"
MINER_MODEL = "claude-sonnet-4-20250514"
MAX_MINER_ITERATIONS = 10
MAX_COORDINATOR_RETRIES = 3
SUBTASK_TIMEOUT_SECONDS = 300

# Languages whose parsers are wired up for Phase 1.5 validation.
# Add new entries here as parsers come online (java, csharp, c, cpp, rust).
SUPPORTED_LANGUAGES = ("python", "typescript", "java", "csharp", "c", "cpp", "rust")
