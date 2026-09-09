-- ABOUTME: The brain's applied-migration ledger — what schema this volume actually got.
-- ABOUTME: Boot gate (mcp) refuses to start when this disagrees with the SPINE manifest.
--
-- Issue #91 / Slice 5.2. The brain is raw-pg by design (Django owns auth via its
-- own django_migrations); this gives the brain the same logbook + boot-gate
-- guarantee. id = the init-file number, so the ledger reads like init/.
--
-- Seeding has two entry points against ONE source of truth (the SPINE in
-- web/openbrain/mcp/boot.py):
--   * Fresh volume  — docker-entrypoint runs every init/*.sql, so this file
--     self-seeds the literal id/name list below. A unit test asserts this list
--     matches the manifest, so a forgotten row fails CI, not prod.
--   * Existing volume — init never re-runs, so `manage.py brain_ledger baseline`
--     creates this table and stamps the manifest. (init/14 only fires on fresh
--     volumes.)
--
-- Name/id-keyed, no checksums: migrations are append-only (you never edit an
-- applied one), so "drift" means the ledger is behind/ahead of the manifest.

create table if not exists brain.schema_migrations (
  id          text primary key,
  name        text not null,
  applied_at  timestamptz not null default now(),
  applied_by  text
);

insert into brain.schema_migrations (id, name, applied_by) values
  ('01', 'extensions',        'bootstrap'),
  ('02', 'thoughts',          'bootstrap'),
  ('03', 'brain',             'bootstrap'),
  ('04', 'hybrid-search',     'bootstrap'),
  ('05', 'consolidation',     'bootstrap'),
  ('06', 'tools',             'bootstrap'),
  ('07', 'summary-cache',     'bootstrap'),
  ('08', 'thoughts-view',     'bootstrap'),
  ('09', 'auth-schema',       'bootstrap'),
  ('10', 'supersede',         'bootstrap'),
  ('11', 'live-filter',       'bootstrap'),
  ('12', 'soft-privacy',      'bootstrap'),
  ('13', 'viewer-filter',     'bootstrap'),
  ('14', 'schema-migrations', 'bootstrap'),
  ('15', 'confidence-traversal', 'bootstrap'),
  ('16', 'alias-scoring',     'bootstrap'),
  ('17', 'phon-tiebreak',     'bootstrap'),
  ('18', 'experience-geo',    'bootstrap'),
  ('19', 'attachments',       'bootstrap'),
  ('20', 'animal-entity-kind', 'bootstrap'),
  ('21', 'resolve-entity-exact-first', 'bootstrap'),
  ('22', 'traversal-merged-anchor', 'bootstrap'),
  ('23', 'flatten-merge-chains', 'bootstrap'),
  ('24', 'capture-idempotency', 'bootstrap')
on conflict (id) do nothing;
