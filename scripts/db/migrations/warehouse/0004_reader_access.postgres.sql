-- Read-only access to the warehouse for teammates, through the login xg_reader (Postgres only;
-- SQLite has no logins). The login itself is created once by the database owner in Supabase's
-- SQL Editor:  create role xg_reader login password '...' connection limit 5;
-- If it doesn't exist, this migration does nothing.
--
-- xg_reader can read every table and view in `warehouse`, nothing in `ops`, and nothing of the
-- other app's tables in `public`. Row level security is on, so each table also needs a read
-- policy: a table added by a later migration needs its own
--   CREATE POLICY reader_select ON <table> FOR SELECT TO xg_reader USING (true);
-- (the SELECT grant on new tables comes from the default privileges below).

DO $$
DECLARE t text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xg_reader') THEN
        RAISE NOTICE 'role xg_reader does not exist; skipping';
        RETURN;
    END IF;
    GRANT USAGE ON SCHEMA warehouse TO xg_reader;
    GRANT SELECT ON ALL TABLES IN SCHEMA warehouse TO xg_reader;  -- tables and views
    ALTER DEFAULT PRIVILEGES IN SCHEMA warehouse GRANT SELECT ON TABLES TO xg_reader;
    FOREACH t IN ARRAY ARRAY['games', 'videos', 'alignments', 'events', 'shots', 'captions', 'loads'] LOOP
        EXECUTE format('CREATE POLICY reader_select ON warehouse.%I FOR SELECT TO xg_reader USING (true)', t);
    END LOOP;
END $$;
