import { mkdir, access, readdir, readFile } from 'node:fs/promises';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { run, readJson, writeJson, sha } from './common.mjs';

export async function setup(home, { dshCli = process.env.HMBOT_MEMORY_DSH_CLI, sources = {}, progress = () => {} } = {}) {
  const lock = await readJson(new URL('./dependencies.json', import.meta.url));
  await mkdir(join(home, 'source'), { recursive: true, mode: 0o700 });
  let systemPython;
  for (const name of [process.env.HMBOT_RSI_PYTHON, 'python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3'].filter(Boolean)) {
    try { const { stdout, stderr } = await run(name, ['--version']); const match = (stdout + stderr).match(/Python (\d+)\.(\d+)/); if (match && (+match[1] > 3 || +match[1] === 3 && +match[2] >= 10)) { systemPython = name; break; } } catch {}
  }
  if (!systemPython) throw new Error('需要 Python 3.10+，可通过 HMBOT_RSI_PYTHON 指定');
  const python = join(home, 'python', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  try { await access(python); } catch { progress('创建 Python 环境'); await run(systemPython, ['-m', 'venv', join(home, 'python')]); }
  try { await run(python, ['-c', 'import yaml; assert yaml.__version__ == "6.0.3"']); }
  catch { await run(python, ['-m', 'pip', 'install', '--disable-pip-version-check', 'PyYAML==6.0.3']); }
  async function checkout(key) {
    const spec = lock[key], directory = resolve(sources[key] || join(home, 'source', key));
    try { await access(join(directory, '.git')); }
    catch { progress('下载 ' + spec.repository); await run('gh', ['repo', 'clone', spec.repository, directory, '--', '--depth=1', ...(key === 'migloop' ? ['--branch', 'feat/dsh-adapter'] : [])]); }
    let commit = (await run('git', ['-C', directory, 'rev-parse', 'HEAD'])).stdout.trim();
    if (spec.commit && commit !== spec.commit) {
      if (sources[key]) throw new Error(key + ' 源码不是固定提交');
      await run('git', ['-C', directory, 'fetch', '--depth=1', 'origin', spec.commit]); await run('git', ['-C', directory, 'checkout', '--detach', spec.commit]); commit = spec.commit;
    }
    const root = (await run('git', ['-C', directory, 'rev-parse', '--show-toplevel'])).stdout.trim();
    if (resolve(root) !== directory || (await run('git', ['-C', directory, 'status', '--porcelain'])).stdout.trim()) throw new Error(key + ' 需要干净的独立 Git checkout');
    return { directory, commit, repository: spec.repository };
  }
  const migloop = await checkout('migloop'), evaluation = await checkout('evaluation');
  const engine = join(home, 'engine', migloop.commit); await mkdir(engine, { recursive: true });
  const skills = join(engine, 'skills'), pyz = join(engine, 'migloop-lineage.pyz');
  try { await access(skills); } catch { progress('构建 Memory skills'); await run(python, [join(migloop.directory, 'skills/migloop-memory-maintain/scripts/build_bundle.py'), '--out', skills]); }
  await run(python, [join(migloop.directory, 'scripts/build_pyz.py'), '-o', pyz]);
  if (!dshCli) {
    const runtime = join(home, 'runtime'); progress('准备 DSH ' + lock.dsh);
    await run('npm', ['install', '--prefix', runtime, '--ignore-scripts', '--no-audit', '--no-fund', '--save-exact', '@deepseek-ai/dsh@' + lock.dsh, 'pnpm@10.32.1'], { timeout: 1200000 });
    dshCli = join(runtime, 'node_modules/@deepseek-ai/dsh/lib/bin.js');
  }
  dshCli = resolve(dshCli);
  const dshPackage = await readJson(join(dirname(dshCli), '..', 'package.json'));
  if (dshPackage.version !== lock.dsh) throw new Error('DSH 版本不匹配，需要 ' + lock.dsh);
  const fixchain = await checkout('fixchain'), viewer = await checkout('viewer');
  // A separate headless profile owns only these experiment sessions.
  const profile = join(home, 'dsh/profiles/headless'); await mkdir(profile, { recursive: true });
  await writeJson(join(profile, 'package.json'), { name: 'hmbot-memory-headless', private: true, dsh: { profile: { bundles: ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless'] } } });
  const fingerprint = {};
  for (const name of await readdir(skills)) {
    const path = join(skills, name, 'package-manifest.json'); try { fingerprint[name] = sha(await readFile(path)); } catch {}
  }
  const state = { schemaVersion: 'hmbot.memory.runtime/1', python, dshCli, dsh: lock.dsh, migloop, evaluation, fixchain, viewer, engine, skills, pyz, packages: fingerprint, installedAt: new Date().toISOString(), pluginRoot: resolve(fileURLToPath(new URL('..', import.meta.url))) };
  await writeJson(join(home, 'runtime.json'), state); return state;
}
