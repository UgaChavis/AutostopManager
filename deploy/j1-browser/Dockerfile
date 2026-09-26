FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        python3 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 j1browser \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /nonexistent --shell /usr/sbin/nologin j1browser

WORKDIR /app
RUN mkdir -p /app/autostop_manager
COPY autostop_manager/__init__.py autostop_manager/j1_sources.py autostop_manager/j1_fetch.py autostop_manager/j1_browser.py autostop_manager/j1_browser_renderer.py autostop_manager/j1_browser_proxy.py /app/autostop_manager/

ARG AUTOSTOP_J1_BROWSER_IMAGE_REVISION
LABEL org.opencontainers.image.revision="${AUTOSTOP_J1_BROWSER_IMAGE_REVISION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONSAFEPATH=1 \
    PYTHONPATH=/app \
    AUTOSTOP_J1_CHROMIUM_BINARY=/usr/bin/chromium

USER 10001:10001
ENTRYPOINT ["/usr/bin/python3"]
