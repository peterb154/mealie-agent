# Your db image. Edit freely.
# Base image gives you Postgres 17 + pgvector; we add PostGIS on top.
FROM pgvector/pgvector:pg17

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        postgresql-17-postgis-3 \
        postgresql-17-postgis-3-scripts \
 && rm -rf /var/lib/apt/lists/*
