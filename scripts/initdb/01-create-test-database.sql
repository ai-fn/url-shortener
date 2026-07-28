-- Runs once, on first initialisation of an empty postgres-data volume. Integration
-- tests connect here, not to the app's `shortener`, so a run cannot touch dev data.
-- An existing volume will not re-run initdb: `docker compose down -v` to recreate.
--
-- \gexec, because Postgres has no CREATE DATABASE IF NOT EXISTS and CREATE DATABASE
-- cannot run inside a transaction, ruling out a DO block. The SELECT yields the
-- statement text only when the database is absent.
SELECT 'CREATE DATABASE shortener_test OWNER shortener'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'shortener_test')\gexec
