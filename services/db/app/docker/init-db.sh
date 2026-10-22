#!/bin/sh
set -eu
echo "1234"
# psql variables safely quote values as SQL literals; never interpolate shell SQL.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=auth_password="$AUTH_DB_PASSWORD" --set=domain_password="$DOMAIN_DB_PASSWORD" <<'SQL'
CREATE USER golos_auth WITH PASSWORD :'auth_password';
CREATE USER golos_domain WITH PASSWORD :'domain_password';
CREATE DATABASE golos_auth OWNER golos_auth;
CREATE DATABASE golos_domain OWNER golos_domain;
REVOKE ALL ON DATABASE golos_auth FROM PUBLIC;
REVOKE ALL ON DATABASE golos_domain FROM PUBLIC;
\connect golos_domain
CREATE EXTENSION IF NOT EXISTS vector;
SQL
