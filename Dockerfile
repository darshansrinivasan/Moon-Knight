FROM python:3.13-slim

# Fast, quiet, no stale bytecode in the image
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code edits don't bust the pip layer
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Non-root; /data is where Railway's volume mounts. The app dir is read-only to
# appuser, so the database defaults there too (override with QC_DB_PATH).
RUN useradd --create-home appuser && mkdir -p /data && chown appuser:appuser /data
ENV QC_DB_PATH=/data/qc.db

# Starts as root only long enough to take ownership of the mounted volume,
# then drops to appuser. Running the app itself as root is never necessary.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*' --workers 1"]
