// Offline PostgreSQL verification of the exact consent-history query used by the agent.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const { PGlite } = await import(process.argv[2] || '@electric-sql/pglite');
const db = new PGlite();
await db.exec(`
  CREATE TABLE ai_inbound_messages(id bigint,workspace_id text,conversation_id text,channel text,text text);
  CREATE TABLE ai_agent_responses(id bigint,inbound_id bigint,workspace_id text,channel text,
    reply_text text,provider_response jsonb,provider_send_ok boolean);
  INSERT INTO ai_inbound_messages VALUES
    (1,'w','thread','whatsapp','help'),(2,'other','thread','whatsapp','foreign'),
    (3,'w','thread','instagram','other channel'),(4,'w','other-thread','whatsapp','other session'),
    (5,'w','thread','whatsapp','yes');
  INSERT INTO ai_agent_responses VALUES
    (1,1,'w','whatsapp','Want a human?', '{"_agent_metadata":{"handoff":{"offer":true,"required":false}}}',true),
    (2,2,'other','whatsapp','foreign response','{}',true),
    (3,3,'w','instagram','other channel response','{}',true),
    (4,4,'w','whatsapp','other session response','{}',true),
    (5,1,'w','whatsapp','failed offer','{}',false);
`);
const source = await readFile(new URL('../app/ops/handoff_consent.py', import.meta.url), 'utf8');
let parameter = 0;
const sql = source.match(/cur\.execute\("""([\s\S]*?)"""/)[1].replace(/%s/g, () => `$${++parameter}`);
let result = await db.query(sql, [5, 'thread', 'whatsapp']);
assert.equal(result.rows.length, 1);
assert.equal(result.rows[0].reply_text, 'Want a human?');
assert.equal(result.rows[0].response_metadata.handoff.offer, true);
assert.equal((await db.query(sql, [5, 'other-thread', 'whatsapp'])).rows.length, 0);
assert.equal((await db.query(sql, [5, 'thread', 'instagram'])).rows.length, 0);
await db.exec("UPDATE ai_agent_responses SET provider_send_ok=false WHERE id=1");
assert.equal((await db.query(sql, [5, 'thread', 'whatsapp'])).rows[0].reply_text, null);
await db.exec("UPDATE ai_agent_responses SET provider_send_ok=true WHERE id=1; INSERT INTO ai_inbound_messages VALUES (6,'w','thread','whatsapp','no'),(7,'w','thread','whatsapp','yes')");
assert.equal((await db.query(sql, [7, 'thread', 'whatsapp'])).rows[0].reply_text, null, 'an unanswered intervening turn cannot reuse the old offer');
await db.close();
console.log('PASS: delivered consent metadata, exact workspace/channel/thread and intervening-turn boundaries.');
