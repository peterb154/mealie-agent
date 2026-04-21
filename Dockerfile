# Your agent's image. Everything here is yours — edit freely.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl libpq5 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Vendored framework code — you own this, edit if you need to.
COPY strands_pg/   /app/strands_pg/
COPY migrations/   /app/migrations/

# Your agent.
COPY app.py       /app/
COPY tools/       /app/tools/
COPY prompts/     /app/prompts/
COPY scripts/     /app/scripts/
COPY static/      /app/static/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV STRANDS_PG_MIGRATIONS_DIR=/app/migrations \
    PORT=8000

# Bake the commit SHA into the image so /api/health can report it.
# docker-compose passes this as a build arg from the host's git checkout.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
