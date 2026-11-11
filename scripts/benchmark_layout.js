'use strict';
// node scripts/benchmark_layout.js [path/to/original/renderer.js]
// Same seeded graph and warmup for both workers; timing excludes initialization,
// messaging and rendering. Function compilation uses the normal JS realm (a vm
// sandbox makes global Math lookups artificially expensive in the old pair loop).
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {performance} = require('node:perf_hooks');
const path = require('node:path');
function source(file) {
  return fs.readFileSync(file, 'utf8').match(/const LAYOUT_WORKER = `([\s\S]*?)`;/)[1];
}
let seed = 42;
function random() { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296; }
function graph(n, m) {
  const positions = new Float32Array(n*2), radii = new Float32Array(n).fill(7);
  const active = new Uint8Array(n).fill(1), edges = new Uint32Array(m*2), weights = new Float32Array(m).fill(1);
  for (let i=0;i<positions.length;i++) positions[i] = (random()-.5)*12000;
  for (let e=0;e<m;e++) { edges[2*e] = Math.floor(random()*(e%4===0 ? 12 : n)); edges[2*e+1] = Math.floor(random()*n); }
  return {type:'start',positions,radii,active,edges,weights,k:84};
}
function worker(file, data, scheduling = {}) {
  const create = new Function('performance', 'setTimeout', 'clearTimeout', 'postMessage',
    'let onmessage;\n' + source(file) + `
    return {send: data => onmessage({data}), tick: () => tick(),
      inspect: code => eval(code), loop: () => loop(),
      initialize: data => { const original=loop; loop=()=>{}; onmessage({data}); loop=original; },
      measure: iterations => { const start=performance.now(); for(let t=0;t<iterations;t++) tick(); return (performance.now()-start)/iterations; }};`);
  const c = create(scheduling.performance || performance, scheduling.setTimeout || (()=>0), ()=>{}, scheduling.postMessage || (()=>{}));
  if (scheduling.setTimeout) c.send(structuredClone(data));
  else c.initialize(structuredClone(data));
  return c;
}
const current = path.join(__dirname, '../viewer/renderer.js');
const data = graph(8000, 40000);
for (const [name,file] of [['before',process.argv[2]], ['after',current]]) {
  if (!file) continue;
  const c = worker(file, data);
  for(let i=0;i<5;i++) c.tick();
  console.log(`${name}: ${c.measure(20).toFixed(2)} ms/tick (8000 nodes, 40000 edges; 5 warmup, 20 measured)`);
}
// Pinning, active-mask preservation, coincident coordinates and cooling.
const small = graph(40,80); small.active[39]=0; small.positions.fill(0);
const c = worker(current,small);
c.send({type:'pin',index:0,x:100,y:200}); c.tick();
assert(c.inspect('P[0]===100 && P[1]===200 && P[78]===0 && P[79]===0 && P.every(Number.isFinite)'));
c.send({type:'unpin',index:0});
assert(c.inspect('alpha===1 && !fixed.has(0)'));
for(let t=0;t<420;t++) c.tick();
assert(c.inspect('alpha < .002 && P.every(Number.isFinite)'));
c.send({type:'pin',index:39,x:999,y:999}); c.tick();
assert(c.inspect('P[78]===0 && P[79]===0'));
// Edge weights and spacing still affect attraction/repulsion at uncapped steps.
function pair(weight,k) {
  const g=graph(2,1); g.positions.set([-50,0,50,0]); g.edges.set([0,1]); g.weights[0]=weight; g.k=k;
  const w=worker(current,g); w.tick(); return w.inspect('P[0]');
}
assert(pair(1,84)>pair(0,84));
assert(pair(0,120)<pair(0,30));
// Simulate a delayed buffer return at convergence. The last snapshot must arrive,
// posts must remain <=30 Hz, and pin/unpin must wake a cooled worker.
let now=0, scheduled=null;
const posts=[];
const timed=worker(current,small,{
  performance:{now:()=>++now}, setTimeout:(fn,delay)=>{scheduled={fn,delay};return 1;},
  postMessage:(value,transfer)=>{assert.equal(transfer[0],value.buffer);posts.push({time:now,value:structuredClone(value,{transfer})});},
});
const advance=()=>{const task=scheduled;scheduled=null;assert(task);now+=task.delay;task.fn();};
while(timed.inspect('alpha > .002')) advance();
assert.equal(posts.length,1); // no buffer was returned yet
assert(timed.inspect('running'));
timed.send({type:'recycle',positions:posts[0].value}); advance();
assert.equal(posts.length,2); assert(posts[1].time-posts[0].time>=1000/30);
assert.equal(timed.inspect('running'),false);
timed.send({type:'unpin',index:0});
assert(timed.inspect('running && alpha===1')); advance();
console.log('Worker checks passed: pins, inactive nodes, coincidence, weights, spacing, cooling, reheating, transfer recycling and final snapshot.');
// Refresh must not rescan layout membership on camera/worker/hover frames.
require('../viewer/renderer.js');
global.requestAnimationFrame=()=>1;
global.Worker=class { postMessage(m){this.input=m;} terminate(){this.terminated=true;} };
const renderer=Object.create(GraphRenderer.prototype);
renderer.nodes=[0,1,2].map(id=>({id,x:id*100,y:0,size:7}));
renderer.edges=[{from:0,to:1}];
renderer.nodeById=new Map(renderer.nodes.map(n=>[n.id,n]));
let scans=0;
const visible=renderer._visibleLayoutLinks;
renderer._visibleLayoutLinks=function(){scans++;return visible.call(this);};
renderer.startLayout();
for(let i=0;i<100;i++) renderer.refresh();
assert.equal(scans,1);
renderer.nodes[1].ghost=true; renderer.refresh();
assert.equal(scans,2);
renderer.edges[0].pull=2; renderer.invalidateLayout(); renderer.refresh();
assert.equal(scans,3);
renderer.stopLayout();
console.log('Renderer checks passed: constant-time refresh, automatic visibility invalidation and explicit layout invalidation.');
