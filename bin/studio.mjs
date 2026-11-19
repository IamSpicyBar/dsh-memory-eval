#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { homePath } from '../runtime/common.mjs';
import { setup } from '../runtime/setup.mjs';
import { serve } from '../runtime/server.mjs';

const [major, minor] = process.versions.node.split('.').map(Number);
if (major < 24 || major === 24 && minor < 9) throw new Error('Memory RSI requires Node.js 24.9+');
const { values, positionals } = parseArgs({ allowPositionals: true, options: { port: { type: 'string', default: '8878' }, 'studio-origin': { type: 'string' }, 'with-l3': { type: 'boolean', default: false }, 'dsh-cli': { type: 'string' }, sources: { type: 'string' } } });
const home = homePath();
try {
  if (positionals[0] === 'setup') {
    const sources = values.sources ? JSON.parse(await (await import('node:fs/promises')).readFile(values.sources, 'utf8')) : {};
    const state = await setup(home, { dshCli: values['dsh-cli'], sources, withL3: values['with-l3'], progress: line => process.stderr.write(line + '\n') });
    process.stdout.write(JSON.stringify({ ready: true, home, dsh: state.dsh, migloop: state.migloop.commit }) + '\n');
  } else if (!positionals.length || positionals[0] === 'start') {
    const server = await serve(home, { port: Number(values.port), studioOrigin: values['studio-origin'] });
    process.stdout.write('Memory RSI: ' + server.origin + '\n');
  } else throw new Error('Usage: memory-rsi setup [--dsh-cli PATH] | start [--port 8878] [--studio-origin https://hmbotstudio.cn]');
} catch (error) { process.stderr.write(String(error.message) + '\n'); process.exitCode = 1; }
