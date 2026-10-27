FROM python:3.13-alpine

RUN adduser -D -H -u 10001 exporter
WORKDIR /app
COPY lhm_exporter.py .

USER exporter
ENV PYTHONUNBUFFERED=1 \
    EXPORTER_PORT=9188
EXPOSE 9188

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:9188/healthz >/dev/null || exit 1

ENTRYPOINT ["python", "/app/lhm_exporter.py"]
