#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { invokeMemoryEvaluation } from '../index.js';
const {positionals,values}=parseArgs({allowPositionals:true,options:{config:{type:'string'},suite:{type:'string'},output:{type:'string'},python:{type:'string'}}});
try { console.log(JSON.stringify(await invokeMemoryEvaluation({action:positionals[0],config:values.config,suite:values.suite,output:values.output},{python:values.python}))); }
catch(error){console.error(error.message);process.exitCode=1;}
