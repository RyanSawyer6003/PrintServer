FROM debian:bookworm-slim

# Standard library only: no pip packages. tzdata provides the time zone names.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY usage.py /app/usage.py
RUN chmod 755 /app/usage.py \
    && ln -s /app/usage.py /usr/local/bin/usage \
    && mkdir -p /data /reports /staff /status-data

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python3", "/app/usage.py"]
CMD ["run"]
