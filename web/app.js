const $ = id => document.getElementById(id);
const value = id => $(id).value.trim();
const lines = id => value(id).split(/\r?\n/).map(s => s.trim()).filter(Boolean);
let token, selected, current, preview, reviewPacket, offset = 0, lastOperation, viewMode = 'report';
const json = item => JSON.stringify(item, null, 2);
function notice(text, error = false) { $('notice').textContent = text; $('notice').className = error ? 'error' : ''; $('notice').style.display = 'block'; }
async function api(path, body) {
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers: { 'x-memory-token': token || '', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const result = await response.json(); if (!response.ok) throw new Error(result.error || '请求失败'); return result;
}
function choices(id, rows, key, label, placeholder) {
  const element = $(id), before = element.value; element.replaceChildren();
  if (!rows.length) { const option = new Option(placeholder || '暂无数据', ''); element.add(option); }
  for (const row of rows) element.add(new Option(label(row), String(key(row))));
  if ([...element.options].some(o => o.value === before)) element.value = before;
}
async function refresh() {
  const state = await api('/api/status');
  choices('run', state.runs, r => r.id, r => r.title + ' · ' + new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short' }).format(new Date(r.createdAt)) + ' 北京时间', '尚无实验');
  if (!selected && state.runs.length) selected = state.runs[0].id;
  if (selected) $('run').value = selected;
  current = state.runs.find(r => r.id === selected);
  if (viewMode === 'report') $('report').textContent = json(current || state.operation || { status: '等待来源' });
  $('run-status').textContent = current?.status || '等待来源';
  document.body.classList.toggle('busy', state.operation?.status === 'running');
  if (current?.l1Prepared && current?.l2Prepared) $('evaluation-budget').textContent = 'L1：' + current.l1Prepared.requests + ' 次；L2：' + current.l2Prepared.tasks * current.configuredRuns * 2 + ' 次（详细参数见本轮记录与冻结 suite）。';
  if (state.operation && state.operation.status !== 'running') {
    const signature = json(state.operation);
    if (lastOperation !== signature) notice(state.operation.error || '已完成：' + state.operation.label, state.operation.status === 'failed');
    lastOperation = signature;
  }
  return state;
}
function requireRun() { if (!selected) throw new Error('先选择或冻结一个实验'); return selected; }
async function action(name, body = {}) { preview = null; $('upload').disabled = true; const response = await api('/api/runs/' + requireRun() + '/' + name, body); notice(response.accepted ? '阶段已开始，右侧记录会自动更新。' : '已完成'); await refresh(); return response; }
function on(id, callback, isAction = true) {
  if (isAction) $(id).dataset.action = 'true';
  $(id).addEventListener('click', async () => {
    const button = $(id); button.disabled = true;
    try { await callback(); } catch (error) { notice(error.message, true); }
    finally { if (id !== 'upload') button.disabled = false; }
  });
}
on('connect', async () => { await api('/api/flywheel/connect', { baseUrl: value('flywheel-url'), token: value('flywheel-token') }); $('flywheel-token').value = ''; const result = await api('/api/flywheel/projects'); choices('project', result.rows || [], row => row.project, row => row.project + ' · ' + (row.doc_total || 0) + ' 条记录', '当前账号没有可见项目'); notice('已连接。请选择项目并读取会话。'); });
on('disconnect', async () => { await api('/api/flywheel/disconnect', {}); choices('project', [], () => '', () => '', '已断开'); notice('已断开，访问令牌已从连接中移除。'); });
async function sessions(next = false) {
  offset = next ? offset + 100 : 0;
  const result = await api('/api/flywheel/sessions?' + new URLSearchParams({ project: value('project'), q: value('session-query'), offset: String(offset) }));
  choices('session', result.sessions || [], row => row.migbot_session_id || row.session_id || row.id, row => [row.android_app_name || row.project, row.android_version || row.version, row.migbot_session_id || row.session_id || row.id].filter(Boolean).join(' · '), '此页无会话');
  notice('本页 ' + (result.sessions || []).length + ' 条，共 ' + (result.total ?? '未知') + ' 条。');
}
on('sessions', () => sessions(false)); on('sessions-more', () => sessions(true));
$('source-kind').addEventListener('change', () => { $('flywheel-picker').hidden = value('source-kind') !== 'flywheel'; $('offline-picker').hidden = value('source-kind') === 'flywheel'; });
on('create', async () => {
  const source = value('source-kind') === 'flywheel' ? { kind: 'flywheel', project: value('project'), sessionId: value('session') } : { kind: value('source-kind'), path: value('source-path'), format: value('source-format'), sessions: lines('dsh-sessions') };
  const history = value('checkpoint-repo') ? { checkpointRepository: value('checkpoint-repo'), generationRef: value('generation-ref'), finalRef: value('final-ref') } : {};
  await api('/api/runs', { title: value('title'), app: value('title'), source, baseStore: value('base-store') || undefined, authorModel: value('author-model'), requestBudget: Number(value('request-budget')), scope: value('scope'), maxIssues: 1, history });
  selected = null; notice('正在核验并冻结来源。'); await refresh();
});
on('credentials', async () => { const result = await api('/api/credentials', { modelKey: value('model-key'), judgeKey: value('judge-key') }); $('model-key').value = ''; $('judge-key').value = ''; notice(result.configured ? '凭据已设置，仅在本地进程内保存。' : '已清除会话凭据。'); });
on('distill', () => action('distill')); on('check', () => action('check')); on('author-tasks', () => action('tasks'));
on('prepare', async () => {
  const body = { ...(value('tasks-path') ? { tasks: value('tasks-path'), project: value('reference-path'), baseline: value('baseline-path') } : {}), model: value('eval-model'), runs: Number(value('runs')), baseUrl: value('eval-endpoint'), judgeModel: value('judge-model'), judgeBaseUrl: value('judge-endpoint'), factQueries: lines('fact-queries') };
  await action('prepare-evaluation', body);
});
on('l1', () => action('l1')); on('l2', () => action('l2'));
on('review-packet', async () => {
  reviewPacket = await api('/api/runs/' + requireRun() + '/review-packet');
  viewMode = 'review'; $('report').textContent = json(reviewPacket);
  const decisions = reviewPacket.suite.items.flatMap(item => Array.from({ length: 8 }, (_, index) => ({ lesson: item.id, rule: 'R' + (index + 1), verdict: 'pending', lessonQuote: '', evidenceQuote: '', reason: '' })));
  $('review-decisions').value = json(decisions); $('review-area').open = true; notice('右侧为冻结证据。请逐条填写复核，模板未预填为通过。');
});
on('review', async () => {
  if (!reviewPacket) throw new Error('先读取本轮复核材料');
  await action('review', { suiteSha256: current.l1Prepared.suiteSha256, reviewer: value('reviewer'), reason: value('review-reason'), decisions: JSON.parse(value('review-decisions')) });
});
on('create-repo', async () => { const result = await api('/api/github/create', { repository: value('repository') }); notice('私有仓库已准备：' + result.url); });
on('preview-upload', async () => {
  preview = await api('/api/runs/' + requireRun() + '/publication-preview', { repository: value('repository'), mode: value('publish-mode') });
  $('upload-preview').hidden = false; $('upload-preview').textContent = json(preview); $('upload').disabled = false; notice('请核对精确文件列表、状态与目标仓库，再点击上传。');
});
on('upload', async () => {
  if (!preview || preview.repository !== value('repository') || preview.mode !== value('publish-mode')) throw new Error('目标改变，请重新预览');
  await api('/api/runs/' + requireRun() + '/publish', { repository: preview.repository, mode: preview.mode, filesSha256: preview.filesSha256 }); preview = null; $('upload').disabled = true; notice('正在向私有 GitHub 仓库提交预览版本。');
});
$('repository').addEventListener('input', () => { preview = null; $('upload').disabled = true; });
$('publish-mode').addEventListener('change', () => { preview = null; $('upload').disabled = true; });
$('run').addEventListener('change', () => { selected = value('run'); viewMode = 'report'; current = null; preview = null; $('upload').disabled = true; refresh().catch(error => notice(error.message, true)); });
on('refresh', async () => { viewMode = 'report'; await refresh(); }, false); on('cancel', async () => { await api('/api/cancel', {}); notice('已请求停止当前阶段；已完成的证据保留。'); }, false);
on('download-report', () => { if (!current) throw new Error('请选择实验'); const url = URL.createObjectURL(new Blob([json(current) + '\n'], { type: 'application/json' })); const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'memory-experiment-' + current.id + '.json'; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }, false);
const boot = await api('/api/bootstrap'); token = boot.token; $('runtime').textContent = boot.runtimeReady ? 'DSH ' + boot.runtime.dsh + ' · 本地环境就绪' : '先运行 memory-rsi setup';
if (!boot.runtimeReady) notice('运行包尚未准备。请在解压目录运行 node bin/studio.mjs setup，然后刷新。', true);
try { const identity = await api('/api/github'); $('repository').value = identity.login + '/memory-rsi-memories'; $('reviewer').value = identity.login; } catch {}
await refresh();
setInterval(() => refresh().catch(error => notice(error.message, true)), 2500);
on('load-sources', async () => { const files = await api('/api/runs/' + requireRun() + '/sources'); choices('trajectory-source', files, item => item.file, item => item.file + ' · ' + item.bytes + ' bytes'); });
on('render-trajectory', () => action('view', { file: value('trajectory-source') }));
on('open-trajectory', async () => { const result = await api('/api/runs/' + requireRun() + '/open-trajectory', {}); $('trajectory-frame').removeAttribute('srcdoc'); $('trajectory-frame').src = result.url; $('trajectory-frame').hidden = false; });
