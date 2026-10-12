FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# config + state + log live in /data (mount a volume there)
VOLUME /data
ENTRYPOINT ["meshscore", "run", "--config", "/data/meshscore.toml", "--data-dir", "/data"]
