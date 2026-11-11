'use strict';
// Dependency-free WebGL2 graph renderer for the relationship explorer.
//
// Draws dots and lines (dashes, arrowheads) with instanced WebGL, labels on a
// 2D canvas overlay, and does hit-testing on the CPU. World coordinates have y
// growing downward (as in the precomputed Python layout). The caller owns the
// node/edge objects; mutate their fields and call refresh():
//   node: {id, x, y, size (world radius), color '#rrggbb', color2 (optional '#rrggbb'), label, hidden, highlighted, ghost (faded context)}
//   edge: {id, from, to, color '#rrggbb', opacity, width (world), dashed, arrow, hidden, highlighted, ghost (faded context)}
// Events (on(name, fn)): click {node, edge}, doubleclick {node} (node null on empty space), hover {node, edge}, dragend {node}.
(function (global) {
  // Shared accessors avoid four closures per node/edge when layout starts.
  const visibilityOwner = Symbol('visibilityOwner'), hiddenValue = Symbol('hidden'), ghostValue = Symbol('ghost');
  const visibilityDescriptors = {
    hidden: {configurable: true, enumerable: true,
      get() { return this[hiddenValue]; },
      set(next) { if (!!next !== !!this[hiddenValue]) this[visibilityOwner]._layoutInvalid = true; this[hiddenValue] = next; }},
    ghost: {configurable: true, enumerable: true,
      get() { return this[ghostValue]; },
      set(next) { if (!!next !== !!this[ghostValue]) this[visibilityOwner]._layoutInvalid = true; this[ghostValue] = next; }},
  };
  const NODE_VS = `#version 300 es
layout(location=0) in vec2 a_corner;
layout(location=1) in vec2 a_pos;
layout(location=2) in float a_radius;
layout(location=3) in vec4 a_color;
layout(location=4) in float a_ring;
layout(location=5) in vec4 a_color2;
uniform vec2 u_center; uniform float u_scale; uniform vec2 u_viewport;
out vec4 v_color2;
out vec2 v_local; out vec4 v_color; out float v_ring; out float v_r;
void main() {
  float r = max(a_radius * u_scale, 1.5);
  float extent = r + (a_ring > 0.5 ? 4.5 : 1.5);
  vec2 px = (a_pos - u_center) * u_scale + a_corner * extent;
  gl_Position = vec4(px / (u_viewport * 0.5) * vec2(1.0, -1.0), 0.0, 1.0);
  v_color2 = a_color2; v_local = a_corner * extent; v_color = a_color; v_ring = a_ring; v_r = r;
}`;
  const NODE_FS = `#version 300 es
precision mediump float;
in vec4 v_color2;
in vec2 v_local; in vec4 v_color; in float v_ring; in float v_r;
out vec4 o;
void main() {
  float d = length(v_local);
  float fill = 1.0 - smoothstep(v_r - 0.75, v_r + 0.75, d);
  float ring = v_ring > 0.5 ? (1.0 - smoothstep(v_r + 2.5, v_r + 3.5, d)) * smoothstep(v_r - 0.5, v_r + 0.5, d) : 0.0;
  float a = max(fill * v_color.a, ring);
  if (a < 0.01) discard;
  vec3 rgb = mix(vec3(1.0), (v_local.x < 0.0 ? v_color.rgb : v_color2.rgb), fill / max(fill + ring, 1e-4));
  o = vec4(rgb, a);
}`;
  // Edge quad: a_corner.x runs along the edge (0..1), a_corner.y across (-1..1).
  const EDGE_VS = `#version 300 es
layout(location=0) in vec2 a_corner;
layout(location=1) in vec4 a_ends;    // source xy, target xy (world)
layout(location=2) in vec2 a_radii;   // source, target radius (world)
layout(location=3) in vec4 a_color;
layout(location=4) in vec4 a_style;   // width (world), dashed, arrow, extra px
uniform vec2 u_center; uniform float u_scale; uniform vec2 u_viewport;
out float v_along; out float v_across; out float v_hw; out vec4 v_color; out float v_dashed;
void main() {
  vec2 s = (a_ends.xy - u_center) * u_scale, t = (a_ends.zw - u_center) * u_scale;
  vec2 d = t - s; float len = length(d);
  vec2 dir = len > 0.0 ? d / len : vec2(1.0, 0.0), nrm = vec2(-dir.y, dir.x);
  float raw = min(a_style.x * u_scale, 10.0) + a_style.w, w = max(raw, 1.0);
  float arrow = a_style.z > 0.5 ? clamp(raw * 3.0, 0.0, 18.0) : 0.0;
  float sr = max(a_radii.x * u_scale, 1.5), tr = max(a_radii.y * u_scale, 1.5) + arrow * 0.8;
  float L = max(len - sr - tr, 0.0);
  vec2 start = s + dir * sr;
  float hw = w * 0.5 + 1.0;
  vec2 px = start + dir * (a_corner.x * L) + nrm * (a_corner.y * hw);
  gl_Position = vec4(px / (u_viewport * 0.5) * vec2(1.0, -1.0), 0.0, 1.0);
  v_along = a_corner.x * L; v_across = a_corner.y * hw; v_hw = w * 0.5;
  v_color = vec4(a_color.rgb, a_color.a * max(min(raw, 1.0), 0.6)); v_dashed = a_style.y;  // sub-pixel lines fade, not vanish
}`;
  const EDGE_FS = `#version 300 es
precision mediump float;
in float v_along; in float v_across; in float v_hw; in vec4 v_color; in float v_dashed;
out vec4 o;
void main() {
  float a = 1.0 - smoothstep(v_hw - 0.5, v_hw + 0.5, abs(v_across));
  if (v_dashed > 0.5) { float m = mod(v_along, 10.0); a *= 1.0 - smoothstep(5.5, 6.5, m); }
  a *= v_color.a;
  if (a < 0.01) discard;
  o = vec4(v_color.rgb, a);
}`;
  // Arrowhead triangle: corner (along, across) with the tip at (0, 0).
  const ARROW_VS = `#version 300 es
layout(location=0) in vec2 a_corner;
layout(location=1) in vec4 a_ends;
layout(location=2) in vec2 a_radii;
layout(location=3) in vec4 a_color;
layout(location=4) in vec4 a_style;
uniform vec2 u_center; uniform float u_scale; uniform vec2 u_viewport;
out vec4 v_color;
void main() {
  vec2 s = (a_ends.xy - u_center) * u_scale, t = (a_ends.zw - u_center) * u_scale;
  vec2 d = t - s; float len = length(d);
  vec2 dir = len > 0.0 ? d / len : vec2(1.0, 0.0), nrm = vec2(-dir.y, dir.x);
  float raw = min(a_style.x * u_scale, 10.0) + a_style.w, size = clamp(raw * 3.0, 0.0, 18.0);  // shrinks away when zoomed out
  vec2 tip = t - dir * max(a_radii.y * u_scale, 1.5);
  vec2 px = tip + dir * (a_corner.x * size) + nrm * (a_corner.y * size * 0.5);
  gl_Position = vec4(px / (u_viewport * 0.5) * vec2(1.0, -1.0), 0.0, 1.0);
  v_color = vec4(a_color.rgb, min(1.0, a_color.a * 1.6));
}`;
  const ARROW_FS = `#version 300 es
precision mediump float;
in vec4 v_color; out vec4 o;
void main() { o = v_color; }`;

  // Barnes-Hut FR repulsion (theta .9), spatial-hash collision, and cooling.
  // Scratch arrays are retained; position snapshots are transferred and recycled.
  const LAYOUT_WORKER = `
let P, R, active, E, W, ids, order, D, next, heads, hx, hy, mask;
let children, mass, mx, my, widths, starts, ends, cells;
let fixed = new Map(), k = 60, running = false, alpha = 1, timer;
let cx = 0, cy = 0, gravity = .12, cellSize = 1, spare = null, lastPost = -Infinity;
function schedule() {
  if (!running) { running = true; timer = setTimeout(loop, 0); }
}
onmessage = ({data: m}) => {
  if (m.type === 'recycle') { if (m.positions.length === P.length) spare = m.positions; return; }
  if (m.type === 'start') {
    clearTimeout(timer); running = false;
    P = m.positions; R = m.radii; active = m.active; E = m.edges; W = m.weights; k = Math.max(1, m.k);
    fixed.clear(); ids = Uint32Array.from(active.keys()).filter(i => active[i]);
    order = new Uint32Array(ids.length); D = new Float32Array(P.length); next = new Int32Array(R.length);
    const capacity = Math.max(32, ids.length * 32 + 32);
    children = new Int32Array(capacity * 4); mass = new Int32Array(capacity);
    mx = new Float64Array(capacity); my = new Float64Array(capacity); widths = new Float64Array(capacity);
    starts = new Int32Array(capacity); ends = new Int32Array(capacity);
    let size = 16; while (size < ids.length * 4) size *= 2;
    heads = new Int32Array(size); hx = new Int32Array(size); hy = new Int32Array(size); mask = size - 1;
    cx = 0; cy = 0; cellSize = 1;
    for (const i of ids) { cx += P[2*i]; cy += P[2*i+1]; cellSize = Math.max(cellSize, 2*R[i]+12); }
    cx /= ids.length || 1; cy /= ids.length || 1;
    let extent2 = 0;
    for (const i of ids) extent2 += (P[2*i]-cx)**2 + (P[2*i+1]-cy)**2;
    // Calibrate central gravity against the initial RMS radius, with a floor
    // for compact/degenerate inputs. The anchor stays fixed throughout the run.
    gravity = Math.max(.12, ids.length*k*k / (2*Math.max(extent2/(ids.length || 1), k*k)));
    spare = new Float32Array(P.length); lastPost = -Infinity; alpha = 1; schedule();
  } else if (m.type === 'stop') { running = false; clearTimeout(timer); }
  else if (m.type === 'pin' || m.type === 'unpin') {
    if (m.type === 'pin') {
      fixed.set(m.index, [m.x, m.y]);
      if (active[m.index]) { P[2*m.index] = m.x; P[2*m.index+1] = m.y; }
    } else fixed.delete(m.index);
    alpha = 1; schedule();
  }
};
function partition(lo, hi, axis, mid) {
  let a = lo, b = hi - 1;
  while (a <= b) {
    if (P[2*order[a]+axis] < mid) a++;
    else { const t = order[a]; order[a] = order[b]; order[b--] = t; }
  }
  return a;
}
function build(lo, hi, x, y, width, depth) {
  const q = cells++, base = q*4;
  children.fill(-1, base, base+4); mass[q] = hi-lo; starts[q] = lo; ends[q] = hi; widths[q] = width;
  let sx = 0, sy = 0;
  for (let a = lo; a < hi; a++) { sx += P[2*order[a]]; sy += P[2*order[a]+1]; }
  mx[q] = sx/(hi-lo); my[q] = sy/(hi-lo);
  if (hi-lo <= 1 || depth >= 24 || width < 1e-5) return q;
  const half = width/2, mid = partition(lo, hi, 0, x+half);
  const a = partition(lo, mid, 1, y+half), b = partition(mid, hi, 1, y+half);
  if (a > lo) children[base] = build(lo,a,x,y,half,depth+1);
  if (mid > a) children[base+1] = build(a,mid,x,y+half,half,depth+1);
  if (b > mid) children[base+2] = build(mid,b,x+half,y,half,depth+1);
  if (hi > b) children[base+3] = build(b,hi,x+half,y+half,half,depth+1);
  return q;
}
function repel(i, q, contains) {
  let vx = P[2*i]-mx[q], vy = P[2*i+1]-my[q], d2 = vx*vx+vy*vy;
  const base = q*4;
  if (children[base] < 0 && children[base+1] < 0 && children[base+2] < 0 && children[base+3] < 0) {
    // Depth-limited buckets can contain coincident dots. Aggregate them too,
    // excluding self, rather than degenerating to all-pairs repulsion.
    const n = mass[q] - (contains ? 1 : 0);
    if (!n) return;
    if (contains) { vx = P[2*i] - (mx[q]*mass[q]-P[2*i])/n; vy = P[2*i+1] - (my[q]*mass[q]-P[2*i+1])/n; d2 = vx*vx+vy*vy; }
    if (d2 < 1e-6) { const angle = i*2.399963229728653; vx = .01*Math.cos(angle); vy = .01*Math.sin(angle); d2 = .0001; }
    const f = k*k*n/d2; D[2*i] += vx*f; D[2*i+1] += vy*f;
  } else if (!contains && widths[q]*widths[q] < .81*d2) {
    const f = k*k*mass[q]/d2; D[2*i] += vx*f; D[2*i+1] += vy*f;
  } else {
    // A cell containing the target must never approximate its own mass.
    for (let c = 0; c < 4; c++) {
      const child = children[base+c];
      if (child >= 0) repel(i, child, contains && ranks[i] >= starts[child] && ranks[i] < ends[child]);
    }
  }
}
let ranks;
function slot(x,y) {
  let s = (Math.imul(x,73856093) ^ Math.imul(y,19349663)) & mask;
  while (heads[s] !== -1 && (hx[s] !== x || hy[s] !== y)) s = (s+1)&mask;
  return s;
}
function tick() {
  if (!ids.length) { alpha = 0; return; }
  D.fill(0); order.set(ids);
  let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity;
  for (const i of ids) { x0=Math.min(x0,P[2*i]); x1=Math.max(x1,P[2*i]); y0=Math.min(y0,P[2*i+1]); y1=Math.max(y1,P[2*i+1]); }
  cells=0; build(0,ids.length,x0,y0,Math.max(x1-x0,y1-y0,1)*1.00001,0);
  if (!ranks || ranks.length !== R.length) ranks = new Uint32Array(R.length);
  for (let a=0;a<order.length;a++) ranks[order[a]]=a;
  for (const i of ids) repel(i,0,true);
  heads.fill(-1);
  for (const i of ids) {
    const x=Math.floor(P[2*i]/cellSize), y=Math.floor(P[2*i+1]/cellSize);
    for (let ox=-1;ox<=1;ox++) for (let oy=-1;oy<=1;oy++) {
      const s=slot(x+ox,y+oy);
      for (let j=heads[s];j!==-1;j=next[j]) {
        let vx=P[2*i]-P[2*j], vy=P[2*i+1]-P[2*j+1], d=Math.hypot(vx,vy);
        if (d < 1e-6) { vx=.01; vy=0; d=.01; }
        const overlap=R[i]+R[j]+12-d;
        if (overlap>0) { const f=4*overlap/d; D[2*i]+=vx*f; D[2*i+1]+=vy*f; D[2*j]-=vx*f; D[2*j+1]-=vy*f; }
      }
    }
    const s=slot(x,y); hx[s]=x; hy[s]=y; next[i]=heads[s]; heads[s]=i;
  }
  for (let e=0;e<W.length;e++) {
    const i=E[2*e],j=E[2*e+1]; if (!active[i] || !active[j]) continue;
    const vx=P[2*j]-P[2*i],vy=P[2*j+1]-P[2*i+1],f=Math.hypot(vx,vy)/k*W[e];
    D[2*i]+=vx*f; D[2*i+1]+=vy*f; D[2*j]-=vx*f; D[2*j+1]-=vy*f;
  }
  const limit=k*.1*alpha;
  for (const i of ids) {
    if (fixed.has(i)) { const p=fixed.get(i); P[2*i]=p[0]; P[2*i+1]=p[1]; continue; }
    let dx=D[2*i]+(cx-P[2*i])*gravity,dy=D[2*i+1]+(cy-P[2*i+1])*gravity;
    const f=Math.min(alpha,limit/(Math.hypot(dx,dy)||1));
    P[2*i]+=dx*f; P[2*i+1]+=dy*f;
  }
  alpha *= .985;
}
function loop() {
  if (!running) return;
  const start=performance.now();
  if (alpha > .002) do tick(); while (alpha > .002 && performance.now()-start < 10);
  const now=performance.now();
  if (spare && now-lastPost >= 1000/30) {
    spare.set(P); const output=spare; spare=null; postMessage(output,[output.buffer]); lastPost=now;
    if (alpha <= .002) { running=false; return; }
  }
  // Wait for a recycled buffer if cooling finished while a snapshot was in flight.
  timer=setTimeout(loop,Math.max(4,1000/30-(performance.now()-lastPost)));
}`;

  function hexColor(hex, alpha) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || ''));
    const n = m ? parseInt(m[1], 16) : 0xaaaaaa;
    return [(n >> 16) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255, alpha];
  }

  function compile(gl, vs, fs) {
    const program = gl.createProgram();
    for (const [type, source] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source); gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
      gl.attachShader(program, shader);
    }
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
    const uniforms = {};
    for (const name of ['u_center', 'u_scale', 'u_viewport']) uniforms[name] = gl.getUniformLocation(program, name);
    return {program, uniforms};
  }

  class GraphRenderer {
    constructor(container, {tooltip} = {}) {
      this.container = container;
      this.tooltipFor = tooltip || (() => null);
      this.nodes = []; this.edges = []; this.nodeById = new Map();
      this.camera = {x: 0, y: 0, scale: 1};
      this.handlers = new Map();
      this.hovered = null;
      this.layoutRunning = false;
      this._dirty = true; this._frame = 0;
      container.style.position = 'relative';
      this.canvas = document.createElement('canvas');
      this.labels = document.createElement('canvas');
      this.tip = document.createElement('div');
      this.tip.className = 'graph-tooltip'; this.tip.hidden = true;
      for (const c of [this.canvas, this.labels]) Object.assign(c.style, {position: 'absolute', inset: '0', width: '100%', height: '100%'});
      this.labels.style.pointerEvents = 'none';
      container.append(this.canvas, this.labels, this.tip);
      const gl = (this.gl = this.canvas.getContext('webgl2', {antialias: false, premultipliedAlpha: false}));
      if (!gl) throw new Error('WebGL2 is not available in this browser');
      this.ctx = this.labels.getContext('2d');
      this.programs = {node: compile(gl, NODE_VS, NODE_FS), edge: compile(gl, EDGE_VS, EDGE_FS), arrow: compile(gl, ARROW_VS, ARROW_FS)};
      this._setupBuffers();
      this._bindInput();
      new ResizeObserver(() => this._resize()).observe(container);
      this._resize();
    }

    // ---- data -------------------------------------------------------------
    setGraph(nodes, edges) {
      this.nodes = nodes; this.edges = edges;
      this.nodeById = new Map(nodes.map(n => [n.id, n]));
      this.invalidateLayout();
      this.refresh();
    }
    // Visibility writes mark a dirty bit; ordinary frames do no membership scans.
    refresh() {
      if (this.layoutRunning && this._layoutInvalid) this.startLayout(this._layoutSpacing);
      this._dirty = true;
      if (!this._frame) this._frame = requestAnimationFrame(() => { this._frame = 0; this._draw(); });
    }
    // Call before refresh() after changing topology, radii, or edge pulls.
    // Ordinary hidden/ghost writes invalidate automatically without scanning on refresh.
    invalidateLayout() { this._layoutInvalid = true; }
    _watchLayoutVisibility() {
      if (!this._visibilityWatched) this._visibilityWatched = new WeakSet();
      const watch = object => {
        if (this._visibilityWatched.has(object)) return;
        this._visibilityWatched.add(object);
        for (const key of ['hidden', 'ghost']) {
          const descriptor = Object.getOwnPropertyDescriptor(object, key);
          // Accessor/non-configurable properties can use explicit invalidateLayout().
          if (descriptor && (!descriptor.configurable || descriptor.get || descriptor.set || !descriptor.writable)) continue;
          object[visibilityOwner] = this;
          object[key === 'hidden' ? hiddenValue : ghostValue] = object[key];
          Object.defineProperty(object, key, visibilityDescriptors[key]);
        }
      };
      this.nodes.forEach(watch); this.edges.forEach(watch);
    }
    on(name, fn) {
      if (!this.handlers.has(name)) this.handlers.set(name, []);
      this.handlers.get(name).push(fn);
    }
    _emit(name, payload) {
      for (const fn of this.handlers.get(name) || []) fn(payload);
    }

    // ---- camera -----------------------------------------------------------
    worldToScreen(x, y) {
      return {x: (x - this.camera.x) * this.camera.scale + this.width / 2, y: (y - this.camera.y) * this.camera.scale + this.height / 2};
    }
    screenToWorld(x, y) {
      return {x: (x - this.width / 2) / this.camera.scale + this.camera.x, y: (y - this.height / 2) / this.camera.scale + this.camera.y};
    }
    setCamera(state, animate = false) {
      const target = {...this.camera, ...state};
      cancelAnimationFrame(this._tween);
      if (!animate) { this.camera = target; this.refresh(); return; }
      const from = {...this.camera}, start = performance.now();
      const step = now => {
        const t = Math.min(1, (now - start) / 400), e = t * t * (3 - 2 * t);
        const ls = Math.log(from.scale) + (Math.log(target.scale) - Math.log(from.scale)) * e;
        this.camera = {x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e, scale: Math.exp(ls)};
        this.refresh();
        if (t < 1) this._tween = requestAnimationFrame(step);
      };
      this._tween = requestAnimationFrame(step);
    }
    fit(ids = null, animate = false) {
      const list = ids ? ids.map(id => this.nodeById.get(id)).filter(Boolean) : this.nodes.filter(n => !n.hidden && !n.ghost);
      if (!list.length) return;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of list) {
        const r = n.size || 7;
        x0 = Math.min(x0, n.x - r); y0 = Math.min(y0, n.y - r); x1 = Math.max(x1, n.x + r); y1 = Math.max(y1, n.y + r);
      }
      const scale = Math.min(this.width / Math.max(x1 - x0, 1), this.height / Math.max(y1 - y0, 1)) * 0.9;
      this.setCamera({x: (x0 + x1) / 2, y: (y0 + y1) / 2, scale: Math.min(scale, 1.2)}, animate);
    }
    focus(id, scale = 1.2, animate = true) {
      const n = this.nodeById.get(id);
      if (n) this.setCamera({x: n.x, y: n.y, scale}, animate);
    }
    screenPosition(id) {
      const n = this.nodeById.get(id);
      return n ? this.worldToScreen(n.x, n.y) : null;
    }

    // ---- hit testing --------------------------------------------------------
    nodeAt(sx, sy) {
      let best = null, bestD = Infinity;
      for (const n of this.nodes) {
        if (n.hidden) continue;
        const p = this.worldToScreen(n.x, n.y), r = Math.max((n.size || 7) * this.camera.scale, 1.5) + 3;
        const d = Math.hypot(p.x - sx, p.y - sy);
        if (d <= r && d - r <= bestD) { best = n; bestD = d - r; }
      }
      return best;
    }
    edgeAt(sx, sy) {
      let best = null, bestD = Infinity;
      for (const e of this.edges) {
        if (e.hidden || e.from === e.to) continue;
        const a = this.nodeById.get(e.from), b = this.nodeById.get(e.to);
        if (!a || !b || a.hidden || b.hidden) continue;
        const p = this.worldToScreen(a.x, a.y), q = this.worldToScreen(b.x, b.y);
        const dx = q.x - p.x, dy = q.y - p.y, len2 = dx * dx + dy * dy;
        const t = len2 ? Math.max(0, Math.min(1, ((sx - p.x) * dx + (sy - p.y) * dy) / len2)) : 0;
        const d = Math.hypot(sx - p.x - t * dx, sy - p.y - t * dy);
        const reach = Math.max((e.width || 1) * this.camera.scale / 2, 1) + 3;
        if (d <= reach && d < bestD) { best = e; bestD = d; }
      }
      return best;
    }

    // ---- input --------------------------------------------------------------
    _bindInput() {
      const c = this.canvas;
      let down = null;
      const local = ev => { const r = c.getBoundingClientRect(); return {x: ev.clientX - r.left, y: ev.clientY - r.top}; };
      c.addEventListener('pointerdown', ev => {
        const p = local(ev), node = this.nodeAt(p.x, p.y);
        down = {p, start: p, node, moved: false, camera: {...this.camera}};
        c.setPointerCapture(ev.pointerId);
        this._hideTip();
      });
      c.addEventListener('pointermove', ev => {
        const p = local(ev);
        if (down) {
          if (Math.hypot(p.x - down.start.x, p.y - down.start.y) > 4) down.moved = true;
          if (!down.moved) return;
          if (down.node) {
            const w = this.screenToWorld(p.x, p.y);
            down.node.x = w.x; down.node.y = w.y;
            this._pin(down.node, true);
          } else {
            this.camera = {...this.camera, x: down.camera.x - (p.x - down.start.x) / this.camera.scale, y: down.camera.y - (p.y - down.start.y) / this.camera.scale};
          }
          this.refresh();
          return;
        }
        this._pointer = p;
        if (!this._hoverFrame) this._hoverFrame = requestAnimationFrame(() => { this._hoverFrame = 0; this._updateHover(); });
      });
      c.addEventListener('pointerup', ev => {
        if (!down) return;
        const p = local(ev), d = down; down = null;
        if (!d.moved) {
          const node = this.nodeAt(p.x, p.y), edge = node ? null : this.edgeAt(p.x, p.y);
          this._emit('click', {node, edge});
        } else if (d.node) {
          this._pin(d.node, false);
          this._emit('dragend', {node: d.node});
        }
      });
      c.addEventListener('pointerleave', () => { this._pointer = null; this._updateHover(); });
      c.addEventListener('dblclick', ev => {
        const p = local(ev), node = this.nodeAt(p.x, p.y);
        if (!node) this._zoomAt(p, 2, true);
        this._emit('doubleclick', {node});
      });
      c.addEventListener('wheel', ev => {
        ev.preventDefault();
        this._zoomAt(local(ev), Math.exp(-ev.deltaY * (ev.deltaMode ? 0.05 : 0.0015)), false);
      }, {passive: false});
    }
    _zoomAt(p, factor, animate) {
      const w = this.screenToWorld(p.x, p.y), scale = Math.max(1e-4, Math.min(20, this.camera.scale * factor));
      this.setCamera({scale, x: w.x - (p.x - this.width / 2) / scale, y: w.y - (p.y - this.height / 2) / scale}, animate);
    }
    _updateHover() {
      const p = this._pointer;
      const node = p ? this.nodeAt(p.x, p.y) : null, edge = p && !node ? this.edgeAt(p.x, p.y) : null;
      const target = node || edge;
      if (target !== this.hovered) {
        this.hovered = target;
        this.canvas.style.cursor = target ? 'pointer' : '';
        this._emit('hover', {node, edge});
        this.refresh();
        const content = target ? this.tooltipFor(node ? 'node' : 'edge', target) : null;
        if (content) {
          this.tip.replaceChildren(typeof content === 'string' ? document.createTextNode(content) : content);
          this.tip.hidden = false;
        } else this._hideTip();
      }
      if (p && !this.tip.hidden) {
        const x = Math.min(p.x + 14, this.width - this.tip.offsetWidth - 4), y = Math.min(p.y + 14, this.height - this.tip.offsetHeight - 4);
        this.tip.style.left = Math.max(0, x) + 'px'; this.tip.style.top = Math.max(0, y) + 'px';
      }
    }
    _hideTip() { this.tip.hidden = true; }

    // ---- drawing ------------------------------------------------------------
    _resize() {
      const dpr = window.devicePixelRatio || 1, r = this.container.getBoundingClientRect();
      this.width = Math.max(1, r.width); this.height = Math.max(1, r.height); this.dpr = dpr;
      for (const c of [this.canvas, this.labels]) { c.width = Math.round(this.width * dpr); c.height = Math.round(this.height * dpr); }
      this.refresh();
    }
    _setupBuffers() {
      const gl = this.gl;
      const quad = (corners, attrs) => {
        const vao = gl.createVertexArray(); gl.bindVertexArray(vao);
        const cb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, cb);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(corners), gl.STATIC_DRAW);
        gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
        const ib = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, ib);
        const stride = attrs.reduce((s, a) => s + a, 0) * 4;
        let offset = 0;
        attrs.forEach((size, i) => {
          gl.enableVertexAttribArray(i + 1);
          gl.vertexAttribPointer(i + 1, size, gl.FLOAT, false, stride, offset);
          gl.vertexAttribDivisor(i + 1, 1);
          offset += size * 4;
        });
        gl.bindVertexArray(null);
        return {vao, buffer: ib, floats: stride / 4, count: corners.length / 2};
      };
      this.nodeGeo = quad([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1], [2, 1, 4, 1, 4]);
      this.edgeGeo = quad([0, -1, 1, -1, 0, 1, 0, 1, 1, -1, 1, 1], [4, 2, 4, 4]);
      this.arrowGeo = quad([0, 0, -1, 1, -1, -1], [4, 2, 4, 4]);
      // The arrow VAO reads the edge instance buffer.
      gl.bindVertexArray(this.arrowGeo.vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.edgeGeo.buffer);
      [4, 2, 4, 4].reduce((offset, size, i) => { gl.vertexAttribPointer(i + 1, size, gl.FLOAT, false, 14 * 4, offset); return offset + size * 4; }, 0);
      gl.bindVertexArray(null);
    }
    _instanceData(key, length) {
      if (!this[key] || this[key].length < length) this[key] = new Float32Array(length);
      return this[key].subarray(0, length);
    }
    _draw() {
      if (!this._dirty) return;
      this._dirty = false;
      const gl = this.gl, {scale} = this.camera;
      gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      const hover = this.hovered;
      const hoverNode = hover && this.nodeById.get(hover.id) === hover ? hover : null;
      // Edges: highlighted ones last so they draw on top.
      const edges = [];
      for (const e of this.edges) {
        if (e.hidden || e.from === e.to) continue;
        const a = this.nodeById.get(e.from), b = this.nodeById.get(e.to);
        if (a && b && !a.hidden && !b.hidden) edges.push([e, a, b]);
      }
      edges.sort((p, q) => (p[0].highlighted || p[0] === hover) - (q[0].highlighted || q[0] === hover));
      const ed = this._instanceData("_edgeData", edges.length * 14);
      let arrows = 0;
      edges.forEach(([e, a, b], i) => {
        const strong = e.highlighted || e === hover;
        const touchesHover = hoverNode && (e.from === hoverNode.id || e.to === hoverNode.id);
        const c = hexColor(e.color || '#64748b', strong ? 0.95 : e.ghost ? (touchesHover ? 0.65 : 0.3) : e.opacity ?? 0.35);
        ed.set([a.x, a.y, b.x, b.y, a.size || 7, b.size || 7, ...c, e.width || 1, e.dashed ? 1 : 0, e.arrow ? 1 : 0, strong ? 2 : 0], i * 14);
        if (e.arrow) arrows++;
      });
      const uniforms = p => {
        gl.useProgram(p.program);
        gl.uniform2f(p.uniforms.u_center, this.camera.x, this.camera.y);
        gl.uniform1f(p.uniforms.u_scale, scale);
        gl.uniform2f(p.uniforms.u_viewport, this.width, this.height);
      };
      if (edges.length) {
        gl.bindBuffer(gl.ARRAY_BUFFER, this.edgeGeo.buffer); gl.bufferData(gl.ARRAY_BUFFER, ed, gl.DYNAMIC_DRAW);
        uniforms(this.programs.edge); gl.bindVertexArray(this.edgeGeo.vao);
        gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, edges.length);
        if (arrows) {
          // Arrow pass reuses the instance buffer; non-arrow edges collapse to nothing.
          const arrowData = this._instanceData("_arrowData", arrows * 14);
          let j = 0;
          for (let i = 0; i < edges.length; i++) if (ed[i * 14 + 12]) arrowData.set(ed.subarray(i * 14, i * 14 + 14), (j++) * 14);
          gl.bufferData(gl.ARRAY_BUFFER, arrowData, gl.DYNAMIC_DRAW);
          uniforms(this.programs.arrow); gl.bindVertexArray(this.arrowGeo.vao);
          gl.drawArraysInstanced(gl.TRIANGLES, 0, 3, arrows);
        }
      }
      const nodes = this.nodes.filter(n => !n.hidden);
      nodes.sort((a, b) => (a.highlighted || a === hover) - (b.highlighted || b === hover));
      const nd = this._instanceData("_nodeData", nodes.length * 12);
      nodes.forEach((n, i) => nd.set([n.x, n.y, n.size || 7, ...hexColor(n.color, n.ghost && n !== hover && !n.highlighted ? 0.5 : 1), n.highlighted || n === hover ? 1 : 0, ...hexColor(n.color2 || n.color, 1)], i * 12));
      gl.bindBuffer(gl.ARRAY_BUFFER, this.nodeGeo.buffer); gl.bufferData(gl.ARRAY_BUFFER, nd, gl.DYNAMIC_DRAW);
      uniforms(this.programs.node); gl.bindVertexArray(this.nodeGeo.vao);
      gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, nodes.length);
      gl.bindVertexArray(null);
      this._drawLabels(nodes);
    }
    // Labels: hovered/selected always; others largest-first while they fit.
    _drawLabels(nodes) {
      const ctx = this.ctx, dpr = this.dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, this.width, this.height);
      ctx.font = '12px system-ui, sans-serif'; ctx.textBaseline = 'middle';
      const forced = n => n.highlighted || n === this.hovered;
      const candidates = nodes.filter(n => n.label && (forced(n) || (!n.ghost && (n.size || 7) * this.camera.scale >= 5)));
      candidates.sort((a, b) => forced(b) - forced(a) || (b.size || 7) - (a.size || 7));
      const taken = [];
      let drawn = 0;
      for (const n of candidates) {
        const p = this.worldToScreen(n.x, n.y), r = Math.max((n.size || 7) * this.camera.scale, 1.5);
        const text = n.label, w = ctx.measureText(text).width + 6, h = 16;
        const box = {x: p.x + r + 3, y: p.y - h / 2, w, h};
        if (box.x > this.width || box.x + w < 0 || box.y > this.height || box.y + h < 0) continue;
        const strong = forced(n);
        if (!strong && (drawn >= 300 || taken.some(t => box.x < t.x + t.w && t.x < box.x + w && box.y < t.y + t.h && t.y < box.y + h))) continue;
        taken.push(box); drawn++;
        ctx.fillStyle = strong ? '#e2e8f0' : 'rgba(15,23,42,0.78)';
        ctx.fillRect(box.x, box.y, w, h);
        ctx.fillStyle = strong ? '#000000' : '#e5e5e5';
        ctx.fillText(text, box.x + 3, p.y + 0.5);
      }
    }

    // ---- layout -------------------------------------------------------------
    _visibleLayoutLinks() {
      const placed = n => n && !n.hidden && !n.ghost;  // faded context dots stay out of the layout
      return this.edges.filter(e => !e.hidden && !e.ghost && e.from !== e.to && placed(this.nodeById.get(e.from)) && placed(this.nodeById.get(e.to)));
    }
    startLayout(spacing = 240) {
      this.stopLayout();
      this._watchLayoutVisibility();
      this._layoutInvalid = false;
      const nodes = this.nodes.filter(n => !n.hidden && !n.ghost);
      this._layoutNodes = nodes; this._layoutLinks = this._visibleLayoutLinks(); this._layoutSpacing = spacing;
      const index = new Map(nodes.map((n, i) => [n.id, i])), n = nodes.length;
      const positions = new Float32Array(n * 2), radii = new Float32Array(n), active = new Uint8Array(n);
      nodes.forEach((node, i) => { positions[2 * i] = node.x; positions[2 * i + 1] = node.y; radii[i] = node.size || 7; });
      const links = this._layoutLinks;
      // Animate only the largest connected group; isolates and small groups
      // stay where they are (repulsion would fling them into a far ring).
      const adjacency = new Map();
      for (const e of links) {
        const a = index.get(e.from), b = index.get(e.to);
        if (!adjacency.has(a)) adjacency.set(a, []);
        if (!adjacency.has(b)) adjacency.set(b, []);
        adjacency.get(a).push(b); adjacency.get(b).push(a);
      }
      const seen = new Set();
      let largest = [];
      for (const startIndex of adjacency.keys()) {
        if (seen.has(startIndex)) continue;
        const group = [startIndex]; seen.add(startIndex);
        for (let q = 0; q < group.length; q++) for (const next of adjacency.get(group[q])) if (!seen.has(next)) { seen.add(next); group.push(next); }
        if (group.length > largest.length) largest = group;
      }
      for (const i of largest) active[i] = 1;
      const edges = new Uint32Array(links.length * 2), weights = new Float32Array(links.length);
      links.forEach((e, j) => { edges[2 * j] = index.get(e.from); edges[2 * j + 1] = index.get(e.to); weights[j] = 1 / (e.pull || 1); });
      const url = URL.createObjectURL(new Blob([LAYOUT_WORKER], {type: 'text/javascript'}));
      this.worker = new Worker(url);
      URL.revokeObjectURL(url);
      this._layoutIndex = index;
      const worker = this.worker;
      this.worker.onmessage = ({data}) => {
        if (this.worker !== worker) return;
        for (const i of largest) {
          const node = nodes[i];
          if (this._draggedLayoutNode === node) continue;
          node.x = data[2 * i]; node.y = data[2 * i + 1];
        }
        worker.postMessage({type: 'recycle', positions: data}, [data.buffer]);
        this.refresh();
      };
      this.worker.postMessage({type: 'start', positions, radii, active, edges, weights, k: spacing * 0.35});
      this.layoutRunning = true;
    }
    stopLayout() {
      if (this.worker) { this.worker.terminate(); this.worker = null; }
      this.layoutRunning = false;
    }
    _pin(node, on) {
      this._draggedLayoutNode = on ? node : null;
      if (!this.worker) return;
      const index = this._layoutIndex.get(node.id);
      if (index === undefined) return;
      this.worker.postMessage(on ? {type: 'pin', index, x: node.x, y: node.y} : {type: 'unpin', index});
    }
  }

  global.GraphRenderer = GraphRenderer;
})(typeof window !== 'undefined' ? window : globalThis);
