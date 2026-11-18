import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { randomBytes } from 'node:crypto';
import { MemoryPipeline } from './pipeline.mjs';
import { FlywheelClient } from './flywheel.mjs';
import { githubIdentity, createMemoryRepository } from './github.mjs';
import { safeError, readJson } from './common.mjs';
import { join } from 'node:path';

const assets = { '/': ['index.html', 'text/html; charset=utf-8'], '/app.js': ['app.js', 'text/javascript; charset=utf-8'], '/style.css': ['style.css', 'text/css; charset=utf-8'] };
export async function serve(home, { port = 8878, pipeline = new MemoryPipeline(home) } = {}) {
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('Invalid local port');
  await mkdir(join(home, 'runs'), { recursive: true, mode: 0o700 });
  const token = randomBytes(32).toString('hex'), reportFrames = new Map(); let client, operation, origin;
  const reply = (response, code, value, type = 'application/json; charset=utf-8', extra = {}) => {
    response.writeHead(code, { 'Content-Type': type, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'", ...extra });
    response.end(Buffer.isBuffer(value) || typeof value === 'string' ? value : JSON.stringify(value));
  };
  function start(label, callback) {
    if (operation?.status === 'running') throw new Error('当前已有操作运行');
    operation = { label, status: 'running', startedAt: new Date().toISOString() };
    void callback().then(result => { operation = { ...operation, status: 'completed', result }; }).catch(error => { operation = { ...operation, status: 'failed', error: safeError(error) }; });
    return { accepted: true, operation };
  }
  const server = createServer(async (request, response) => {
    try {
      if (request.headers.host !== new URL(origin).host) return reply(response, 403, { error: 'Host 不匹配' });
      const url = new URL(request.url, origin), path = url.pathname;
      if (request.method === 'GET' && assets[path]) {
        const [file, type] = assets[path]; return reply(response, 200, await readFile(new URL('../web/' + file, import.meta.url)), type);
      }
      const frame = path.match(/^\/api\/runs\/([a-f0-9-]{36})\/trajectory-frame$/);
      if (request.method === 'GET' && frame && reportFrames.get(url.searchParams.get('key'))?.id === frame[1]) {
        const item = reportFrames.get(url.searchParams.get('key'));
        return reply(response, 200, await readFile(item.path, 'utf8'), 'text/html; charset=utf-8', { 'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; sandbox allow-scripts; frame-ancestors 'self'; base-uri 'none'; form-action 'none'", 'Referrer-Policy': 'no-referrer' });
      }
      if (path !== '/api/bootstrap' && request.headers['x-memory-token'] !== token) return reply(response, 403, { error: '请从本机控制台发起请求' });
      if (request.method === 'GET') {
        if (path === '/api/bootstrap') { let rt; try { rt = await pipeline.runtime(); } catch {} return reply(response, 200, { token, home, runtimeReady: Boolean(rt), runtime: rt && { dsh: rt.dsh, migloop: rt.migloop.commit }, version: '0.2.0' }); }
        if (path === '/api/status') return reply(response, 200, { operation, active: pipeline.active && { id: pipeline.active.id, stage: pipeline.active.stage }, connection: client && { baseUrl: client.baseUrl, credentialPersisted: false }, runs: await pipeline.list() });
        if (path === '/api/flywheel/projects') { if (!client) throw new Error('请先连接飞轮'); return reply(response, 200, await client.projects(url.searchParams.get('q') || '')); }
        if (path === '/api/flywheel/sessions') { if (!client) throw new Error('请先连接飞轮'); return reply(response, 200, await client.sessions({ project: url.searchParams.get('project') || '', q: url.searchParams.get('q') || '', offset: Number(url.searchParams.get('offset') || 0) })); }
        if (path === '/api/github') return reply(response, 200, await githubIdentity());
        const sources = path.match(/^\/api\/runs\/([a-f0-9-]{36})\/sources$/);
        if (sources) return reply(response, 200, await pipeline.sources(sources[1]));
        const trajectory = path.match(/^\/api\/runs\/([a-f0-9-]{36})\/trajectory$/);
        if (trajectory) { const report = await pipeline.get(trajectory[1]); if (!report.trajectory?.path?.startsWith(pipeline.directory(trajectory[1]) + '/trajectory-')) throw new Error('轨迹报告尚未生成'); return reply(response, 200, await readFile(report.trajectory.path, 'utf8'), 'text/html; charset=utf-8'); }
        const match = path.match(/^\/api\/runs\/([a-f0-9-]{36})(\/review-packet)?$/);
        if (match) return reply(response, 200, match[2] ? { suite: await readJson(join(pipeline.directory(match[1]), 'l1-suite/suite.json')), report: await readJson(join(pipeline.directory(match[1]), 'l1-replies/report.json')).catch(() => null) } : await pipeline.get(match[1]));
      }
      if (request.method !== 'POST' || request.headers.origin !== origin || !/^application\/json(?:;|$)/i.test(request.headers['content-type'] || '')) return reply(response, 403, { error: '需要同源 JSON 请求' });
      let bytes = 0, chunks = [];
      for await (const chunk of request) { bytes += chunk.length; if (bytes > 1024 * 1024) return reply(response, 413, { error: '请求超过 1 MiB' }); chunks.push(chunk); }
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (path === '/api/credentials') return reply(response, 200, pipeline.setCredentials(body));
      if (path === '/api/flywheel/connect') { const pending = new FlywheelClient(body.baseUrl, body.token || ''); await pending.projects(); client = pending; return reply(response, 200, { connected: true, baseUrl: client.baseUrl, credentialPersisted: false }); }
      if (path === '/api/flywheel/disconnect') { client = undefined; return reply(response, 200, { connected: false }); }
      if (path === '/api/runs') return reply(response, 202, start('冻结来源', () => pipeline.create(body, client)));
      if (path === '/api/cancel') return reply(response, 200, pipeline.cancel());
      if (path === '/api/github/create') return reply(response, 201, await createMemoryRepository(body.repository));
      const openFrame = path.match(/^\/api\/runs\/([a-f0-9-]{36})\/open-trajectory$/);
      if (openFrame) {
        const report = await pipeline.get(openFrame[1]), key = randomBytes(24).toString('hex');
        if (!report.trajectory?.path?.startsWith(pipeline.directory(openFrame[1]) + '/trajectory-')) throw new Error('先生成轨迹报告');
        if (reportFrames.size > 100) reportFrames.delete(reportFrames.keys().next().value);
        reportFrames.set(key, { id: openFrame[1], path: report.trajectory.path });
        return reply(response, 200, { url: '/api/runs/' + openFrame[1] + '/trajectory-frame?key=' + key });
      }
      const match = path.match(/^\/api\/runs\/([a-f0-9-]{36})\/(view|distill|check|tasks|prepare-evaluation|l1|l2|review|publication-preview|publish)$/);
      if (match) {
        const [, id, action] = match;
        if (action === 'review') return reply(response, 200, await pipeline.reviewL1(id, body));
        if (action === 'publication-preview') return reply(response, 200, await pipeline.previewPublication(id, body));
        if (action === 'publish') return reply(response, 202, start('上传 Memory', () => pipeline.publish(id, body)));
        return reply(response, 202, start(action, () => action === 'view' ? pipeline.view(id, body) : action === 'distill' ? pipeline.distill(id) : action === 'check' ? pipeline.checkDelivery(id) : action === 'tasks' ? pipeline.authorTasks(id) : action === 'prepare-evaluation' ? pipeline.prepareEvaluation(id, body) : pipeline.evaluate(id, action)));
      }
      return reply(response, 404, { error: '未知入口' });
    } catch (error) { return reply(response, 400, { error: safeError(error) }); }
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolve); });
  origin = 'http://127.0.0.1:' + server.address().port;
  return { server, origin, pipeline, close: () => new Promise(resolve => { pipeline.cancel(); server.close(resolve); }) };
}
