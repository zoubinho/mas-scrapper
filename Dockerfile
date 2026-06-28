# MAS FID Directory — web app.
# Based on the official Playwright image so headless Chromium + all OS
# dependencies are present for the auto-refresh (the MAS /print page is a JS SPA).
# The image's bundled browser matches the pinned playwright version, so the
# fetcher's executable_path auto-detection resolves to it (no MAS_CHROMIUM_PATH
# needed in production).
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MAS_DATA_DIR=/data \
    MAS_FETCH_METHOD=auto \
    PORT=5570

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mas_scrapper.py fetcher.py app.py ./
COPY static ./static

# Persistent snapshot storage (mount a volume here in production)
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 5570

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','5570')+'/healthz').status==200 else 1)"

# 2 workers; generous timeout for the on-demand headless-browser fetch.
# `exec` so gunicorn is PID 1 and receives SIGTERM from `docker stop`.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-5570} --workers 2 --timeout 180 app:app"]
