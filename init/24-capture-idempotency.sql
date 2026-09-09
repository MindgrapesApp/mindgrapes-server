-- ABOUTME: Adds brain.capture_idempotency — (owner, key) -> the stored capture response.
-- ABOUTME: Server-side dedup for the app's offline-retry captures (#59).
--
-- Issue #59: the iOS app retries captures from an offline queue. If the server
-- commits the write but the ACK is lost in transit, the phone retries and today
-- mints a DUPLICATE experience. This table lets the two capture doors
-- (POST /capture/note, /capture/image) dedup on a client-supplied idempotency_key.
--
--   * Scope is (owner, idempotency_key): keys are untrusted client input, so one
--     member's key must never resolve to another member's experience. The
--     composite primary key IS the unique constraint the write's
--     `on conflict (owner, idempotency_key) do nothing` targets.
--   * `response` stores the FULL assembled service-result dict (jsonb), not just
--     the experience id, so both doors — note returns {experience_id}; image
--     returns {experience_id, attachment_id, object_key, byte_len} — replay
--     through one uniform lookup instead of reconstructing a shape by join.
--   * `experience_id` is a convenience back-reference only; `response` is the
--     source of truth for what a replay returns.
--
-- Retention: keys are only useful while a capture sits in the app's offline
-- retry queue, which is short-lived. A pg_cron sweep deletes rows older than 30
-- days (a wide margin over any realistic offline window); a retry that arrives
-- after its key is swept degrades to the pre-#59 duplicate behavior, which is
-- acceptable — the guarantee is a best-effort optimization over the retry
-- window, not a permanent uniqueness contract.
--
-- Ordering: sorts after init/23-flatten-merge-chains.sql. Idempotent: CREATE
-- TABLE / INDEX IF NOT EXISTS + the unschedule/schedule guard + on-conflict
-- schema_version insert. Safe to re-run against an existing volume via
-- `manage.py brain_ledger migrate`.

create table if not exists brain.capture_idempotency (
  owner            text not null,
  idempotency_key  text not null,
  response         jsonb not null,
  experience_id    uuid,
  created_at       timestamptz not null default now(),
  primary key (owner, idempotency_key)
);

create index if not exists capture_idempotency_created_at_idx
  on brain.capture_idempotency (created_at);

-- TTL sweep: cron.schedule errors on a duplicate jobname, so unschedule first;
-- the sub-block keeps a missing previous schedule from stopping the install.
do $$
begin
  begin perform cron.unschedule('capture-idempotency-ttl'); exception when others then null; end;
  perform cron.schedule(
    'capture-idempotency-ttl',
    '17 4 * * *',
    $cron$ delete from brain.capture_idempotency where created_at < now() - interval '30 days'; $cron$
  );
end $$;

insert into brain.schema_version (version, description)
  values (19, 'capture-idempotency: (owner, key) -> stored response for offline-retry dedup (#59)')
  on conflict (version) do nothing;
