# syntax=docker/dockerfile:1
FROM docker.io/pgvector/pgvector:pg17 AS runtime
COPY --link --chmod=755 docker/init-db.sh /docker-entrypoint-initdb.d/10-golos-v2.sh
