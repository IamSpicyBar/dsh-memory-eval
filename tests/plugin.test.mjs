import test from 'node:test';
import assert from 'node:assert/strict';
import {apply,invokeMemoryEvaluation} from '../index.js';
test('unit registration exposes evaluation and rejects undeclared paths before execution',async()=>{
 let tool,dispose;apply({get:()=>({register:value=>{tool=value;return()=>{}}}),effect:fn=>{dispose=fn}});
 assert.equal(tool.name,'memory_eval');assert.equal(typeof dispose,'function');
 await assert.rejects(invokeMemoryEvaluation({action:'l3-prepare',config:'relative.json',output:'/tmp/new'}),/绝对路径/);
});
