# One image for every Python service (api, worker, scheduler, egress proxy, migrations).
FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# ffmpeg + aria2 are what yt-dlp shells out to; tini reaps the process groups the worker spawns.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg aria2 tini ca-certificates util-linux \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
# Non-root, fixed uid so volume ownership is predictable.
RUN useradd --system --uid 10001 --create-home --home-dir /home/ytaria ytaria \
 && mkdir -p /data/media /data/scratch && chown -R ytaria:ytaria /data
USER ytaria
ENV YTARIA_MEDIA_ROOT=/data/media YTARIA_SCRATCH_ROOT=/data/scratch YTARIA_ENV=production
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--no-server-header"]
