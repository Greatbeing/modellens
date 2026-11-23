# syntax=docker/dockerfile:1
#
# ModelLens proxy image: OpenAI-compatible gateway that routes on measured
# evidence. Build from the repository root:
#
#   docker build -f deploy/proxy.Dockerfile -t modellens-proxy .
#   docker run --rm -p 127.0.0.1:8766:8766 \
#     -v "$PWD/config.json:/config/config.json:ro" \
#     -v modellens-data:/data modellens-proxy
#
# The history file lives on a volume: it is the evidence the router learns from.

FROM python:3.12-slim AS build
WORKDIR /src
# Only what the wheel needs; tests and the worker are not part of the runtime.
COPY pyproject.toml README.md LICENSE modellens.py modellens_proxy.py ./
RUN pip install --no-cache-dir --upgrade pip build \
 && python -m build --wheel --outdir /wheels

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MODELLENS_CONFIG=/config/config.json \
    MODELLENS_DATA=/data/history.json

# Non-root, and the two writable places are owned by it up front so a
# read-only root filesystem still works.
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin modellens \
 && mkdir -p /data /config \
 && chown -R modellens:modellens /data /config

COPY --from=build /wheels/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

USER modellens
EXPOSE 8766
VOLUME ["/data"]

# /modellens/health reports table age, breakers and spend; a proxy listening but
# unable to answer that is not healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8766/modellens/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["modellens-proxy"]
CMD ["--config", "/config/config.json", "--data", "/data/history.json", "--listen", "0.0.0.0:8766"]
