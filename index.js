import { fileURLToPath } from 'node:url';
import { isAbsolute } from 'node:path';
import { spawn } from 'node:child_process';

export const name = 'dsh-memory-eval';
export const inject = ['tools'];
export async function invokeMemoryEvaluation(args, { python = process.env.HMBOT_RSI_PYTHON || 'python3', signal, timeout = 7200000 } = {}) {
  if (!['prepare', 'run', 'score', 'l1-prepare', 'l1-run', 'l1-score', 'l3-prepare', 'l3-run', 'l3-score'].includes(args.action)) throw new Error('memory_eval action 必须为 prepare/run/score 或 l1-/l3-prepare/run/score');
  const level = args.action.startsWith('l1-') ? 'l1' : args.action.startsWith('l3-') ? 'l3' : 'l2', action = args.action.replace(/^l[13]-/, '');
  for (const key of [action === 'prepare' ? 'config' : 'suite', 'output']) if (!isAbsolute(args[key] || '')) throw new Error(key + ' 需要绝对路径');
  const argv = [fileURLToPath(new URL(level === 'l2' ? './evaluate.py' : `./${level}.py`, import.meta.url)), action,
    ...(action === 'prepare' ? ['--config', args.config] : ['--suite', args.suite]), '--out', args.output];
  return new Promise((resolve, reject) => {
    const child = spawn(python, argv, { signal, timeout, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' }, stdio: ['ignore', 'pipe', 'pipe'] });
    let output = '', error = '';
    child.stdout.on('data', bytes => { output += bytes; if (output.length > 1024 * 1024) child.kill(); });
    child.stderr.on('data', bytes => { error = (error + bytes).slice(-4000); });
    child.once('error', reject);
    child.once('close', code => {
      if (code !== 0) { reject(new Error('Memory 评估执行失败：' + error.replace(/(?:sk-|Bearer\s+)\S+/gi, '[REDACTED]'))); return; }
      try { resolve(JSON.parse(output)); } catch { reject(new Error('Memory 评估未返回有效报告')); }
    });
  });
}

export function apply(ctx, config = {}) {
  const dispose = ctx.get('tools').register({ name: 'memory_eval',
    description: 'Memory 评测：默认 L1 来源/语义/事实复核及 L2 内容传递；可选 l3-prepare/run/score 对历史 Agent 原生回放，需单独环境。L3 未配置或失败不影响默认 L1/L2 流程。prepare/score 不调用模型；run 使用显式预算，缺项/截断不可冒充通过，不自动发布 Memory。',
    parameters: { type: 'object', properties: { action: { enum: ['prepare', 'run', 'score', 'l1-prepare', 'l1-run', 'l1-score', 'l3-prepare', 'l3-run', 'l3-score'] }, config: { type: 'string' }, suite: { type: 'string' }, output: { type: 'string' } }, required: ['action', 'output'], additionalProperties: false },
    output: { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    execute: (args, exec) => invokeMemoryEvaluation(args, { python: config.python, signal: exec?.signal }),
  });
  ctx.effect(() => dispose);
}
