# The scraper. Playwright's image already carries Chromium and its OS deps;
# Xvfb is what lets us run that Chromium *headed* on a machine with no display.
# Headed matters: Reddit serves headless Chromium a block page.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv

WORKDIR /srv

# xvfb is present in most Playwright images, but pinning it here means the
# build fails loudly rather than the worker failing at 3am on a base bump.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/worker.txt ./requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY app ./app
COPY docker/worker-entrypoint.sh /usr/local/bin/worker-entrypoint
COPY docker/shared-data.sh /usr/local/lib/shared-data.sh
RUN chmod +x /usr/local/bin/worker-entrypoint

# pwuser is uid/gid 1001 here while the api image's user is 1000. Rather
# than renumber either, add pwuser to gid 1000 so both can write the
# shared SQLite directory and each other's WAL sidecar files.
RUN usermod -aG 1000 pwuser

# Starts as root only to fix the bind-mount ownership, then drops to pwuser -
# the image's non-root user - so Chromium keeps its sandbox.
ENTRYPOINT ["worker-entrypoint"]
CMD ["python", "-m", "app.worker.monitor", "--loop"]
