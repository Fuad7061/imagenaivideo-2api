FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8765 \
    DATA_DIR=/data

WORKDIR /app

COPY miricanvas_client.py ./
COPY main.py ./
COPY app ./app

RUN adduser -D -u 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 || exit 1

CMD ["python", "main.py"]
