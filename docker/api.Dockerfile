# The JSON API. No browser, no Playwright - this image stays small.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements/api.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# uid 1000 so it matches the worker and both can write the shared volume.
RUN useradd --uid 1000 --create-home --shell /bin/bash portal \
    && mkdir -p /data \
    && chown -R portal:portal /data /srv

USER portal

EXPOSE 8000

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
