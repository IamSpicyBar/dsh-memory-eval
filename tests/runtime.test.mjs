import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { FlywheelClient } from '../runtime/flywheel.mjs';
import { serve } from '../runtime/server.mjs';
import { publicationPreview } from '../runtime/github.mjs';
import { writeJson } from '../runtime/common.mjs';
import { request } from 'node:http';
import { apply as budgetPlugin } from '../runtime/budget.mjs';

test('shared agent request budget counts child requests before dispatch and stops at the cap', async t => {
  const root = await mkdtemp(join(tmpdir(), 'memory-budget-')); t.after(() => rm(root, { recursive: true, force: true }));
  let hook, sent = 0;
  budgetPlugin({ on: (name, fn) => { assert.equal(name, 'agent/request'); hook = fn; } }, { requests: 2, output: join(root, 'budget.json') });
  for (const sessionId of ['root', 'child']) assert.equal(hook({ sessionId }, () => ++sent), sent);
  assert.throws(() => hook({ sessionId: 'another-child' }, () => ++sent), /budget reached/);
  assert.equal(sent, 2); assert.deepEqual(JSON.parse(await readFile(join(root, 'budget.json'))), { requests: 2, limit: 2 });
});

test('flywheel freezes streamed bytes and sends no dashboard token to presigned object storage', async t => {
  const root = await mkdtemp(join(tmpdir(), 'memory-flywheel-')); t.after(() => rm(root, { recursive: true, force: true }));
  const requests = [], payload = Buffer.from('actual archive bytes');
  const client = new FlywheelClient('https://flywheel.example/api/v1/agent-perf/', 'fixture-private-token', { fetchImpl: async (url, options) => {
    requests.push({ url: String(url), options });
    return requests.length === 1 ? Response.json({ download_url: 'https://objects.example/frozen.tar.gz?signed=temporary' }) : new Response(payload);
  }});
  const receipt = await client.download({ project: 'p', sessionId: 's' }, join(root, 'source'));
  assert.deepEqual(await readFile(receipt.archive), payload);
  assert.equal(requests[0].options.headers.Authorization, 'Bearer fixture-private-token');
  assert.equal(requests[1].options.headers, undefined);
  assert.doesNotMatch(await readFile(join(root, 'source/download.json'), 'utf8'), /fixture-private-token|signed=temporary/);
  assert.equal(JSON.parse(requests[0].options.body).migbot_session_id, 's');
});
test('flywheel rejects credential URLs, insecure hosts, redirects and oversized streams', async t => {
  assert.throws(() => new FlywheelClient('http://remote.example/api/'), /HTTPS/);
  assert.throws(() => new FlywheelClient('https://user:secret@remote.example/'), /HTTPS/);
  const root = await mkdtemp(join(tmpdir(), 'memory-size-')); t.after(() => rm(root, { recursive: true, force: true }));
  const client = new FlywheelClient('https://example.test/api/', '', { fetchImpl: async (_url, options) => { assert.equal(options.redirect, 'error'); return new Response('too large'); } });
  await assert.rejects(client.download({ project: 'p', sessionId: 's' }, join(root, 'source'), { maxBytes: 2 }), /上限/);
  await assert.rejects(client.request('https://evil.example/path'), /越界/);
});
test('local console enforces Host, token and Origin; model credentials remain transient', async t => {
  const root = await mkdtemp(join(tmpdir(), 'memory-http-')); t.after(() => rm(root, { recursive: true, force: true }));
  const instance = await serve(root, { port: 0 }); t.after(() => instance.close());
  const boot = await (await fetch(instance.origin + '/api/bootstrap')).json();
  assert.equal((await fetch(instance.origin + '/api/status')).status, 403);
  const wrongHost = await new Promise((resolve, reject) => { const req = request(instance.origin + '/api/bootstrap', { headers: { Host: 'evil.example' } }, response => { response.resume(); resolve(response.statusCode); }); req.on('error', reject); req.end(); });
  assert.equal(wrongHost, 403);
  const base = { method: 'POST', headers: { 'x-memory-token': boot.token, 'Content-Type': 'application/json', Origin: 'https://evil.example' }, body: JSON.stringify({ modelKey: 'fixture-secret' }) };
  assert.equal((await fetch(instance.origin + '/api/credentials', base)).status, 403);
  const response = await fetch(instance.origin + '/api/credentials', { ...base, headers: { ...base.headers, Origin: instance.origin } });
  assert.equal(response.status, 200); assert.equal((await response.json()).persisted, false);
  const state = await (await fetch(instance.origin + '/api/status', { headers: { 'x-memory-token': boot.token } })).text();
  assert.doesNotMatch(state, /fixture-secret/);
  assert.equal(instance.pipeline.credentials.modelKey, 'fixture-secret');
});
test('validated publication rejects missing or stale evaluations and unversioned store files', async t => {
  const root = await mkdtemp(join(tmpdir(), 'memory-publish-')); t.after(() => rm(root, { recursive: true, force: true }));
  const run = join(root, 'distillation'), revision = 'a'.repeat(64);
  await mkdir(join(run, 'memory'), { recursive: true }); await mkdir(join(run, 'store'));
  await writeFile(join(run, 'memory/index.md'), 'candidate memory');
  await writeJson(join(run, 'store/HEAD.json'), { revision });
  await writeJson(join(run, 'delivery-check.json'), { revision });
  const report = { distillation: { run }, memory: { revision } };
  await writeJson(join(root, 'experiment.json'), report);
  await assert.rejects(publicationPreview(root, { repository: 'owner/repo', mode: 'validated' }), /L1/);
  const candidate = await publicationPreview(root, { repository: 'owner/repo', mode: 'candidate' });
  assert.ok(candidate.files['memory/index.md']); assert.equal(candidate.mode, 'candidate');
  await writeJson(join(run, 'store/credentials.json'), { accidentally: 'secret' });
  await assert.rejects(publicationPreview(root, { repository: 'owner/repo' }), /非版本化/);
});
