import { mkdir, readFile, writeFile, cp, access, readdir } from 'node:fs/promises';
import { createWriteStream } from 'node:fs';
import { join, resolve, dirname, delimiter } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { run, readJson, writeJson, sha, safeError, inside } from './common.mjs';
import { invokeMemoryEvaluation } from '../index.js';
import { publicationPreview, publishMemory } from './github.mjs';

const here = dirname(fileURLToPath(import.meta.url));
export class MemoryPipeline {
  constructor(home) { this.home = home; this.active = null; this.credentials = {}; }
  async runtime() { return readJson(join(this.home, 'runtime.json')); }
  setCredentials({ modelKey, judgeKey }) { this.credentials = { modelKey, judgeKey: judgeKey || modelKey }; return { configured: Boolean(modelKey), persisted: false }; }
  async list() {
    let entries; try { const { readdir } = await import('node:fs/promises'); entries = await readdir(join(this.home, 'runs')); } catch { return []; }
    const results = [];
    for (const id of entries) if (/^[a-f0-9-]{36}$/.test(id)) { try { results.push(await this.get(id)); } catch {} }
    return results.sort((a, b) => b.createdAt.localeCompare(a.createdAt));
  }
  directory(id) { if (!/^[a-f0-9-]{36}$/.test(id)) throw new Error('未知实验'); return join(this.home, 'runs', id); }
  async get(id) { return readJson(join(this.directory(id), 'experiment.json')); }
  async save(report) { await writeJson(join(this.directory(report.id), 'experiment.json'), report); }
  async create(config, client) {
    if (this.active) throw new Error('请等待当前阶段结束');
    if (!config.title?.trim()) throw new Error('请输入实验名称');
    // Secrets are accepted only by setCredentials / the connection object.
    if (/"(?:token|password|apiKey|modelKey|judgeKey)"\s*:/i.test(JSON.stringify(config))) throw new Error('实验配置不能包含凭据');
    const rt = await this.runtime(), id = randomUUID(), root = this.directory(id);
    await mkdir(join(this.home, 'runs'), { recursive: true, mode: 0o700 });
    await mkdir(root, { recursive: false, mode: 0o700 });
    const report = { schemaVersion: 'hmbot.memory.experiment/2', id, title: config.title.trim(), createdAt: new Date().toISOString(), status: 'preparing-source', config, runtime: { dsh: rt.dsh, migloop: rt.migloop.commit, evaluation: rt.evaluation.commit, fixchain: rt.fixchain.commit, viewer: rt.viewer.commit }, stages: [] };
    await this.save(report);
    const abort = new AbortController(); this.active = { id, stage: 'source', abort };
    try {
      let materials, format;
      if (config.source.kind === 'flywheel') {
        if (!client) throw new Error('请先连接数据飞轮');
        const receipt = await client.download(config.source, join(root, 'download'), { signal: abort.signal });
        const result = await run(rt.python, [join(here, 'flywheel.py'), 'freeze', '--source', receipt.archive, '--sha256', receipt.sha256, '--out', join(root, 'source'), '--summary'], { signal: abort.signal });
        const source = JSON.parse(result.stdout);
        if (!source.eligible) throw new Error('飞轮导出有缺片或附件缺失，请补齐后使用');
        materials = source.root; format = 'a2h-export/4'; report.source = { ...source, receipt };
        if (source.project !== config.source.project || source.migbotSessionId !== config.source.sessionId) throw new Error('飞轮返回了与所选项目／Session 不一致的导出');
      } else if (config.source.kind === 'export') {
        const result = await run(rt.python, [join(here, 'flywheel.py'), 'freeze', '--source', resolve(config.source.path), '--out', join(root, 'source'), '--summary'], { signal: abort.signal });
        const source = JSON.parse(result.stdout); if (!source.eligible) throw new Error('离线导出有缺片');
        materials = source.root; format = 'a2h-export/4'; report.source = source;
      } else if (config.source.kind === 'directory') {
        materials = resolve(config.source.path); format = config.source.format;
        if (!inside(root, materials) && !inside(materials, root)) await access(materials); else throw new Error('材料不能与实验输出重叠');
        report.source = { identity: { kind: 'directory', path: materials, format } };
      } else throw new Error('请选择飞轮或离线来源');
      const prepareConfig = join(root, 'distill-input.json');
      await writeJson(prepareConfig, { materials, format, app: config.app || config.title, serverSessionId: config.source.sessionId, baseStore: config.baseStore || undefined, sessions: config.source.sessions || [], scope: config.scope || '', maxIssues: config.maxIssues || 1, ...config.history });
      const { prepareOffline } = await import(pathToFileURL(join(rt.fixchain.directory, 'lib/offline.js')));
      const prepared = await prepareOffline(prepareConfig, join(root, 'distillation'), { runtime: join(this.home, 'runtime.json'), signal: abort.signal });
      report.distillation = { run: prepared.prepared, initialRevision: prepared.start_revision }; report.status = 'source-ready';
      report.stages.push({ stage: 'source', status: 'completed', at: new Date().toISOString() }); await this.save(report); return report;
    } catch (error) { report.status = abort.signal.aborted ? 'cancelled' : 'failed'; report.error = safeError(error); await this.save(report); throw error; }
    finally { this.active = null; }
  }
  async stage(id, name, action) {
    if (this.active) throw new Error('当前已有阶段运行');
    const report = await this.get(id), abort = new AbortController();
    this.active = { id, stage: name, abort }; report.status = name + '-running'; await this.save(report);
    try { const value = await action(report, await this.runtime(), abort.signal); report.stages.push({ stage: name, status: 'completed', at: new Date().toISOString() }); report.status = name + '-completed'; report.error = undefined; await this.save(report); return value; }
    catch (error) { report.status = abort.signal.aborted ? 'cancelled' : 'needs-attention'; report.error = safeError(error); report.stages.push({ stage: name, status: report.status, at: new Date().toISOString() }); await this.save(report); throw error; }
    finally { this.active = null; }
  }
  async worker(report, rt, directory, prompt, label, signal) {
    const key = this.credentials.modelKey || process.env.DEEPSEEK_API_KEY;
    if (!key) throw new Error('请在本地控制台填写模型凭据');
    report.usage ||= {};
    const totalLimit = label === 'task-author' ? 20 : report.config.requestBudget || 30;
    const remaining = totalLimit - (report.usage[label] || 0);
    if (remaining <= 0) throw new Error('本阶段已用完冻结的请求预算；不会隐式重置预算');
    const attempt = (await readdir(directory)).filter(name => name.startsWith(label + '-') && name.endsWith('.jsonl')).length + 1;
    const prefix = label + '-' + attempt, patch = join(directory, prefix + '.patch.json'), budget = join(this.directory(report.id), prefix + '-budget.json');
    await writeJson(patch, [{ id: 'agent-default-model', config: { provider: 'deepseek-official', model: report.config.authorModel || 'deepseek-flash' } }, { id: 'session-title-llm', disabled: true }, { id: 'llm-retry', disabled: true }, { insert: [{ id: 'memory-request-budget', name: join(here, 'budget.mjs'), config: { requests: remaining, output: budget } }] }]);
    const context = join(directory, 'analysis-runtime.json');
    let previous = {}; try { previous = await readJson(context); } catch {}
    const analysis = { schema: 'migloop-analysis-runtime/1', materials: join(report.distillation.run, 'materials'), analysis: { platforms: ['dsh'], models: [report.config.authorModel || 'deepseek-flash'], root_session_ids: previous.analysis?.root_session_ids || [] } };
    await writeJson(context, analysis);
    const log = createWriteStream(join(directory, prefix + '.jsonl'), { flags: 'wx', mode: 0o600 });
    let buffer = '', contextChanged = false;
    const onOutput = bytes => { buffer += bytes; let at; while ((at = buffer.indexOf('\n')) >= 0) { const line = buffer.slice(0, at); buffer = buffer.slice(at + 1); try { const event = JSON.parse(line); if (event.type === 'session' && event.sessionId && !analysis.analysis.root_session_ids.includes(event.sessionId)) { analysis.analysis.root_session_ids.push(event.sessionId); contextChanged = true; } } catch {} } };
    try { await run(process.execPath, [rt.dshCli, '--profile', 'headless', '--patch', patch, '--json', '--approval-policy', 'never', prompt], { cwd: directory, signal, processGroup: true, onOutput, timeout: (report.config.timeoutSeconds || 1800) * 1000, log, env: { PATH: resolve(dirname(rt.dshCli), '../../../.bin') + delimiter + process.env.PATH, DSH_HOME: join(this.home, 'dsh'), DSH_TELEMETRY_DISABLED: '1', DEEPSEEK_API_KEY: key, HMBOT_MEMORY_RUNTIME: join(this.home, 'runtime.json'), MIGLOOP_ANALYSIS_CONTEXT: context, PYTHONDONTWRITEBYTECODE: '1' } }); }
    finally { await new Promise((resolve, reject) => { log.end(resolve); log.once('error', reject); }); if (contextChanged) await writeJson(context, analysis); try { report.usage[label] = (report.usage[label] || 0) + (await readJson(budget)).requests; } catch {} }
  }
  async distill(id) {
    return this.stage(id, 'distill', async (report, rt, signal) => {
      if (report.l1Prepared || report.l2Prepared) throw new Error('评测已冻结；继续优化请以当前 store 为基线新建实验');
      const directory = report.distillation.run;
      await this.worker(report, rt, directory, 'Read CLAUDE.md and job.json. Follow the exact session-to-memory skill; use independent subagents for cards. Deliver the versioned memory reading pack to memory/index.md and completion.json. Do not author evaluation tasks in this stage.', 'distill-worker', signal);
      const { checkOffline } = await import(pathToFileURL(join(rt.fixchain.directory, 'lib/offline.js')));
      const delivery = await checkOffline(directory, { runtime: join(this.home, 'runtime.json'), signal });
      report.memory = { revision: delivery.revision, path: dirname(delivery.index) }; return delivery;
    });
  }
  async checkDelivery(id) {
    return this.stage(id, 'delivery-check', async (report, rt) => {
      const { checkOffline } = await import(pathToFileURL(join(rt.fixchain.directory, 'lib/offline.js')));
      const delivery = await checkOffline(report.distillation.run, { runtime: join(this.home, 'runtime.json') });
      report.memory = { revision: delivery.revision, path: dirname(delivery.index) }; return delivery;
    });
  }
  async sources(id) {
    const report = await this.get(id);
    const manifest = await readJson(join(report.distillation.run, 'materials-manifest.json'));
    return manifest.files.filter(file => file.file.endsWith('.jsonl'));
  }
  async view(id, { file }) {
    return this.stage(id, 'trajectory', async (report, rt, signal) => {
      const files = await this.sources(id);
      const selected = files.find(item => item.file === file);
      if (!selected) throw new Error('请选择本轮已冻结的原生轨迹');
      const input = selected.originalFile ? resolve(report.distillation.run, selected.originalFile) : join(report.distillation.run, 'materials', file);
      if (!inside(report.distillation.run, input)) throw new Error('轨迹来源越界');
      const { renderOffline } = await import(pathToFileURL(join(rt.viewer.directory, 'lib/offline.js')));
      const result = await renderOffline(input, join(this.directory(id), 'trajectory-' + Date.now() + '.html'), { runtime: join(this.home, 'runtime.json'), signal });
      report.trajectory = result; return result;
    });
  }
  async authorTasks(id) {
    return this.stage(id, 'author-tasks', async (report, rt, signal) => {
      if (!report.memory) throw new Error('先完成制卡和交付检查');
      if (report.l2Prepared) throw new Error('题集已冻结；请新建实验后重新出题');
      const directory = join(this.directory(id), 'eval-input'); await mkdir(directory, { recursive: true });
      await cp(join(rt.evaluation.directory, 'l2/TASK-SCHEMA.md'), join(directory, 'TASK-SCHEMA.md'));
      await writeJson(join(directory, 'source.json'), { materials: join(report.distillation.run, 'materials'), cards: join(report.distillation.run, 'cards'), memory: report.memory.path, job: join(report.distillation.run, 'job.json') });
      await writeFile(join(directory, 'CLAUDE.md'), 'Author L2 content-transfer tasks from the frozen source.json paths, following TASK-SCHEMA.md. Each task must derive from one actual historical dispatch. Extract the exact historical failure into baseline/, the retained correct reference into reference/, and the original inputs into reference/. Save YAML/JSON tasks under tasks/ with calibrated contains/regex assertions: EVERY assertion fails the historical failure and passes the reference. Explicitly include cases, lessons and baselineLessons: []. Do not fabricate historical inputs or use current SDK declarations as historical ones. Do not run a model, change Memory, or inspect future L2 answers. Explain all source citations and limitations in AUTHORING.md. Use relative project paths. If evidence is missing, stop and report the missing evidence. No full-app or generalization claims.\n');
      await this.worker(report, rt, directory, 'Read CLAUDE.md, source.json and TASK-SCHEMA.md, then author at most ONE frozen L2 task for this pilot. Do not print or transmit complete source archives; read only relevant evidence spans.', 'task-author', signal);
      report.taskInputs = { tasks: join(directory, 'tasks'), project: join(directory, 'reference'), baseline: join(directory, 'baseline') }; return report.taskInputs;
    });
  }
  async prepareEvaluation(id, options) {
    return this.stage(id, 'prepare-evaluation', async (report, rt) => {
      if (!report.memory) throw new Error('先完成交付检查');
      if (!options.judgeModel?.trim() || options.judgeModel === (report.config.authorModel || 'deepseek-flash')) throw new Error('请先填写与作者不同的判官模型');
      const root = this.directory(id), inputs = options.tasks ? options : report.taskInputs;
      if (!inputs) throw new Error('请生成题集或选择已有校准题集');
      const l2 = { ...inputs, upstream: rt.evaluation.directory, memory: report.memory.path, baselineMemory: options.baselineMemory || undefined, model: options.model || report.config.authorModel || 'deepseek-flash', baseUrl: options.baseUrl || 'https://api.deepseek.com/anthropic', runs: options.runs || 3, maxTokens: options.maxTokens || 8192, temperature: 0.2, minValid: 1, maxRequests: 6 };
      report.configuredRuns = l2.runs;
      const l2ConfigSha256 = sha(JSON.stringify(l2));
      if (report.l2Prepared && report.l2ConfigSha256 !== l2ConfigSha256) throw new Error('L2 已冻结，修改输入或预算请新建实验');
      if (!report.l2Prepared) {
        await writeJson(join(root, 'l2-config.json'), l2);
        report.l2Prepared = await invokeMemoryEvaluation({ action: 'prepare', config: join(root, 'l2-config.json'), output: join(root, 'l2-suite') }, { python: rt.python });
        report.l2ConfigSha256 = l2ConfigSha256; report.l2MemoryRevision = report.memory.revision;
      }
      const l1 = { runtime: join(this.home, 'runtime.json'), store: join(report.distillation.run, 'store'), baseRevision: report.distillation.initialRevision, upstream: rt.evaluation.directory, authorModel: report.config.authorModel || 'deepseek-flash', judgeModel: options.judgeModel, baseUrl: options.judgeBaseUrl || l2.baseUrl, factQueries: options.factQueries || [], documents: options.documents || [], calibration: options.calibration, maxLessons: 4 };
      const l1ConfigSha256 = sha(JSON.stringify(l1));
      if (report.l1Prepared && report.l1ConfigSha256 !== l1ConfigSha256) throw new Error('L1 已冻结，修改输入请新建实验');
      if (!report.l1Prepared) {
        await writeJson(join(root, 'l1-config.json'), l1);
        report.l1Prepared = await invokeMemoryEvaluation({ action: 'l1-prepare', config: join(root, 'l1-config.json'), output: join(root, 'l1-suite') }, { python: rt.python });
        report.l1ConfigSha256 = l1ConfigSha256;
      }
      return { l1: report.l1Prepared, l2: report.l2Prepared };
    });
  }
  async evaluate(id, level) {
    if (!['l1', 'l2'].includes(level)) throw new Error('未知评测层级');
    return this.stage(id, level, async (report, rt, signal) => {
      if (!report[level + 'Prepared']) throw new Error('请先冻结评测输入');
      const key = level === 'l1' ? this.credentials.judgeKey : this.credentials.modelKey;
      // The Python runner receives only its model credential; no flywheel token.
      const env = { ANTHROPIC_AUTH_TOKEN: key || process.env.ANTHROPIC_AUTH_TOKEN || process.env.DEEPSEEK_API_KEY || '' };
      if (!env.ANTHROPIC_AUTH_TOKEN) throw new Error('请填写评测模型凭据');
      const root = this.directory(id), driver = join(here, '..', level === 'l1' ? 'l1.py' : 'evaluate.py');
      const result = await run(rt.python, [driver, 'run', '--suite', join(root, level + '-suite'), '--out', join(root, level + '-replies')], { env, signal, timeout: 7200000 });
      report[level] = JSON.parse(result.stdout); return report[level];
    });
  }
  async reviewL1(id, { suiteSha256, reviewer, reason, decisions }) {
    const report = await this.get(id), root = this.directory(id);
    if (this.active || !report.l1Prepared || report.l1Prepared.suiteSha256 !== suiteSha256 || !reviewer?.trim() || !reason?.trim()) throw new Error('需要已冻结的 L1 材料、复核人和理由');
    if (!Array.isArray(decisions) || !decisions.length) throw new Error('请逐条提供 R1–R8 的复核结论与证据引用');
    const suite = await readJson(join(root, 'l1-suite/suite.json'));
    if (sha(await readFile(join(root, 'l1-suite/suite.json'))) !== suiteSha256) throw new Error('L1 冻结证据改变');
    if ((suite.lint.summary || suite.lint).lessons_with_open_hits !== 0) throw new Error('机械检查仍有未处理命中，先修订 Memory');
    if (suite.memoryRevision !== report.memory.revision) throw new Error('复核版本不匹配');
    for (const item of suite.items) {
      const rows = decisions.filter(d => d.lesson === item.id);
      const contains = (value, quote) => typeof value === 'string' ? value.includes(quote) : value && typeof value === 'object' && Object.values(value).some(v => contains(v, quote));
      if (rows.length !== 8 || new Set(rows.map(d => d.rule)).size !== 8 || rows.some(d => !/^R[1-8]$/.test(d.rule) || !['pass', 'n/a'].includes(d.verdict) || !d.reason?.trim() || !d.lessonQuote || !contains(item.lesson, d.lessonQuote) || !d.evidenceQuote || !contains({ item, facts: suite.facts }, d.evidenceQuote))) throw new Error('每条经验需要八条可定位的通过或不适用结论');
    }
    report.l1Review = { accepted: true, suiteSha256, memoryRevision: report.memory.revision, reviewer: reviewer.trim(), reason: reason.trim(), decisions, reviewedAt: new Date().toISOString(), mode: 'explicit-human-review' };
    await writeJson(join(root, 'l1-human-review.json'), report.l1Review); await this.save(report); return report.l1Review;
  }
  async previewPublication(id, options) {
    if (this.active) throw new Error('请等待当前阶段结束');
    const report = await this.get(id), rt = await this.runtime();
    const { checkOffline } = await import(pathToFileURL(join(rt.fixchain.directory, 'lib/offline.js')));
    await checkOffline(report.distillation.run, { runtime: join(this.home, 'runtime.json') });
    return publicationPreview(this.directory(id), options);
  }
  async publish(id, options) { await this.previewPublication(id, options); const result = await publishMemory(this.directory(id), options); const report = await this.get(id); report.publication = result; await this.save(report); return result; }
  cancel() { this.active?.abort.abort(); return { cancellationRequested: Boolean(this.active) }; }
}
