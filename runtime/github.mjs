import { readFile, lstat } from 'node:fs/promises';
import { join } from 'node:path';
import { run, readJson, tree, sha, writeJson } from './common.mjs';

const repoName = value => { if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(value || '')) throw new Error('GitHub 仓库需要 owner/repo'); return value; };
async function api(path, body, method) {
  const result = await run('gh', ['api', path, '--method', method || (body ? 'POST' : 'GET'), ...(body ? ['--input', '-'] : [])], { input: body ? JSON.stringify(body) : undefined, maxOutput: 16 * 1024 * 1024 });
  return JSON.parse(result.stdout);
}
export async function githubIdentity() { const value = await api('user'); return { login: value.login }; }
export async function createMemoryRepository(name) {
  repoName(name); const [owner, repository] = name.split('/');
  const { login } = await githubIdentity();
  if (owner !== login) throw new Error('新建仓库请使用当前 GitHub 账号；组织仓库请先自行创建');
  try { const result = await api('repos/' + name); if (!result.private) throw new Error('Memory 仓库需要为私有'); return { repository: name, url: result.html_url }; }
  catch (error) { if (!String(error).includes('404')) throw error; }
  await api('user/repos', { name: repository, private: true, auto_init: true, description: 'Versioned Memory RSI reading packs, evidence cards and evaluation summaries. No raw sessions.' });
  return { repository: name, url: 'https://github.com/' + name };
}

export async function publicationPreview(runDirectory, { repository, mode = 'candidate' }) {
  repoName(repository);
  const report = await readJson(join(runDirectory, 'experiment.json'));
  const runPath = report.distillation?.run;
  if (!runPath) throw new Error('本轮尚未完成 Memory 制备');
  const delivery = await readJson(join(runPath, 'delivery-check.json'));
  const current = await readJson(join(runPath, 'store/HEAD.json'));
  if (delivery.revision !== current.revision) throw new Error('Memory 在交付检查后发生变化');
  if (!['candidate', 'validated'].includes(mode)) throw new Error('未知发布状态');
  const l1Valid = report.l1?.accepted && report.l1.memoryRevision === current.revision && report.l1.suiteSha256 === report.l1Prepared?.suiteSha256 || report.l1Review?.accepted && report.l1Review.memoryRevision === current.revision && report.l1Review.suiteSha256 === report.l1Prepared?.suiteSha256;
  if (mode === 'validated' && (!l1Valid || !report.l2?.accepted || report.l2MemoryRevision !== current.revision || !report.l2Prepared || report.l2.suiteSha256 !== report.l2Prepared.suiteSha256)) throw new Error('已验证版本需要本 revision 的 L1 完整通过／人工复核及 L2 严格改善');
  const validationTarget = report.config?.validationTarget || 'content';
  const l3Valid = report.l3?.accepted && report.l3.memoryRevision === current.revision && report.l3MemoryRevision === current.revision && report.l3.suiteSha256 === report.l3Prepared?.suiteSha256;
  if (mode === 'validated' && validationTarget === 'agent-replay' && !l3Valid) throw new Error('本轮主动选择了 Agent 回放验证目标，还需要本 revision 的 L3 完整通过；仍可上传候选');
  const l3State = l3Valid ? 'passed' : !report.l3Prepared ? 'not-configured' : !report.l3 ? 'not-run' : !report.l3.complete ? 'inconclusive' : !report.l3.processReviewComplete ? 'needs-review' : 'failed';
  const revision = current.revision, files = {};
  for (const folder of ['memory', 'store']) {
    const root = join(runPath, folder), frozen = await tree(root, { limit: 32 * 1024 * 1024 });
    for (const [name, hash] of Object.entries(frozen.files)) {
      if (!/\.(md|json)$/.test(name)) throw new Error('Memory 发布目录含非预期文件：' + name);
      if (folder === 'store' && !/^(?:HEAD\.json|snapshots\/[a-f0-9]{64}\.json|cases\/case-[a-f0-9]+\/[a-f0-9]{64}\.json|sessions\/.+\.json)$/.test(name)) throw new Error('store 中存在非版本化 Memory 文件：' + name);
      const bytes = await readFile(join(root, name));
      files[folder + '/' + name] = { sha256: hash, bytes: bytes.length };
    }
  }
  const summary = { schemaVersion: 'hmbot.memory.publication/1', memoryRevision: revision, mode, validationTarget, validationScope: validationTarget === 'content' ? 'L1 + L2 content transfer' : 'L1 + L2 + L3 historical agent replay', l3State, source: report.source?.identity || report.source?.receipt && { project: report.source.receipt.project, migbotSessionId: report.source.receipt.migbotSessionId, sha256: report.source.receipt.sha256 }, l1: report.l1 && { accepted: report.l1.accepted, verdict: report.l1.verdict, suiteSha256: report.l1.suiteSha256 }, l1Review: report.l1Review, l2: report.l2, l3: report.l3, runtime: report.runtime, sourceManifestSha256: report.source?.manifestSha256 };
  const summaryBytes = Buffer.from(JSON.stringify(summary, null, 2) + '\n');
  files['evaluation/summary.json'] = { sha256: sha(summaryBytes), bytes: summaryBytes.length };
  const preview = { repository, revision, mode, prefix: 'revisions/' + revision, files, summary, filesSha256: sha(JSON.stringify(files)) };
  return preview;
}

export async function publishMemory(runDirectory, options) {
  const preview = await publicationPreview(runDirectory, options);
  if (options.filesSha256 !== preview.filesSha256) throw new Error('上传预览已改变，请重新查看');
  const metadata = await api('repos/' + preview.repository);
  if (!metadata.private) throw new Error('当前版本只上传到私有 Memory 仓库');
  const branch = metadata.default_branch;
  const head = await api(`repos/${preview.repository}/git/ref/heads/${branch}`);
  const parent = await api(`repos/${preview.repository}/git/commits/${head.object.sha}`);
  const report = await readJson(join(runDirectory, 'experiment.json'));
  const entries = [];
  for (const [name, item] of Object.entries(preview.files)) {
    const bytes = name === 'evaluation/summary.json' ? Buffer.from(JSON.stringify(preview.summary, null, 2) + '\n') : await readFile(join(report.distillation.run, name));
    if (sha(bytes) !== item.sha256) throw new Error('待上传 Memory 改变');
    const blob = await api(`repos/${preview.repository}/git/blobs`, { content: bytes.toString('base64'), encoding: 'base64' });
    entries.push({ path: preview.prefix + '/' + name, mode: '100644', type: 'blob', sha: blob.sha });
  }
  const latest = await api(`repos/${preview.repository}/git/blobs`, { content: JSON.stringify({ revision: preview.revision, mode: preview.mode, validationTarget: preview.summary.validationTarget, validationScope: preview.summary.validationScope, path: preview.prefix, filesSha256: preview.filesSha256 }), encoding: 'utf-8' });
  entries.push({ path: preview.mode === 'validated' ? 'LATEST.json' : 'CANDIDATE.json', mode: '100644', type: 'blob', sha: latest.sha });
  const treeResult = await api(`repos/${preview.repository}/git/trees`, { base_tree: parent.tree.sha, tree: entries });
  const commit = await api(`repos/${preview.repository}/git/commits`, { message: `memory: ${preview.mode} ${preview.revision.slice(0, 12)}`, tree: treeResult.sha, parents: [head.object.sha] });
  await api(`repos/${preview.repository}/git/refs/heads/${branch}`, { sha: commit.sha, force: false }, 'PATCH');
  const receipt = { repository: preview.repository, commit: commit.sha, revision: preview.revision, mode: preview.mode, filesSha256: preview.filesSha256, url: `https://github.com/${preview.repository}/tree/${commit.sha}/${preview.prefix}` };
  await writeJson(join(runDirectory, 'publication.json'), receipt); return receipt;
}
