FROM python:3.12-slim

# ffmpeg is only used to copy the audio track out in the Groq backup path.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMP_DIR=/tmp/reel-brain

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY fetchers ./fetchers
COPY services ./services

# ROLE=worker runs the analysis worker; anything else serves the web app.
# Render sets $PORT; default to 8000 locally.
CMD ["sh", "-c", "if [ \"$ROLE\" = worker ]; then exec python -m app.worker_main; else exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}; fi"]
