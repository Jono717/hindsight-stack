-- Runs ONCE, on first initialisation of an empty pg_data volume.
-- Docker's postgres entrypoint ignores this directory if the volume already
-- holds a database, so editing this file does not affect an existing stack.
--
-- Hindsight's own migrations create these too. Doing it here as well makes the
-- requirement explicit and fails loudly at database creation time rather than
-- part-way through the API's first migration run.

-- Vector similarity search. Pre-installed in the pgvector/pgvector image.
CREATE EXTENSION IF NOT EXISTS vector;

-- Trigram matching. Hindsight's entity resolution uses the `%` operator and
-- pg_trgm.similarity_threshold for fuzzy name matching during retain.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
