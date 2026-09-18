# Sandbox image for verifying BitSwarm's own pull requests.
# The regression gate runs tests/ inside this image, so it needs git and the
# legacy validator's dependencies (minus bittensor, which the tests do not use).
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN grep -vE '^\s*(#|$|bittensor)' /tmp/requirements.txt | pip install --no-cache-dir -r /dev/stdin \
    && pip install --no-cache-dir pytest==8.3.5
ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    GIT_AUTHOR_NAME=bitswarm GIT_AUTHOR_EMAIL=bitswarm@local \
    GIT_COMMITTER_NAME=bitswarm GIT_COMMITTER_EMAIL=bitswarm@local
USER 65534:65534
