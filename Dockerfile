# syntax=docker/dockerfile:1.6
#
# One image, three roles: `serve` (default), `worker`, and one-off commands.
# The engine is pinned to a commit of glalit52/TerraSync; TERRASHIELD_REF must
# match the pin in pyproject.toml (tests/test_packaging.py checks).

ARG PYTHON_VERSION=3.12
ARG TERRASHIELD_REF=8338df5593cc0fe0f880c7caca99ba3b6f57bc84

FROM python:${PYTHON_VERSION}-slim AS build
ARG WITH_IMAGERY=1
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels . \
    && if [ "$WITH_IMAGERY" = "1" ]; then \
         pip wheel --no-cache-dir --wheel-dir /wheels "rasterio>=1.3"; fi

FROM python:${PYTHON_VERSION}-slim
ARG TERRASHIELD_REF
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TERRASYNC_DB=/data/terrasync.db \
    TERRASYNC_WEBAPP=/app/webapp/index.html
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
    && rm -rf /wheels
# The web application, from the same engine commit as the API it talks to.
ADD https://raw.githubusercontent.com/glalit52/TerraSync/${TERRASHIELD_REF}/webapp/index.html /app/webapp/index.html
RUN chmod 0644 /app/webapp/index.html \
    && useradd --system --uid 10001 --home /data terrasync \
    && mkdir -p /data && chown terrasync /data
USER terrasync
VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4)" || exit 1
ENTRYPOINT ["terrasync"]
CMD ["serve"]
