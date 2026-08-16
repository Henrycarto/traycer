FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --system --uid 10001 --home /app --shell /usr/sbin/nologin traycer \
    && chown -R traycer:traycer /app
USER traycer

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"

# This service holds a decrypted behavioral payload in memory for the length
# of one request, and it holds the RSA private keys for as long as it runs.
# More workers means more copies of both, so the count stays low and scaling
# happens by adding tasks rather than by widening one.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
