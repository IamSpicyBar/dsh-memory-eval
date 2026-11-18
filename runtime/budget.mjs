import { writeFileSync, renameSync } from 'node:fs';
export const name = 'memory-request-budget';
export const inject = ['agents'];
export function apply(ctx, config = {}) {
  const limit = config.requests;
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 256) throw new Error('Memory request budget must be 1..256');
  let count = 0;
  const record = () => { if (config.output) { writeFileSync(config.output + '.tmp', JSON.stringify({ requests: count, limit }), { mode: 0o600 }); renameSync(config.output + '.tmp', config.output); } };
  record();
  ctx.on('agent/request', (_event, next) => { if (count >= limit) throw new Error('Memory stage model request budget reached'); count++; record(); return next(); });
}
