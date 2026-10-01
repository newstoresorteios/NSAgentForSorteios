/** Offline PostgreSQL permission and scope regressions using PGlite 0.5.8.
 * Install only as a developer tool: npm install --prefix .tools/pglite-security --ignore-scripts --save-exact @electric-sql/pglite@0.5.8
 * No production connection or customer data is used.
 */
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { PGlite } from '../.tools/pglite-security/node_modules/@electric-sql/pglite/dist/index.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const drafts = path.resolve(root, '../Chatbo-backendAgent/supabase/drafts');
const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';
const results = { engine: 'PGlite 0.5.8 / PostgreSQL 18.3', production_connections: 0, checks: [] };
async function expectDenied(db, sql, message = /permission denied|workspace_required|workspace_immutable|incident_immutable|validation_required|duplicate_active/) {
  await assert.rejects(() => db.exec(sql), message);
}
async function roles(db) {
  await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS; GRANT USAGE ON SCHEMA public TO anon,authenticated,service_role;');
}

async function verifyLegacy() {
  const db = new PGlite();
  await roles(db);
  await db.exec(`
    CREATE TABLE users(id bigserial PRIMARY KEY, payload text);
    CREATE TABLE payments(id bigserial PRIMARY KEY, payload text);
    CREATE TABLE draws(id bigserial PRIMARY KEY, payload text);
    INSERT INTO users(payload) VALUES ('synthetic');
    CREATE VIEW user_coupon_balance_expiry AS SELECT id,payload FROM users;
    CREATE FUNCTION app_probe() RETURNS integer LANGUAGE sql AS 'SELECT 1';
    GRANT ALL ON ALL TABLES IN SCHEMA public TO anon,authenticated,service_role;
    GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon,authenticated,service_role;
    ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO anon,authenticated;
    ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO anon,authenticated;
  `);
  const migration = await readFile(path.join(drafts, 'legacy_nsdb_server_only.sql'), 'utf8');
  await db.exec(migration);
  await db.exec(migration);
  for (const role of ['anon', 'authenticated']) {
    await db.exec(`SET ROLE ${role}`);
    for (const sql of ["SELECT * FROM users", "INSERT INTO payments(payload) VALUES ('denied')",
      "UPDATE users SET payload='denied'", "DELETE FROM users", "SELECT * FROM user_coupon_balance_expiry",
      "SELECT nextval('users_id_seq')", "SELECT app_probe()"])
      await expectDenied(db, sql);
    await db.exec('RESET ROLE');
  }
  for (const role of ['postgres', 'service_role']) {
    await db.exec(`SET ROLE ${role}`);
    await db.exec("INSERT INTO payments(payload) VALUES ('server'); UPDATE payments SET payload='updated'; SELECT * FROM payments; SELECT * FROM user_coupon_balance_expiry; SELECT app_probe(); DELETE FROM payments WHERE payload='updated';");
    await db.exec('RESET ROLE');
  }
  await db.exec("CREATE TABLE future_table(id bigserial, payload text); CREATE FUNCTION future_probe() RETURNS integer LANGUAGE sql AS 'SELECT 1';");
  for (const role of ['anon', 'authenticated']) {
    await db.exec(`SET ROLE ${role}`);
    await expectDenied(db, 'SELECT * FROM future_table');
    await expectDenied(db, 'SELECT future_probe()');
    await db.exec('RESET ROLE');
  }
  await db.exec("SET ROLE service_role; INSERT INTO future_table(payload) VALUES ('server'); SELECT future_probe(); RESET ROLE;");
  const rls = await db.query("SELECT bool_and(relrowsecurity) ok FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname IN ('users','payments','draws')");
  assert.equal(rls.rows[0].ok, true);
  results.checks.push('legacy_anon_authenticated_crud_view_sequence_rpc_denied', 'legacy_postgres_service_role_crud_preserved',
    'legacy_future_objects_secure_defaults', 'legacy_rls_enabled', 'legacy_migration_idempotent');
  await db.close();
}

async function verifyMemory() {
  const db = new PGlite();
  const bootstrapSource = await readFile(path.join(root, 'app/core/db.py'), 'utf8');
  const bootstrapIndex = bootstrapSource.match(/DO \$memory_index\$[\s\S]*?\$memory_index\$;/)?.[0];
  assert.ok(bootstrapIndex, 'memory bootstrap compatibility guard exists');
  await roles(db);
  await db.exec(`
    CREATE TABLE ai_inbound_messages(id bigint PRIMARY KEY,workspace_id uuid);
    CREATE TABLE ai_agent_responses(id bigint PRIMARY KEY,workspace_id uuid,inbound_id bigint);
    CREATE TABLE ai_contact_memories(id bigserial PRIMARY KEY,tenant_id text,workspace_id uuid,sender_key text,memory_key text,status text DEFAULT 'active',source_inbound_id bigint,source_response_id bigint);
    CREATE UNIQUE INDEX uq_ai_contact_memory_active_key ON ai_contact_memories(tenant_id,sender_key,memory_key) WHERE status='active';
    CREATE TABLE ai_conversation_summaries(id bigserial PRIMARY KEY,tenant_id text,workspace_id uuid,conversation_key text,last_inbound_id bigint,last_response_id bigint,UNIQUE(tenant_id,conversation_key));
    CREATE TABLE ai_memory_proposals(id bigserial PRIMARY KEY,workspace_id uuid,inbound_id bigint,response_id bigint);
    CREATE TABLE ai_prompt_compilations(id bigserial PRIMARY KEY,workspace_id uuid,inbound_id bigint,response_id bigint);
    CREATE TABLE ai_learning_cases(id bigserial PRIMARY KEY,tenant_id text,workspace_id uuid,case_key text,conversation_key text,failure_codes jsonb,customer_excerpt text,bad_reply text,correction text,insight_id bigint,status text,updated_at timestamptz DEFAULT now());
    CREATE TABLE ai_agent_instruction_extensions(id bigserial PRIMARY KEY,tenant_id text,workspace_id uuid,instruction_text text,instruction_hash text,status text,expires_at timestamptz,metadata jsonb);
    INSERT INTO ai_inbound_messages VALUES (1,'${A}'),(2,'${B}');
    INSERT INTO ai_agent_responses VALUES (10,'${A}',1),(20,'${B}',2),(30,'${A}',1);
    INSERT INTO ai_contact_memories(tenant_id,sender_key,memory_key,source_inbound_id,source_response_id) VALUES
      ('store','one','color',1,10),('store','conflict','color',1,20),('store','unknown','color',NULL,NULL);
    INSERT INTO ai_prompt_compilations(inbound_id) VALUES (1),(2);
    INSERT INTO ai_conversation_summaries(tenant_id,conversation_key,last_inbound_id) VALUES ('store','thread',1);
  `);
  const prepare = await readFile(path.join(drafts, 'memory_scope_learning_policy.sql'), 'utf8');
  const enforce = await readFile(path.join(drafts, 'memory_scope_learning_policy_enforce.sql'), 'utf8');
  await db.exec('DROP INDEX uq_ai_contact_memory_active_key');
  await db.exec(bootstrapIndex);
  assert.notEqual((await db.query("SELECT to_regclass('public.uq_ai_contact_memory_active_key') old_index")).rows[0].old_index, null);
  await db.exec(prepare);
  await db.exec(prepare);
  let rows = (await db.query('SELECT workspace_id,scope_status FROM ai_contact_memories ORDER BY id')).rows;
  assert.deepEqual(rows, [{ workspace_id: A, scope_status: 'verified' },
    { workspace_id: null, scope_status: 'quarantined' }, { workspace_id: null, scope_status: 'quarantined' }]);
  // PREPARE must still accept the old writer until every process is upgraded.
  await db.exec("INSERT INTO ai_contact_memories(tenant_id,sender_key,memory_key,source_inbound_id) VALUES ('store','late-old-worker','color',2)");
  await db.exec(enforce);
  await db.exec(enforce);
  await db.exec(bootstrapIndex);
  assert.equal((await db.query("SELECT to_regclass('public.uq_ai_contact_memory_active_key') old_index")).rows[0].old_index, null);
  results.checks.push('bootstrap_retains_legacy_support_without_recreating_global_key_after_enforce');
  rows = (await db.query("SELECT workspace_id FROM ai_contact_memories WHERE sender_key='late-old-worker'")).rows;
  assert.equal(rows[0].workspace_id, B);
  await expectDenied(db, "INSERT INTO ai_contact_memories(tenant_id,sender_key,memory_key) VALUES ('store','no-scope','color')");
  await expectDenied(db, `UPDATE ai_contact_memories SET workspace_id='${B}' WHERE id=1`);
  await db.exec(`INSERT INTO ai_contact_memories(tenant_id,workspace_id,sender_key,memory_key) VALUES ('store','${B}','one','color');
    INSERT INTO ai_conversation_summaries(tenant_id,workspace_id,conversation_key) VALUES ('store','${B}','thread');`);
  rows = (await db.query('SELECT response_id FROM ai_prompt_compilations ORDER BY id')).rows;
  assert.deepEqual(rows, [{response_id:null},{response_id:20}]);
  await db.exec(`INSERT INTO ai_learning_cases(tenant_id,workspace_id,case_key,failure_codes,customer_excerpt,bad_reply,correction)
    VALUES ('store','${A}','case-1','["repeat"]','synthetic','bad','good'),('store','${A}','case-2','["repeat"]','synthetic','bad','good');`);
  await expectDenied(db, "UPDATE ai_learning_cases SET bad_reply='rewritten' WHERE id=1");
  assert.equal((await db.query("SELECT incident_count FROM ai_learning_case_patterns WHERE pattern_key='repeat'")).rows[0].incident_count, 2);
  await expectDenied(db, `INSERT INTO ai_agent_instruction_extensions(tenant_id,workspace_id,instruction_text,instruction_hash,status,metadata) VALUES ('store','${A}','Safe instruction','hash','active','{}')`);
  const proof = JSON.stringify({policy_validation:{version:'commercial-policy-v1',status:'passed',instruction_hash:'hash'}});
  await db.query('INSERT INTO ai_agent_instruction_extensions(tenant_id,workspace_id,instruction_text,instruction_hash,status,metadata) VALUES ($1,$2,$3,$4,$5,$6)',
    ['store',A,'Safe instruction','hash','active',proof]);
  await expectDenied(db, `INSERT INTO ai_agent_instruction_extensions(tenant_id,workspace_id,instruction_text,instruction_hash,status,metadata) VALUES ('store','${A}','Safe instruction','hash','active','${proof}')`);
  await db.exec('SET ROLE anon');
  await expectDenied(db, 'SELECT * FROM ai_contact_memories');
  await expectDenied(db, 'SELECT * FROM ai_learning_case_patterns');
  await db.exec(`RESET ROLE; SET ROLE service_role; INSERT INTO ai_contact_memories(tenant_id,workspace_id,sender_key,memory_key) VALUES ('store','${A}','server','color'); SELECT * FROM ai_contact_memories; SELECT * FROM ai_learning_case_patterns; RESET ROLE;`);
  results.checks.push('memory_backfill_uses_only_unambiguous_evidence', 'memory_ambiguous_legacy_quarantined',
    'memory_prepare_accepts_old_writer', 'memory_enforce_reconciles_late_writer', 'memory_workspace_required_immutable',
    'memory_cross_workspace_unique_keys', 'prompt_ambiguous_response_not_guessed', 'learning_incidents_immutable_patterns_aggregate',
    'extension_activation_requires_validation_and_deduplicates', 'memory_service_role_preserved_anon_denied', 'memory_migrations_idempotent');
  await db.close();
}

try {
  await verifyLegacy();
  await verifyMemory();
  results.passed = true;
  console.log(JSON.stringify(results, null, 2));
  const output = process.argv[2];
  if (output) await writeFile(output, JSON.stringify(results, null, 2) + '\n');
} catch (error) {
  console.error(JSON.stringify({passed:false,message:error.message,code:error.code,stack:error.stack?.split('\n').slice(0,5)}, null, 2));
  process.exitCode = 1;
}
