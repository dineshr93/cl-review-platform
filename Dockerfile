FROM python:3.13-slim AS runtime

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

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4)"

ENTRYPOINT ["clreview-server"]
