FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

# Two entrypoints share this image (see railway.json for which service runs
# which command) — the FastAPI webhook receiver, and the cron-runner that
# processes due jobs. Both need the full repo, so one image, two Railway
# services pointed at the same build with different start commands.
EXPOSE 8000
CMD ["uvicorn", "pipeline.receiver.app:app", "--host", "0.0.0.0", "--port", "8000"]
