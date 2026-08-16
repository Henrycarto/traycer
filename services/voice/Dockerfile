# openSMILE is built from source rather than pulled from a package index.
# There is no maintained distro package for 3.x, and pinning the tag keeps
# acoustic values reproducible across rebuilds. A feature vector that shifts
# because a base image moved would look like a patient changing.
FROM debian:bookworm-slim AS opensmile-build

ARG OPENSMILE_REF=v3.0.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch "${OPENSMILE_REF}" https://github.com/audeering/opensmile.git .

RUN cmake -B build -S . \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
    && cmake --build build --parallel "$(nproc)" --target SMILExtract

RUN mkdir -p /opt/opensmile/bin /opt/opensmile/config \
    && cp build/progsrc/smilextract/SMILExtract /opt/opensmile/bin/SMILExtract \
    && cp -r config/* /opt/opensmile/config/


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OPENSMILE_BINARY=/usr/local/bin/SMILExtract \
    OPENSMILE_CONFIG=/opt/opensmile/config/egemaps/v02/eGeMAPSv02.conf

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=opensmile-build /opt/opensmile/bin/SMILExtract /usr/local/bin/SMILExtract
# Configs are mounted read only. The content guard reads them before every
# extraction, so a writable config directory would be a way around the guard.
COPY --from=opensmile-build /opt/opensmile/config /opt/opensmile/config
RUN chmod 0555 /usr/local/bin/SMILExtract && chmod -R a-w /opt/opensmile/config

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Audio is written to a temporary file during extraction and destroyed after.
# The service runs unprivileged so that file is never readable by another
# container process sharing the node.
RUN useradd --system --uid 10001 --home /app --shell /usr/sbin/nologin traycer \
    && chown -R traycer:traycer /app
USER traycer

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
