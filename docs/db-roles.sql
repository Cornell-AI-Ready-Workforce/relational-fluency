-- Roles for the analysis database (run once, as rf_admin, after db-schema.sql).
--
--   rf_loader   what tools/load_analysis_db.py connects as: reads and writes
--               every table in schema rf.
--   rf_analyst  what people connect as: reads every table and view EXCEPT
--               participant_identity, which holds the CloudResearch keys.
--
-- Passwords are set here with \password so they are never in a file:
--   psql "$DSN" -f docs/db-roles.sql
--   psql "$DSN" -c '\password rf_loader'
--   psql "$DSN" -c '\password rf_analyst'

CREATE ROLE rf_loader  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE rf_analyst LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;

GRANT CONNECT ON DATABASE rf TO rf_loader, rf_analyst;
GRANT USAGE ON SCHEMA rf TO rf_loader, rf_analyst;

-- loader: everything
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA rf TO rf_loader;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA rf TO rf_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA rf GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rf_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA rf GRANT USAGE, SELECT ON SEQUENCES TO rf_loader;

-- analyst: read-only, minus identity
GRANT SELECT ON ALL TABLES IN SCHEMA rf TO rf_analyst;
REVOKE ALL ON rf.participant_identity FROM rf_analyst;
ALTER DEFAULT PRIVILEGES IN SCHEMA rf GRANT SELECT ON TABLES TO rf_analyst;

-- analysts may write their own ratings (Phase 2) but nothing else
GRANT INSERT, UPDATE ON rf.rating, rf.rating_assignment TO rf_analyst;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA rf TO rf_analyst;
