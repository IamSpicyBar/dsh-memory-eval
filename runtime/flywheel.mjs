import { createWriteStream } from 'node:fs';
import { mkdir, rename, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { Transform, Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { writeJson } from './common.mjs';

function endpoint(value) {
  const url = new URL(value);
  if (!(url.protocol === 'https:' || url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) || url.username || url.password || url.search || url.hash) throw new Error('飞轮 API 地址需要 HTTPS，或本机 HTTP，且不能携带凭据');
  url.pathname = url.pathname.replace(/\/$/, '') + '/'; return url;
}

// Only this process owns the access token. Persistent receipts contain identity
// and hashes, never access tokens or temporary download URLs.
export class FlywheelClient {
  #token; #base; #fetch;
  constructor(base, token = '', { fetchImpl = fetch } = {}) { this.#base = endpoint(base); this.#token = token; this.#fetch = fetchImpl; }
  get baseUrl() { return this.#base.href; }
  async request(path, options = {}) {
    const url = new URL(path, this.#base);
    if (url.origin !== this.#base.origin || !url.pathname.startsWith(this.#base.pathname)) throw new Error('飞轮请求越界');
    const signal = options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(300000)]) : AbortSignal.timeout(300000);
    const response = await this.#fetch(url, { ...options, redirect: 'error', signal, headers: { Accept: 'application/json', ...(this.#token ? { Authorization: 'Bearer ' + this.#token } : {}), ...options.headers } });
    if (!response.ok) throw new Error(`飞轮接口 ${response.status}；请检查地址、权限、令牌有效期和导出开关`);
    return response;
  }
  async projects(query = '') { return (await this.request('projects?' + new URLSearchParams({ q: query, limit: '100', offset: '0' }))).json(); }
  async sessions({ project = '', q = '', offset = 0 } = {}) { return (await this.request('sessions?' + new URLSearchParams({ project, q, limit: '100', offset: String(offset) }))).json(); }
  async download({ project, sessionId }, directory, { maxBytes = 4 * 1024 ** 3, signal } = {}) {
    if (!project || !sessionId) throw new Error('请选择项目和业务 Session');
    await mkdir(directory, { recursive: false, mode: 0o700 });
    const output = join(directory, 'export.tar.gz'), temporary = output + '.part';
    try {
      let response = await this.request('export-stages', { method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project, migbot_session_id: sessionId }) });
      if (/application\/json/i.test(response.headers.get('content-type') || '')) {
        const reply = await response.json();
        const target = new URL(reply.download_url, this.#base);
        if (target.origin === this.#base.origin) response = await this.request(target.href, { signal });
        else {
          if (target.protocol !== 'https:' || target.username || target.password) throw new Error('导出下载地址无效');
          // The presigned object receives no dashboard Authorization header.
          response = await this.#fetch(target, { redirect: 'error', signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(300000)]) : AbortSignal.timeout(300000) });
          if (!response.ok) throw new Error('飞轮对象下载失败：' + response.status);
        }
      }
      const stream = createWriteStream(temporary, { flags: 'wx', mode: 0o600 });
      const hash = createHash('sha256'); let bytes = 0;
      const counter = new Transform({ transform(chunk, _encoding, callback) { bytes += chunk.length; if (bytes > maxBytes) callback(new Error('飞轮导出超过本次下载上限')); else { hash.update(chunk); callback(null, chunk); } } });
      await pipeline(Readable.fromWeb(response.body), counter, stream, { signal });
      await rename(temporary, output);
      const receipt = { schemaVersion: 'hmbot.memory.flywheel-download/1', api: this.#base.href, project, migbotSessionId: sessionId, bytes, sha256: hash.digest('hex'), downloadedAt: new Date().toISOString(), archive: output };
      await writeJson(join(directory, 'download.json'), receipt); return receipt;
    } catch (error) { await rm(temporary, { force: true }); throw error; }
  }
}
