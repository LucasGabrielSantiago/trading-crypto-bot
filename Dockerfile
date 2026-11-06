FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt requirements-docker.txt ./
RUN pip install --no-cache-dir -r requirements-docker.txt

COPY main.py ./
COPY bot ./bot

RUN useradd --system --uid 10001 bot && mkdir /data && chown bot:bot /data
USER bot
VOLUME /data

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8765/health', timeout=3)"

CMD ["python", "main.py", "ui"]
