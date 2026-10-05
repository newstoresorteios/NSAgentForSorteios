/** Offline PostgreSQL validation with the same PGlite dev dependency as security checks. */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { PGlite } from '../.tools/pglite-security/node_modules/@electric-sql/pglite/dist/index.js';

const db = new PGlite();
try {
  await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; GRANT USAGE ON SCHEMA public TO anon,authenticated;');
  const migration = await readFile(new URL('../sql/037_direct_checkout_executions.sql', import.meta.url), 'utf8');
  await db.exec(migration);
  await db.exec(migration);
  // Exercise production SQL, including its workspace/scope predicates and CAS.
  const source = await readFile(new URL('../app/direct/checkout_journal.py', import.meta.url), 'utf8');
  const queries = [...source.matchAll(/cur\.execute\("""([\s\S]*?)"""/g)].map(m => m[1]);
  assert.equal(queries.length, 3);
  function run(index, args) {
    const keys = [];
    const sql = queries[index].replace(/%\((\w+)\)s/g, (_, key) => {
      if (!keys.includes(key)) keys.push(key);
      return '$' + (keys.indexOf(key) + 1);
    });
    return db.query(sql, keys.map(k => typeof args[k] === 'object' ? JSON.stringify(args[k]) : args[k]));
  }
  const args = {id: 'a'.repeat(32), workspace: '11111111-1111-4111-8111-111111111111',
    scope: 'b'.repeat(64), proposal: {product_id: '42', unit_price: '1250.00'}};
  assert.equal((await run(0, args)).rows.length, 1);
  assert.equal((await run(0, args)).rows.length, 0);
  assert.equal((await run(1, args)).rows[0].status, 'started');
  assert.equal((await run(1, {...args, workspace: '22222222-2222-4222-8222-222222222222'})).rows.length, 0);
  assert.equal((await run(1, {...args, scope: 'c'.repeat(64)})).rows.length, 0);
  assert.equal((await run(2, {...args, scope: 'c'.repeat(64), status: 'completed', result: {}})).rows.length, 0);
  assert.equal((await run(2, {...args, status: 'completed', result: {cart_url: 'https://store.test/cart'}})).rows.length, 1);
  assert.equal((await run(2, {...args, status: 'unknown', result: {}})).rows.length, 0);
  assert.equal((await run(1, args)).rows[0].result.cart_url, 'https://store.test/cart');
  for (const role of ['anon', 'authenticated']) {
    await db.exec(`SET ROLE ${role}`);
    for (const sql of ['SELECT * FROM public.ai_direct_checkout_executions',
      "UPDATE public.ai_direct_checkout_executions SET status='started'",
      'DELETE FROM public.ai_direct_checkout_executions',
      "INSERT INTO public.ai_direct_checkout_executions (id,workspace_id,scope,proposal) VALUES ('x',null,'x','{}')"]) {
      await assert.rejects(() => db.exec(sql), /permission denied/);
    }
    await db.exec('RESET ROLE');
  }
  assert.equal((await db.query("SELECT relrowsecurity FROM pg_class WHERE oid='public.ai_direct_checkout_executions'::regclass")).rows[0].relrowsecurity, true);
  console.log(JSON.stringify({passed: true, engine: 'PGlite', production_connections: 0,
    checks: ['migration_idempotent', 'claim_unique', 'workspace_and_identity_scope',
             'terminal_receipt_immutable', 'receipt_replay', 'anon_authenticated_crud_denied', 'rls_enabled']}));
} finally {
  await db.close();
}
