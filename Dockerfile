# Base image is pinned to an immutable digest. To refresh (do it with a dependency bump, not
# on a schedule nobody runs), read the digest and paste it below, then rebuild and re-run the
# CI docker job:
#   docker buildx imagetools inspect python:3.13-slim --format '{{json .Manifest.Digest}}'
FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CLREVIEW_DB=/data/clreview.db \
    CLREVIEW_HOST=0.0.0.0 \
    CLREVIEW_PORT=8765

WORKDIR /app

RUN groupadd --system clreview \
 && useradd --system --gid clreview --home-dir /app clreview \
 && mkdir -p /data && chown clreview:clreview /data

COPY pyproject.toml README.md LICENSE ./
COPY clreview ./clreview
RUN pip install --no-cache-dir .

USER clreview
VOLUME ["/data"]
EXPOSE 8765

# The port is read from the environment on purpose: a hardcoded 8765 reports a healthy
# server as "unhealthy" whenever CLREVIEW_PORT is overridden (measured: CLREVIEW_PORT=9111
# serves /api/health 200 while docker inspect says unhealthy).
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('CLREVIEW_PORT','8765'), timeout=4)"

ENTRYPOINT ["clreview-server"]
