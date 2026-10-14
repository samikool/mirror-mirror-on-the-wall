# Tooling preparation only. The integration test runs without network access.
FROM ubuntu:24.04
ARG VSCODE_VERSION=latest
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl python3 libasound2t64 libatk-bridge2.0-0 libnss3 \
    libx11-xcb1 libxkbfile1 libsecret-1-0 libgbm1 libgtk-3-0t64 \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir /opt/code && curl -fL --retry 3 \
    "https://update.code.visualstudio.com/${VSCODE_VERSION}/linux-x64/stable" \
    | tar -xz --strip-components=1 -C /opt/code
