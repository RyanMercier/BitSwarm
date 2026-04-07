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
COORDINATOR_MODEL = "claude-sonnet-4-20250514"
MINER_MODEL = "claude-sonnet-4-20250514"
MAX_MINER_ITERATIONS = 10
MAX_COORDINATOR_RETRIES = 3
SUBTASK_TIMEOUT_SECONDS = 300
