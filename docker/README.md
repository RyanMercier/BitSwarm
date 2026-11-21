# Docker sandbox

One image, `bitswarm-base:latest`, built from `docker/Dockerfile.base`:

```bash
docker build -f docker/Dockerfile.base -t bitswarm-base:latest .
```

`validator/sandbox.py` wraps two kinds of commands in it:

- every grader gate (stub, integration, holdback, additive, regression), and
- every `bash` tool call a miner agent makes (`miner/tools.py:execute_bash`).

Each container runs with `--network=none`, `--cpus=2`, `--memory=4g`,
`--pids-limit=512`, the caller's uid, `HOME=/tmp`, and only the repo under
test mounted at `/work`. Environment forwarding is allowlist-only
(`PYTHON*`, `LANG`, `LC_ALL`), so no host key material crosses in.

`BITSWARM_SANDBOX=docker` makes a missing daemon or image a hard failure. The
red-team runner always sets it; `auto` would silently fall back to host
execution, and `off` is never used for experiment runs.
