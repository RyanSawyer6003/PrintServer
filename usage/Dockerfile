FROM debian:trixie-slim

# Standard library only: no pip packages. tzdata provides the time zone names.
# apt-get upgrade: the slim base image lags Debian's security updates by up
# to a few weeks, and the vulnerability scan in CI counts its packages too.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    python3 \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY usage.py /app/usage.py
# The print server theme, shared with the CUPS pages (the CUPS image copies it too).
COPY theme/ /app/theme/
RUN chmod 755 /app/usage.py \
    && ln -s /app/usage.py /usr/local/bin/usage \
    && mkdir -p /data /reports /staff /status-data

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python3", "/app/usage.py"]
CMD ["run"]
