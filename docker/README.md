# Docker stack

Three image layers, all rebuilt locally:

```
bitswarm-base       all 7 language toolchains + claude CLI + pip deps  (~2.5 GB)
  +- bitswarm-miner       FROM base + miner code                              (+~5 MB)
  +- bitswarm-validator   FROM base + validator code                          (+~10 MB)
```

The base image is heavy. The two slim images sitting on top reuse all
of its layers, so iterating on miner / validator source code only
rebuilds the last few hundred KB.


## What's in the base image

| Toolchain | Languages it serves |
|---|---|
| `python` 3.11 + `pytest` | python |
| `node` 20 + `npm` + `typescript` + `vitest` | typescript / javascript |
| `openjdk-17-jdk-headless` + `maven` | java |
| `dotnet-sdk-8.0` | csharp |
| `build-essential` (gcc, g++, make) | c, cpp |
| `rustc` + `cargo` (Debian packages) | rust |
| `@anthropic-ai/claude-code` (npm global) | the `claude_code` backend |
| `anthropic` + `openai` (pip) | the `sdk` and `openai` backends |


## First-time setup

```bash
# 1. Build the base. Heavy, slow (~5-10 min on a laptop). Run from
#    the project root, NOT the docker/ subdirectory.
cd /path/to/BitSwarm
docker build -f docker/Dockerfile.base -t bitswarm-base:latest .

# 2. Build the slim miner + validator images and start the stack.
cd docker
docker compose --env-file ../.env up --build
```

`.env` lives at the project root. Example for the three backends:

```bash
# A. Anthropic SDK (metered)
ANTHROPIC_API_KEY=sk-ant-...
MINER_BACKEND=sdk
COORDINATOR_BACKEND=sdk

# B. Claude Code subprocess (free if you have a subscription).
#    Also uncomment the ~/.claude mount in docker-compose.yml.
MINER_BACKEND=claude_code
COORDINATOR_BACKEND=claude_code

# C. OpenAI-compatible production miner (mix-and-match with any
#    coordinator backend).
COORDINATOR_BACKEND=sdk
ANTHROPIC_API_KEY=sk-ant-...
MINER_BACKEND=openai
MINER_OPENAI_API_KEY=sk-...
MINER_OPENAI_BASE_URL=https://api.deepseek.com
MINER_OPENAI_MODEL=deepseek-chat
```


## Claude Code auth in containers

The CLI reads `~/.claude/.credentials.json` for OAuth. For the
`claude_code` backend in containers, mount your host's credentials in
read-only and the CLI inside the container will pick them up:

```yaml
# in each service block in docker-compose.yml
volumes:
  - ~/.claude:/root/.claude:ro
```

The compose file ships with these lines commented; uncomment them when
you set `*_BACKEND=claude_code`. (Don't mount them otherwise; the bind
adds nothing for the SDK / openai paths and clutters the logs.)


## Submitting jobs

The validator's HTTP API takes a spec, a `target_repo_path` (resolved
inside the container's filesystem), and a list of miner URLs. The
default `..:/work:ro` mount on the validator gives you the whole repo
tree, so `target_repo_path: /work/demo/target_repo` works
out-of-the-box.

```bash
curl -X POST http://localhost:8080/submit \
  -H 'Content-Type: application/json' \
  -d @- <<'EOF'
{
  "spec": "Build a Wordle clone (see demo/spec_wordle_generic.txt)",
  "target_repo_path": "/work/demo/target_repo",
  "miner_urls": [
    "http://miner-1:8081",
    "http://miner-2:8081",
    "http://miner-3:8081"
  ]
}
EOF
```

Set `COORDINATOR_LANGUAGE=<lang>` in `.env` (or per-call) to pick the
language; the runner inside the validator picks the right profile, and
the multi-language merge-time test runner uses the right command
(`pytest`, `npx vitest`, `mvn`, `dotnet test`, `make`, or
`cargo test`).


## Cache + state

A named `bitswarm-cache` volume holds the decomposition cache
(`~/.bitswarm/`) and persists across `docker compose down`. Wipe with
`docker volume rm docker_bitswarm-cache` if a stale entry is causing
trouble.


## Trimming the image

If you only care about one language in production, copy the base
Dockerfile and delete the install steps for the others. The Node /
JDK / .NET layers are independent so removing any one of them keeps
the rest valid. The miner / validator Dockerfiles don't change.
