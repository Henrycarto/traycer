FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The scientific stack ships manylinux wheels for this platform, so no
# compiler is needed at image build time. libgomp is the one runtime library
# numpy and scikit-learn link against that the slim base omits.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Deployed images add the private packages here with an extra index and a
# build secret. Their absence is handled at runtime rather than at build time,
# so this image is the same one anyone can build from the public repository.
ARG PRIVATE_INDEX_URL=""
RUN if [ -n "$PRIVATE_INDEX_URL" ]; then \
        pip install --no-cache-dir --extra-index-url "$PRIVATE_INDEX_URL" \
            traycer-relapse-model traycer-coefficients ; \
    fi

COPY app ./app

RUN useradd --system --uid 10001 --home /app --shell /usr/sbin/nologin traycer \
    && chown -R traycer:traycer /app
USER traycer

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
