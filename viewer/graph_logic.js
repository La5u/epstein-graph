'use strict';
// Pure viewer logic (no DOM, no renderer): filtering, counts, paths, search.
// Loaded by app.js in the page and by scripts/test_visualization.py in Node.

// Audit/label-rule verdicts that mean "not an individual's name".
const NON_NAME_QUALITY = new Set(['group_or_role', 'not_a_name', 'boilerplate', 'ocr_garbage', 'redaction_or_placeholder']);
const CO_MENTION_TYPES = new Set(['shares_exported_efta_reference', 'co_mentioned_in_documents', 'victim_document_co_mention', 'same_document']);
const RELATIONSHIP_LABELS = {
  victim_of: 'survivor of',
  victim_document_co_mention: 'survivor co-mention',
  flew_together: 'flew together',
  same_day_presence: 'same-day mentions',
  same_day_document_mentions: 'same-day mentions',
  co_mentioned_in_documents: 'co-mentioned',
  same_document: 'in the same document',
  shares_exported_efta_reference: 'shared document reference',
};

function relationshipLabel(kind) {
  if (!kind) return '';
  return RELATIONSHIP_LABELS[kind] || String(kind).replace(/_/g, ' ');
}

function entityChoice(n) {
  return `${n.record.name} [${n.record.entity_type}; ${n.id}]`;
}

// People carrying a hidden legal-status flag disappear with their links.
function filterStatuses(nodes, edges, hiddenStatuses) {
  const kept = nodes.filter(n => !(n.record.legal_status_flags || []).some(s => hiddenStatuses.has(s)));
  const ids = new Set(kept.map(n => n.id));
  return {nodes: kept, edges: edges.filter(e => ids.has(e.from) && ids.has(e.to))};
}

// hide: {unresolved, coMentions, linkedCoMentions, artifacts}
function filterLayers(nodes, edges, relationshipType, hide = {}) {
  const kept = nodes.filter(n => (!hide.incidental || (n.record.relevance || n.record.metadata?.relevance) !== 'incidental')
    && (!hide.unresolved || n.record.entity_type !== 'participant')
    && !(hide.artifacts && (n.record.suspected_parse_artifact || NON_NAME_QUALITY.has(n.record.label_quality))));
  const ids = new Set(kept.map(n => n.id));
  return {nodes: kept, edges: edges.filter(e => {
    const kind = e.record.relationship_type;
    if (!ids.has(e.from) || !ids.has(e.to)) return false;
    if (relationshipType && kind !== relationshipType) return false;
    if (hide.coMentions && CO_MENTION_TYPES.has(kind)) return false;
    return !(hide.linkedCoMentions && kind === 'co_mentioned_in_documents' && e.record.metadata?.new_link === false);
  })};
}

// Whether a node's type is shown. hiddenTypes holds types ('place') and subtypes ('place:airport').
// Half-and-half nodes (dual_status: also a survivor) show when any of their types is shown.
function typeShown(record, hiddenTypes) {
  const type = record.entity_type;
  if (!hiddenTypes.has(type) && !(record.subtype && hiddenTypes.has(`${type}:${record.subtype}`))) return true;
  return !!record.dual_status && (!hiddenTypes.has('survivor') || !hiddenTypes.has('participant'));
}

// Hidden entity types disappear with their links.
function entityVisibility(nodes, edges, hiddenTypes, relationshipType) {
  const visible = new Set(nodes.filter(n => typeShown(n.record, hiddenTypes)).map(n => n.id));
  const visibleEdges = new Set();
  for (const e of edges) {
    if (relationshipType && e.record.relationship_type !== relationshipType) continue;
    if (visible.has(e.from) && visible.has(e.to)) visibleEdges.add(e.id);
  }
  // A dot whose only document partners are of hidden types would float alone: it waits for them.
  const linked = new Set();
  for (const e of edges) if (visibleEdges.has(e.id) && e.from !== e.to) { linked.add(e.from); linked.add(e.to); }
  for (const n of nodes) if (visible.has(n.id) && !linked.has(n.id)
    && n.record.document_isolation_reason === 'document partners are hidden by this view') visible.delete(n.id);
  return {visible, visibleEdges};
}

// Links from a visible node to an eligible node hidden only by its type: drawn faded
// (with that node as a faded "ghost") for context, e.g. emails' links to people.
function contextEdges(edges, visible, eligible, relationshipType) {
  return edges.filter(e => e.from !== e.to && (!relationshipType || e.record.relationship_type === relationshipType)
    && ((visible.has(e.from) && !visible.has(e.to) && eligible.has(e.to)) || (visible.has(e.to) && !visible.has(e.from) && eligible.has(e.from))));
}

// Positions pulled toward the visible dots' centre when few types are shown, so one
// type is not scattered over the whole canvas: factor 1 (home layout) .. 0.25.
function compactFactor(visibleCount, totalCount) {
  if (!totalCount || visibleCount >= 0.5 * totalCount) return 1;
  return Math.max(0.25, Math.min(1, Math.sqrt(visibleCount / totalCount) * 1.4));
}

// Which visible links to draw: all (''), those of focused nodes ('focus'),
// or those touching one entity type ('type:person').
function linkSelection(edges, visibleEdges, mode, focusIds, typeOf) {
  if (!mode) return visibleEdges;
  const wanted = mode === 'focus' ? (id => focusIds.has(id)) : (id => typeOf(id) === mode.slice(5));
  return new Set(edges.filter(e => visibleEdges.has(e.id) && (wanted(e.from) || wanted(e.to))).map(e => e.id));
}

// Unique neighbours per node (undirected, no self-loops, repeats counted once).
function connectionCounts(nodes, edges, relationshipType) {
  const neighbors = new Map(nodes.map(n => [n.id, new Set()]));
  for (const e of edges) {
    if (relationshipType && e.record.relationship_type !== relationshipType) continue;
    if (e.from === e.to) continue;
    neighbors.get(e.from)?.add(e.to);
    neighbors.get(e.to)?.add(e.from);
  }
  return new Map([...neighbors].map(([id, set]) => [id, set.size]));
}

// Dot radius 7-34, logarithmic in connections.
function connectionSize(count, maxCount) {
  // Radius 3..70 on a log scale, curved so mid-sized hubs stand apart from the many
  // one- or two-link nodes instead of all landing near the same size.
  return 3 + 67 * (maxCount > 0 ? Math.log1p(count) / Math.log1p(maxCount) : 0) ** 1.6;
}

// Push overlapping circles {x, y, radius} apart in place; returns whether any moved.
// Grid cells fit a typical dot; each pair is found from its larger dot, which searches as far
// as its own diameter (so the few big hubs don't make every small dot scan huge cells).
function separateDots(points, gap = 12, maxPasses = 24) {
  if (points.length < 2) return false;
  const radii = points.map(p => p.radius).sort((x, y) => x - y);
  const cell = Math.max(1, radii[Math.floor(radii.length * 0.9)] * 2 + gap + 1);
  const larger = (a, i, b, j) => a.radius > b.radius || (a.radius === b.radius && i < j);
  let moved = false;
  for (let pass = 0; pass < maxPasses; pass++) {
    const grid = new Map();
    for (let i = 0; i < points.length; i++) {
      const p = points[i], key = Math.floor(p.x / cell) * 4194304 + Math.floor(p.y / cell);
      const list = grid.get(key);
      if (list) list.push(i); else grid.set(key, [i]);
    }
    let collisions = 0;
    for (let i = 0; i < points.length; i++) {
      const a = points[i], cx = Math.floor(a.x / cell), cy = Math.floor(a.y / cell);
      const reach = Math.ceil((2 * a.radius + gap) / cell);
      for (let dx = -reach; dx <= reach; dx++) for (let dy = -reach; dy <= reach; dy++) {
        const list = grid.get((cx + dx) * 4194304 + cy + dy);
        if (!list) continue;
        for (const j of list) {
          if (j === i) continue;
          const b = points[j];
          if (!larger(a, i, b, j)) continue;
          const minimum = a.radius + b.radius + gap;
          let vx = b.x - a.x, vy = b.y - a.y, distance = Math.hypot(vx, vy);
          if (distance >= minimum) continue;
          if (distance < 1e-9) { const angle = (Math.min(i, j) * 31 + Math.max(i, j) * 17) * 2.399963229728653; vx = Math.cos(angle); vy = Math.sin(angle); distance = 1; }
          const shift = (minimum - Math.hypot(b.x - a.x, b.y - a.y)) / 2 + 0.01;
          const mx = vx / distance * shift, my = vy / distance * shift;
          a.x -= mx; a.y -= my; b.x += mx; b.y += my;
          collisions++; moved = true;
        }
      }
    }
    if (!collisions) break;
  }
  return moved;
}

// Positions for neighbours gathered around a centre in non-touching rings.
// centre {x, y, radius}; neighbours [{id, radius}] -> [{id, x, y}]
function ringPositions(center, neighbors, gap = 20) {
  if (!neighbors.length) return [];
  const largest = Math.max(...neighbors.map(n => n.radius)), pitch = 2 * largest + gap;
  let ring = center.radius + largest + gap, index = 0;
  const out = [];
  while (index < neighbors.length) {
    // Chord spacing guarantees dots on a ring don't touch.
    const capacity = Math.max(1, Math.floor(Math.PI / Math.asin(Math.min(1, pitch / (2 * ring)))));
    const count = Math.min(capacity, neighbors.length - index);
    for (let slot = 0; slot < count; slot++) {
      const angle = -Math.PI / 2 + slot * 2 * Math.PI / count;
      out.push({id: neighbors[index++].id, x: center.x + ring * Math.cos(angle), y: center.y + ring * Math.sin(angle)});
    }
    ring += pitch;
  }
  return out;
}

// Fewest-hop path over allowed links; returns the edge list or null.
function shortestPath(from, to, incident, allowed) {
  const previous = new Map([[from, null]]), queue = [from];
  for (let i = 0; i < queue.length; i++) {
    const id = queue[i];
    if (id === to) break;
    for (const e of incident.get(id) || []) {
      if (!allowed.has(e.id)) continue;
      const other = e.from === id ? e.to : e.from;
      if (!previous.has(other)) { previous.set(other, {id, edge: e}); queue.push(other); }
    }
  }
  if (!previous.has(to)) return null;
  const path = [];
  for (let id = to; id !== from; id = previous.get(id).id) path.unshift(previous.get(id).edge);
  return path;
}

// Neighbours over allowed links: [[otherId, {records, types:Set}]], most records first.
function neighborRows(id, incident, allowed, nameOf) {
  const rows = new Map();
  for (const e of incident.get(id) || []) {
    if (!allowed.has(e.id)) continue;
    const other = e.from === id ? e.to : e.from;
    if (other === id) continue;
    const row = rows.get(other) || {records: 0, types: new Set()};
    row.records += e.record.weight ?? 1; row.types.add(relationshipLabel(e.record.relationship_type));
    rows.set(other, row);
  }
  return [...rows].sort((a, b) => b[1].records - a[1].records || String(nameOf(a[0])).localeCompare(nameOf(b[0])));
}

// Exact datalist choice, else exact name, else substring; prefer visible, then
// eligible. Never silently picks one of several matches.
function matchNode(query, nodes, isVisible, isEligible) {
  query = String(query || '').trim().toLowerCase();
  if (!query) return null;
  const chosen = nodes.find(n => entityChoice(n).toLowerCase() === query);
  if (chosen) return chosen;
  const names = n => [n.record.name, ...(n.record.websites || [])].map(x => x.toLowerCase());
  const exact = nodes.filter(n => names(n).includes(query));
  const candidates = exact.length ? exact : nodes.filter(n => names(n).some(x => x.includes(query)));
  const visible = candidates.filter(n => isVisible(n.id)), eligible = candidates.filter(n => isEligible(n.id));
  const preferred = visible.length ? visible : eligible.length ? eligible : candidates;
  return preferred.length === 1 ? preferred[0] : null;
}

// Page-image sets (one .jpg per Bates number) link to the page's own image; PDFs take #page=.
function pageUrl(url, page) {
  if (!Number.isInteger(page)) return url;
  const m = /^(.*\D)(\d+)\.jpg$/.exec(url);
  return m ? m[1] + String(Number(m[2]) + page).padStart(m[2].length, '0') + '.jpg' : url + `#page=${page + 1}`;
}

function documentUrl(urls, efta) {
  const entry = urls?.documents?.[efta];
  if (typeof entry === 'string') return entry;
  const t = Number.isInteger(entry) ? urls.templates[entry] : null;
  return t ? (t[1] === null ? t[0] : t[0] + efta + t[1]) : undefined;
}

async function decodeGzip(base64) {
  const binary = atob(base64), bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
}

// Share concurrent reads and retain only a few recently used chunks. Never
// attach inflated records to graph objects: eviction must actually release them.
class DetailStore {
  constructor(read, chunkSize = 256, maxChunks = 4, maxBytes = 4 * 1024 * 1024) {
    this.read = read; this.chunkSize = chunkSize; this.maxChunks = maxChunks; this.maxBytes = maxBytes;
    this.cache = new Map(); this.pending = new Map(); this.bytes = 0;
  }
  async get(index) {
    const chunk = Math.floor(index / this.chunkSize), offset = index % this.chunkSize;
    let entry = this.cache.get(chunk);
    if (entry) { this.cache.delete(chunk); this.cache.set(chunk, entry); }
    else {
      let pending = this.pending.get(chunk);
      if (!pending) {
        pending = (async () => {
          const text = await decodeGzip(await this.read(chunk));
          const entry = {records: JSON.parse(text), bytes: text.length * 2};
          this.cache.set(chunk, entry); this.bytes += entry.bytes;
          while (this.cache.size > this.maxChunks || this.bytes > this.maxBytes) {
            const oldest = this.cache.keys().next().value;
            this.bytes -= this.cache.get(oldest).bytes; this.cache.delete(oldest);
          }
          return entry;
        })();
        this.pending.set(chunk, pending);
      }
      try { entry = await pending; }
      finally { if (this.pending.get(chunk) === pending) this.pending.delete(chunk); }
    }
    return entry.records[offset];
  }
}

const EDGE_COLORS = {same_document: '#38bdf8', co_mentioned_in_documents: '#f59e0b', same_day_presence: '#a78bfa', same_day_document_mentions: '#a78bfa',
  flew_to_or_from: '#c084fc', mentioned_place_in_email: '#e879f9', victim_of: '#f87171', victim_document_co_mention: '#fca5a5'};
function edgeTitle(r) {
  const kind = r.relationship_type, weight = r.weight;
  if (['sent_to', 'cc_to', 'header_label_association', 'upstream_resolved_header_identity'].includes(kind))
    return `${kind} · ${weight} source message records`;
  if (['co_mentioned_in_documents', 'same_document'].includes(kind)) return `${kind} · ${weight} shared documents (reading lead, not a relationship)`;
  if (['same_day_presence', 'same_day_document_mentions'].includes(kind))
    return `same-day dated mentions · ${weight} shared dates in flight logs / schedules (not verified presence or proof they met)`;
  if (kind === 'shares_exported_efta_reference') return `${kind} · ${weight} shared EFTA references`;
  if (kind === 'victim_of') return `survivor_of · upstream survivor-perpetrator allegation link (${weight} record; abuse type and dates in the record). An allegation recorded upstream, not a proven offence; survivors carry a curated label, not a corpus-derived identity.`;
  if (kind === 'victim_document_co_mention') {
    const docs = r.metadata?.shared_documents ?? weight, score = r.metadata?.specificity_score ?? weight;
    return `survivor_document_co_mention · ${docs} shared EFTA documents with a censored survivor designator, specificity ${score} (each document counts 1/(names in it - 1), so long compilations naming many people add little). A reading lead, not a relationship; no survivor identity is resolved.`;
  }
  return `${kind} · upstream weight ${weight} (type-specific meaning, not confidence)`;
}

if (typeof module !== 'undefined') {
  module.exports = {typeShown, NON_NAME_QUALITY, CO_MENTION_TYPES, relationshipLabel, entityChoice, filterStatuses, filterLayers, contextEdges, compactFactor,
    entityVisibility, linkSelection, connectionCounts, connectionSize, separateDots, ringPositions, shortestPath,
    neighborRows, matchNode, documentUrl, pageUrl, decodeGzip, DetailStore, edgeTitle};
}
