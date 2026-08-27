# The JSON API. No browser, no Playwright - this image stays small.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# setpriv (util-linux) is how the entrypoint drops root after fixing the
# bind-mount ownership. Not in python:slim by default.
RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/api.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker/api-entrypoint.sh /usr/local/bin/api-entrypoint
COPY docker/shared-data.sh /usr/local/lib/shared-data.sh
RUN chmod +x /usr/local/bin/api-entrypoint

# uid 1000 so it matches the worker and both can write the shared volume.
RUN useradd --uid 1000 --create-home --shell /bin/bash portal \
    && mkdir -p /data \
    && chown -R portal:portal /data /srv

EXPOSE 8000

# Starts as root ONLY to chown the /data bind mount, then drops to `portal`.
ENTRYPOINT ["api-entrypoint"]
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
