import { spawn } from 'node:child_process';
import { mkdir, readFile, writeFile, rename, lstat, readdir } from 'node:fs/promises';
import { join, resolve, relative, isAbsolute } from 'node:path';
import { createHash } from 'node:crypto';
import { homedir } from 'node:os';

export const sha = bytes => createHash('sha256').update(bytes).digest('hex');
export const homePath = () => resolve(process.env.HMBOT_MEMORY_HOME || join(homedir(), '.hmbot-memory-rsi'));
export const readJson = async path => JSON.parse(await readFile(path, 'utf8'));
export async function writeJson(path, value) {
  await mkdir(resolve(path, '..'), { recursive: true, mode: 0o700 });
  await writeFile(path + '.tmp', JSON.stringify(value, null, 2) + '\n', { mode: 0o600 });
  await rename(path + '.tmp', path);
}
export function inside(root, path) {
  const rel = relative(resolve(root), resolve(path));
  return !isAbsolute(rel) && rel !== '..' && !rel.startsWith('../');
}
export const safeError = error => String(error?.message || error).replace(/(?:sk-|gh[pousr]_|github_pat_)\S+|Bearer\s+\S+/gi, '[REDACTED]').slice(-2000);
export function run(binary, args, { cwd, env = {}, signal, timeout = 600000, log, onOutput, maxOutput = 4 * 1024 * 1024, input, processGroup = false } = {}) {
  return new Promise((done, fail) => {
    const detached = processGroup && process.platform !== 'win32';
    const child = spawn(binary, args, { cwd, env: { ...process.env, ...env }, signal, detached, windowsHide: true, stdio: [input === undefined ? 'ignore' : 'pipe', 'pipe', 'pipe'] });
    let stdout = '', stderr = '', size = 0, streamError;
    const stop = () => { try { if (detached && child.pid) process.kill(-child.pid, 'SIGTERM'); else child.kill('SIGTERM'); } catch {} };
    const onError = error => { streamError ||= error; stop(); };
    if (input !== undefined) { child.stdin.on('error', onError); child.stdin.end(input); }
    log?.on('error', onError);
    const timer = setTimeout(stop, timeout);
    signal?.addEventListener('abort', stop, { once: true });
    child.stdout.on('data', bytes => { size += bytes.length; try { onOutput?.(bytes); if (log) log.write(bytes); } catch (error) { onError(error); } stdout = (stdout + bytes).slice(-maxOutput); });
    child.stderr.on('data', bytes => { if (log) log.write(bytes); stderr = (stderr + bytes).slice(-4000); });
    child.once('error', error => { clearTimeout(timer); signal?.removeEventListener('abort', stop); fail(error); });
    child.once('close', code => { clearTimeout(timer); signal?.removeEventListener('abort', stop); streamError ? fail(streamError) : code === 0 ? done({ stdout, stderr, bytes: size }) : fail(new Error(safeError(stderr || stdout || `${binary} exited ${code}`))); });
  });
}
export async function tree(root, { limit = 128 * 1024 * 1024 } = {}) {
  const files = {}; let size = 0;
  async function visit(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name), info = await lstat(path);
      if (info.isSymbolicLink()) throw new Error('不接受符号链接：' + path);
      if (info.isDirectory()) await visit(path);
      else if (info.isFile()) { size += info.size; if (size > limit || Object.keys(files).length >= 20000) throw new Error('目录超过快照上限'); files[relative(root, path)] = sha(await readFile(path)); }
      else throw new Error('不接受特殊文件');
    }
  }
  await visit(root); return { files, bytes: size, sha256: sha(JSON.stringify(files)) };
}
