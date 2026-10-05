import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { createProductStore } from '../src/product-store.js';

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-store-'));
  const store = await createProductStore(directory);
  const db = new DatabaseSync(join(directory, 'dashboard.sqlite3'));
  t.after(async () => { db.close(); await store.close(); await rm(directory, { recursive: true, force: true }); });
  await store.transaction(state => {
    for (const id of ['first', 'second']) {
      state.operations[id] = { id, kind: 'deployments', status: 'succeeded', stage: 'complete',
        ci: { state: 'published', run_id: '123', steps: [{ log: 'private build output' }] },
        telemetry: { events: [{ message: 'private history' }] }, queue: { sequence: 1 } };
      state.applications[id] = { id, environment_target_id: 'aws', app: id };
    }
    state.bindings['123'] = { operation_id: 'first' };
    state.keys.request = 'first';
    state.plans.plan = { public: { status: 'ready' } };
  });
  db.exec('CREATE TABLE audit (resource TEXT, action TEXT)');
  for (const table of ['operations', 'plans', 'applications', 'bindings', 'idempotency', 'personal_state']) {
    for (const action of ['INSERT', 'UPDATE', 'DELETE']) db.exec(`CREATE TRIGGER audit_${table}_${action} AFTER ${action} ON ${table}
      BEGIN INSERT INTO audit VALUES ('${table}', '${action}'); END;`);
  }
  return { store, db };
}

test('targeted reads and queue snapshots are detached and omit deployment payloads', async t => {
  const { store } = await fixture(t);
  const row = store.read('operations', 'first');
  row.status = 'failed'; row.telemetry.events[0].message = 'changed';
  const rows = store.read('operations'); rows.first.queue.sequence = 100;
  const queue = store.schedulingState();
  assert.equal(queue.operations.first.ci.run_id, '123');
  assert.equal(queue.operations.first.ci.steps, undefined);
  assert.equal(queue.operations.first.telemetry, undefined);
  queue.operations.first.queue.sequence = 200;
  queue.bindings['123'].operation_id = 'second';
  assert.equal(store.read('operations', 'first').status, 'succeeded');
  assert.equal(store.read('operations', 'first').queue.sequence, 1);
  assert.equal(store.read('operations', 'first').telemetry.events[0].message, 'private history');
  assert.equal(store.read('bindings', '123').operation_id, 'first');
  assert.equal(store.read('operations', '__proto__'), undefined);
  assert.equal(store.read('__proto__'), undefined);
});

test('no-op transactions do not write rows; one operation update preserves all other records', async t => {
  const { store, db } = await fixture(t);
  await store.transaction(() => {});
  await store.updateOperation('first', () => {});
  assert.equal(db.prepare('SELECT count(*) AS count FROM audit').get().count, 0);
  await store.updateOperation('first', row => { row.url = 'https://app.example.test'; });
  assert.deepEqual(db.prepare('SELECT resource, action FROM audit').all().map(row => ({ ...row })),
    [{ resource: 'operations', action: 'UPDATE' }]);
  assert.equal(JSON.parse(db.prepare("SELECT record FROM operations WHERE id = 'first'").get().record).url, 'https://app.example.test');
  assert.equal(store.read('bindings', '123').operation_id, 'first');
  assert.equal(store.read('operations', 'second').status, 'succeeded');
  // Valid name exchanges and deleting parent + references remain atomic.
  await store.transaction(state => {
    [state.applications.first.app, state.applications.second.app] = [state.applications.second.app, state.applications.first.app];
    delete state.operations.first; delete state.bindings['123']; delete state.keys.request;
  });
  assert.equal(db.prepare('SELECT count(*) AS count FROM bindings').get().count, 0);
  assert.equal(store.read('applications', 'first').app, 'second');
});

test('latest deployment reads preserve admission order, ownership and detached results', async t => {
  const { store } = await fixture(t);
  const owner = store.dashboard.session().id, stranger = store.dashboard.session().id;
  const identity = { app: 'demo', target_id: 'aws', application_id: 'app-1', session_id: owner };
  await store.transaction(state => {
    const record = { ...state.operations.first, ...identity, cd: { state: 'succeeded' } };
    for (const [id, changes] of [
      ['old', { created_at: '2099-01-01T00:00:00Z' }],
      ['current', { created_at: '2026-01-01T00:00:00Z' }],
      ['pending', { cd: { state: 'not_started' } }],
      ['other-app', { application_id: 'app-2', session_id: stranger }],
      ['other-target', { target_id: 'gcp' }],
      ['build', { kind: 'builds' }],
      ['legacy', { cd: undefined }],
    ]) state.operations[id] = { ...record, ...changes, id };
  });
  const selected = store.latestDeployment(identity, { requireCdState: true });
  assert.equal(selected.id, 'current', 'last admission, not wall-clock sorting');
  selected.telemetry.events[0].message = 'modified';
  assert.equal(store.read('operations', 'current').telemetry.events[0].message, 'private history');
  assert.equal(store.latestDeployment({ app: 'demo', target_id: 'aws' }).id, 'legacy', 'legacy log ownership remains protected');
  assert.equal(store.latestDeployment({ app: 'demo', target_id: 'aws' }, { requireCdState: true }).id, 'other-app');
  assert.equal(store.latestDeployment({ ...identity, session_id: 'missing' }), undefined);
  await store.updateOperation('pending', row => { row.cd.state = 'running'; });
  assert.equal(store.latestDeployment(identity, { requireCdState: true }).id, 'pending', 'subsequent admission invalidates a prior read');
});

test('async operation updates serialize with full transactions and roll back failures', async t => {
  const { store, db } = await fixture(t);
  let release, started;
  const blocked = new Promise(resolve => { release = resolve; });
  const ready = new Promise(resolve => { started = resolve; });
  const first = store.updateOperation('first', async row => { row.count = 1; started(); await blocked; });
  await ready;
  const second = store.transaction(state => { state.operations.first.count += 1; });
  assert.equal(store.read('operations', 'first').count, undefined);
  release(); await Promise.all([first, second]);
  assert.equal(store.read('operations', 'first').count, 2);
  await assert.rejects(store.updateOperation('first', row => { row.count = 100; throw new Error('cancelled'); }), /cancelled/);
  assert.equal(store.read('operations', 'first').count, 2);
  await store.updateOperation('first', row => { row.count += 1; });
  assert.equal(store.read('operations', 'first').count, 3);
  await assert.rejects(store.transaction(state => {
    state.operations.first.status = 'failed';
    state.bindings['123'].operation_id = 'missing';
  }), /FOREIGN KEY/);
  assert.equal(store.read('operations', 'first').status, 'succeeded');
  assert.equal(db.prepare("SELECT status FROM operations WHERE id = 'first'").get().status, 'succeeded');
  assert.equal(db.prepare("SELECT operation_id FROM bindings WHERE run_id = '123'").get().operation_id, 'first');
  await assert.rejects(store.updateOperation('first', () => {}), /requires recovery/);
});
