'use strict';
// Relationship explorer UI. Expects globals: data (payload), COLORS,
// GraphRenderer (renderer.js) and the graph_logic.js functions.
const $ = id => document.getElementById(id);
const detailStore = new DetailStore(chunk => {
  const block = $('detail-' + chunk);
  if (!block) throw new Error('Missing embedded detail chunk ' + chunk);
  return block.textContent;
}, data.detailChunkSize);
let activeReveal = {}, activePages = {};
async function fullRecord(item) {
  if (item.detail === undefined) return revealRecord(item.record);
  const raw = await detailStore.get(item.detail), hot = item.record;
  const record = {...raw, ...hot};
  if (raw.documents) record.documents = raw.documents;
  if (raw.metadata) record.metadata = {...raw.metadata, ...hot.metadata};
  return revealRecord(record);
}
function revealRecord(r) {
  // Run on every access, including cache hits after unlocking. No traversal of
  // compressed details on unlock, and no inflated records retained by skeletons.
  if (activeReveal[r.id]?.fields) Object.assign(r, activeReveal[r.id].fields);
  for (const m of r.merged_identities || []) if (activeReveal[m.id]) m.value = activeReveal[m.id].value;
  for (const x of r.expand || []) if (activeReveal[x.id]) x.value = activeReveal[x.id].value;
  for (const items of Object.values(r.identity_review || {})) for (const x of items) {
    if (activeReveal[x.id]) x.value = activeReveal[x.id].value;
    if (activeReveal[x.canonical]) x.canonicalValue = activeReveal[x.canonical].value;
    if (activeReveal[x.member]) x.memberValue = activeReveal[x.member].value;
  }
  return r;
}
function detailError(error) { setStatus('Unable to load details: ' + error.message); }
for (const e of data.edges) if (e.detail !== undefined) {
  const kind = e.record.relationship_type;
  e.color = EDGE_COLORS[kind] || '#64748b';
  e.dashed = CO_MENTION_TYPES.has(kind) || ['same_day_presence', 'same_day_document_mentions'].includes(kind);
  e.arrow = ['sent_to', 'cc_to'].includes(kind);
}
const nodeById = new Map(data.nodes.map(n => [n.id, n]));
const edgeById = new Map(data.edges.map(e => [e.id, e]));
const incident = new Map(data.nodes.map(n => [n.id, []]));
for (const e of data.edges) {
  incident.get(e.from)?.push(e);
  if (e.to !== e.from) incident.get(e.to)?.push(e);
}
// PUBLIC_REVEAL contains approved contacts and stable HMAC labels for all others.
applyReveal(Object.fromEntries(Object.entries(PUBLIC_REVEAL).map(([id, value]) => [id, {value}])));

const PRESENT_TYPES = new Set(data.nodes.map(n => n.record.entity_type));
// Header names are hashes until the viewer password is entered, so they start hidden.
let unlocked = false;
// Places start with Epstein's properties only.
const defaultHidden = () => new Set([...['address', 'airport', 'institution', 'city', 'other'].map(s => 'place:' + s), ...(unlocked ? [] : ['participant'])]);
// Subcategories are a secondary filter: under MIN_SUBTYPE nodes they join "other"
// (postal codes join addresses; "ner" is just how an organization was found), and a
// type left with fewer than two real subcategories shows none. The detail line keeps
// the exact kind. Epstein's properties always stay a subcategory (shown by default).
const MIN_SUBTYPE = 20, KEEP_SUBTYPES = new Set(['place:property']);
const FOLD_SUBTYPE = {'place:postal_code': 'address', 'organization:ner': 'other'};
function foldSubtypes(nodes) {
  const counts = new Map(), key = r => `${r.entity_type}:${r.subtype}`;
  for (const n of nodes) if (n.record.subtype) counts.set(key(n.record), (counts.get(key(n.record)) || 0) + 1);
  const target = k => FOLD_SUBTYPE[k] || (counts.get(k) < MIN_SUBTYPE && !KEEP_SUBTYPES.has(k) ? 'other' : k.split(':')[1]);
  const kept = {};
  for (const k of counts.keys()) (kept[k.split(':')[0]] ||= new Set()).add(target(k));
  const chipless = new Set(Object.keys(kept).filter(t => kept[t].size < 2));
  for (const n of nodes) {
    const r = n.record; if (!r.subtype || chipless.has(r.entity_type)) continue;
    const to = target(key(r));
    if (to !== r.subtype) { r.exact_subtype = r.subtype; r.subtype = to; }
  }
  return chipless;
}
const CHIPLESS = foldSubtypes(data.nodes);  // types shown without subcategory chips or shades
const SUBTYPES = {};  // type -> [subtype, count], most frequent first
for (const n of data.nodes) if (n.record.subtype) (SUBTYPES[n.record.entity_type] ||= new Map()).set(n.record.subtype, ((SUBTYPES[n.record.entity_type].get(n.record.subtype)) || 0) + 1);
for (const t in SUBTYPES) SUBTYPES[t] = [...SUBTYPES[t]].sort((a, b) => b[1] - a[1]);
// Subtypes are shades of their type's colour, darkest for the most frequent.
const shade = (hex, t) => {
  const n = parseInt(hex.slice(1), 16), to = t < 0 ? 0 : 255, f = Math.abs(t);
  return '#' + [n >> 16, (n >> 8) & 255, n & 255].map(c => Math.round(c + (to - c) * f).toString(16).padStart(2, '0')).join('');
};
const SHADES = {};
for (const [type, subs] of Object.entries(SUBTYPES)) subs.forEach(([sub], i) => {
  SHADES[`${type}:${sub}`] = subs.length < 2 || CHIPLESS.has(type) ? COLORS[type] : shade(COLORS[type] || '#aaaaaa', -0.3 + 0.85 * i / (subs.length - 1));
});
const typeColor = r => SHADES[`${r.entity_type}:${r.subtype}`] || COLORS[r.entity_type] || '#aaaaaa';
// Also named as a co-conspirator and publicly a self-described victim: half survivor red, half person white.
const nodeColors = n => { n.color = n.record.dual_status ? COLORS.survivor : typeColor(n.record); n.color2 = n.record.dual_status ? COLORS.person : undefined; };
for (const n of data.nodes) { nodeColors(n); n.size = 7; n.count = 0; n.homeX = n.x; n.homeY = n.y; }
const state = {
  hiddenTypes: defaultHidden(),
  hide: {artifacts: true, linkedCoMentions: true, incidental: true},
  hiddenStatuses: new Set(),
  relationship: '',
  linkMode: '',
  hover: null,
  detail: null,
  selectedNodes: new Set(),
  selectedEdges: new Set(),
  visibleLinks: new Set(),
  eligible: new Set(),
  spacing: 240,
};

const renderer = new GraphRenderer($('graph'), {tooltip});
renderer.setGraph(data.nodes, data.edges);

function setStatus(text = '') {
  const filters = [...state.hiddenTypes].filter(t => PRESENT_TYPES.has(t) || (SUBTYPES[t.split(':')[0]] || []).some(([sub]) => t === `${t.split(':')[0]}:${sub}`)).length + state.hiddenStatuses.size + Number(!!state.relationship)
    + Number(!!state.hide.isolates)
    + Number(!!(state.hide.coMentions || state.hide.linkedCoMentions)) + Number(!!state.linkMode);
  const dots = data.nodes.filter(n => !n.hidden && !n.ghost).length, links = data.edges.filter(e => !e.hidden && !e.ghost).length;
  $('status').textContent = `${dots.toLocaleString()} dots · ${links.toLocaleString()} links · ${filters} active filters · layout ${renderer.layoutRunning ? 'animating' : 'frozen'}${text ? ' · ' + text : ''}`;
}
function element(tag, text) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; return e; }

// ---- filtering ---------------------------------------------------------------
function update() {
  const statusOk = filterStatuses(data.nodes, data.edges, state.hiddenStatuses);
  const eligible = filterLayers(statusOk.nodes, statusOk.edges, state.relationship, state.hide);
  state.eligible = new Set(eligible.nodes.map(n => n.id));
  const view = entityVisibility(eligible.nodes, eligible.edges, state.hiddenTypes, state.relationship);
  const counts = connectionCounts(eligible.nodes.filter(n => view.visible.has(n.id)), eligible.edges.filter(e => view.visibleEdges.has(e.id)));
  if (state.hide.isolates) {
    for (const id of view.visible) if (!counts.get(id)) view.visible.delete(id);
    for (const e of eligible.edges) if (!view.visible.has(e.from) || !view.visible.has(e.to)) view.visibleEdges.delete(e.id);
  }
  state.visibleLinks = view.visibleEdges;
  state.context = contextEdges(eligible.edges, view.visible, state.eligible, state.relationship);
  // Context for every dot only when one type of at most 200 dots is shown (else hovered / selected only).
  state.singleType = view.visible.size <= 200 && new Set(eligible.nodes.filter(n => view.visible.has(n.id)).map(n => n.record.entity_type)).size <= 1;
  const maxCount = Math.max(0, ...counts.values());
  for (const n of data.nodes) {
    n.count = counts.get(n.id) || 0;
    n.size = connectionSize(n.count, maxCount);
    n.hidden = n.baseHidden = !view.visible.has(n.id);
  }
  renderer.invalidateLayout();  // dot sizes follow the view's connection counts
  compact(view.visible);
  if (nodeById.get(state.hover)?.hidden) state.hover = null;
  // Selections that the filters hid are dropped.
  select([...state.selectedNodes].filter(id => !nodeById.get(id).hidden), [...state.selectedEdges].filter(id => state.visibleLinks.has(id)));
  if (!renderer.layoutRunning) spreadDots();
  const detail = state.detail;
  if (detail?.node !== undefined && view.visible.has(detail.node)) showNode(nodeById.get(detail.node));
  else if (detail?.edge !== undefined && state.visibleLinks.has(detail.edge)) showEdge(edgeById.get(detail.edge));
  else if (!detail?.path || detail.path.some(id => !state.visibleLinks.has(id)) || detail.nodes.some(id => !view.visible.has(id))) clearDetails();
  setStatus();
}

function focusIds() {
  const ids = new Set(state.selectedNodes);
  if (state.hover !== null) ids.add(state.hover);
  return ids;
}
function applyLinks() {
  const focus = focusIds();
  const shown = linkSelection(data.edges, state.visibleLinks, state.linkMode, focus, id => nodeById.get(id)?.record.entity_type);
  // Faded context: links to nodes hidden only by their type, for every visible dot when one
  // type is shown, else for the hovered / selected ones.
  const ghostEdges = new Set();
  for (const n of data.nodes) { n.hidden = n.baseHidden ?? n.hidden; n.ghost = false; }
  for (const e of state.context || []) {
    const inside = nodeById.get(e.from).baseHidden ? e.to : e.from, outside = inside === e.from ? e.to : e.from;
    if (!state.singleType && !focus.has(inside) && !focus.has(outside)) continue;
    ghostEdges.add(e.id);
    const ghost = nodeById.get(outside); ghost.hidden = false; ghost.ghost = true;
  }
  for (const e of data.edges) {
    e.ghost = ghostEdges.has(e.id);
    e.hidden = !shown.has(e.id) && !e.ghost; e.highlighted = state.selectedEdges.has(e.id);
  }
  renderer.refresh();
  setStatus();
}
// Few types shown: pull every dot toward the visible dots' centre (home layout otherwise).
const PLACE_TYPES = new Set(['place']);  // mostly hidden by default; the default view is not compacted
function compact(visible) {
  const shown = data.nodes.filter(n => visible.has(n.id));
  const f = compactFactor(shown.length, data.nodes.filter(n => !PLACE_TYPES.has(n.record.entity_type)).length);
  if (f === (state.compact?.f ?? 1)) return;  // unchanged: keep drags and layout moves
  saveHome();
  const cx = shown.reduce((s, n) => s + n.homeX, 0) / (shown.length || 1), cy = shown.reduce((s, n) => s + n.homeY, 0) / (shown.length || 1);
  state.compact = {f, cx, cy};
  for (const n of data.nodes) { n.x = cx + (n.homeX - cx) * f; n.y = cy + (n.homeY - cy) * f; }
}
// Current positions back to home coordinates (undoing the compaction), so moves survive it.
function saveHome() {
  const {f = 1, cx = 0, cy = 0} = state.compact || {};
  for (const n of data.nodes) { n.homeX = cx + (n.x - cx) / f; n.homeY = cy + (n.y - cy) / f; }
}
function select(nodeIds = [], edgeIds = []) {
  state.selectedNodes = new Set(nodeIds); state.selectedEdges = new Set(edgeIds);
  for (const n of data.nodes) n.highlighted = state.selectedNodes.has(n.id);
  applyLinks();
}

// Push overlapping dots apart (only while the layout is frozen).
// Overlap removal runs in ~8 ms slices across animation frames: a filter change can need
// hundreds of passes over thousands of dots, which blocked clicks for half a second.
// state.spreading is true until the dots have settled (tests wait for it).
let spreadJob = 0;
function spreadDots() {
  const job = ++spreadJob, points = data.nodes.filter(n => !n.hidden && !n.ghost).map(n => ({n, x: 0, y: 0, radius: 0}));
  let passes = 0;
  state.spreading = true;
  const step = () => {
    if (job !== spreadJob) return;
    for (const p of points) { p.x = p.n.x; p.y = p.n.y; p.radius = p.n.size; }  // keeps drags made meanwhile
    const start = performance.now();
    let settled = false;
    do {
      if (!separateDots(points, 12, 1)) { settled = true; break; }
    } while (++passes < 500 && performance.now() - start < 8);
    for (const p of points) { p.n.x = p.x; p.n.y = p.y; }
    renderer.refresh();
    if (settled || passes >= 500) state.spreading = false;
    else requestAnimationFrame(step);
  };
  step();
}
function stopSpreading() { spreadJob++; state.spreading = false; }

// ---- details panel -------------------------------------------------------------
function documentLinks(docs, title) {
  const box = element('div');
  if (!docs || !docs.length) return box;
  box.appendChild(element('h2', title));
  for (const d of docs) {
    const row = element('div');
    // Only link official DOJ / archive.org URLs; everything else is plain text.
    const url = d.pdf_url || documentUrl(data.urls, d.efta_number);
    const page = Number.isInteger(d.page) ? d.page + 1 : null, label = d.efta_number + (page ? ` p.${page}` : '');
    if (typeof url === 'string' && (url.startsWith('https://www.justice.gov/') || url.startsWith('https://archive.org/'))) {
      const a = element('a', label); a.href = pageUrl(url, d.page); a.target = '_blank'; a.rel = 'noopener noreferrer'; row.appendChild(a);
    } else row.appendChild(element('span', label));
    // epsteinexposed.com only hosts the DOJ DataSet files; court-record sections and estate productions 404 there.
    if (/^EFTA\d{8}$/.test(d.efta_number || '') && typeof url === 'string' && url.startsWith('https://www.justice.gov/epstein/files/DataSet')) {
      const x = element('a', ' · readable copy'); x.href = 'https://epsteinexposed.com/documents/efta-' + d.efta_number.slice(4);
      x.target = '_blank'; x.rel = 'noopener noreferrer'; row.appendChild(x);
    }
    if (d.how) row.appendChild(element('span', ` · ${d.how}`)).className = 'muted';
    const info = d.document_info || data.docinfo?.[d.efta_number];  // document type, subpoena, FBI case, DOJ version status
    if (typeof info === 'string') row.appendChild(element('span', ` · ${info}`)).className = 'muted';
    box.appendChild(row);
  }
  return box;
}
// Non-PDF data behind a node or link: flight records, Jmail emails, profile pages.
function recordLinks(records, total, title) {
  const box = element('div');
  if (!records || !records.length) return box;
  box.appendChild(element('h2', total > records.length ? `${title} (${records.length} of ${total})` : title));
  for (const rec of records) {
    const row = element('div');
    if (typeof rec.url === 'string' && rec.url.startsWith('https://')) {
      const a = element('a', rec.ref); a.href = rec.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; row.appendChild(a);
    } else row.appendChild(element('span', rec.ref));
    row.appendChild(element('span', ` · ${rec.kind}`)).className = 'muted';
    box.appendChild(row);
  }
  return box;
}
const PAGE_PATH = /^(\.\.\/)*(data\/private\/)?node_docs\/[A-Za-z0-9_]+\.html$/;
function pageLink(page, anchor, text) {
  if (typeof page !== 'string' || !PAGE_PATH.test(page)) return element('div');
  const a = element('a', text); a.href = page + (anchor ? '#' + anchor : ''); a.target = '_blank';
  const div = element('div'); div.appendChild(a); return div;
}
// ---- merged / separated panel: what build_communications folded into this node ----
const RULES = {
  reviewed_identity_merge: 'reviewed merge', reviewed_audit_merge: 'reviewed merge',
  ocr_variant_of_more_frequent_address: 'OCR misreading of a more frequent address', ocr_glued_prefix: 'address with OCR junk glued on',
  same_gmail_mailbox: 'same Gmail mailbox (dots / +tags)', header_name_variant: 'spelling variant of a graph person',
  header_name_ocr_skeleton: 'same name up to OCR look-alike letters / spacing', header_name_one_edit: 'one letter different',
  header_name_truncated: 'cut-off spelling', header_names_point_to_one_identity: 'address whose header names all point to this person',
  same_account_alias_domain: 'same Apple account (me.com / mac.com / icloud.com)', address_spells_one_name: 'address spells this name',
  header_name_is_address: 'header name is the address local part', header_name_is_misread_address: 'header name is an address with the @ misread', header_name_on_one_identity_addresses: 'name written almost only on this person’s addresses',
  short_header_name_on_one_identity: 'one-word name / initials written almost only on this identity’s addresses',
  same_couple_spelling: 'same couple, other spelling', automated_company_sender: 'automated sender of the same company',
  document_names_one_person: 'its document names only this person with that name', reviewed_document_attribution: 'reviewed per document',
  header_name_middle_dropped: 'same name without the middle name', header_name_initial_unique: 'surname initial, only one such person',
  header_name_initial_dominant: 'surname initial, by far the most frequent such person', header_name_two_edits: 'two letters different (rare spelling)',
  header_name_short_first_name: 'short form of the first name', header_name_first_initial: 'first initial + surname',
  phone_one_digit_variant_same_person: 'phone number one digit different',
  airline_flight_number: 'flight number of this airline', same_aircraft_model: 'same aircraft model',
};
const ruleText = rule => RULES[String(rule).split(':')[0]] || String(rule);
let reasons = {};
function nameOf(id, value) { return value || nodeById.get(id)?.record.name || String(id); }
function reviewEntry(title, detail, reason, docsList, focus) {
  const box = element('details');
  box.className = 'merge';
  box.appendChild(element('summary', title));
  if (detail) box.appendChild(element('p', detail)).className = 'muted';
  if (reason) box.appendChild(element('p', 'Review: ' + reason));
  if (docsList && docsList.length) box.appendChild(documentLinks(docsList, 'Documents'));
  if (focus !== undefined && nodeById.get(focus)) { const b = element('button', 'Open that node'); b.onclick = () => focusNode(focus); box.appendChild(b); }
  return box;
}
function mergePanel(r) {
  const box = element('div'), review = r.identity_review || {};
  const merged = r.merged_identities || [], names = (r.original_names || []).filter(x => x !== r.name);
  if (!merged.length && !names.length && !Object.keys(review).length) return box;
  box.appendChild(element('h2', 'Merged / separated'));
  if (names.length) box.appendChild(element('p', 'Also written as: ' + names.join(' · ')));
  if (merged.length) {
    const total = r.merged_identity_count || merged.length;
    box.appendChild(element('p', `${total} identities merged in${total > merged.length ? ` (the ${merged.length} with most records listed)` : ''}:`)).className = 'muted';
    for (const m of merged) {
      const ocr = (m.ocr_artifacts || []).length ? ` · OCR: ${m.ocr_artifacts.join(', ')}` : '';
      box.appendChild(reviewEntry(`${nameOf(m.id, m.value)} — ${m.records ?? '?'} records`,
        `${ruleText(m.rule)} · ${m.confidence}${ocr}`, reasons[String(m.id)], m.source_documents, m.id));
    }
    if (r.merged_identities_by_rule) box.appendChild(element('p', 'By rule: ' + Object.entries(r.merged_identities_by_rule).map(([k, v]) => `${ruleText(k)} ${v}`).join(' · '))).className = 'muted';
  }
  for (const x of review.reattributed_in || []) {
    box.appendChild(reviewEntry(`${x.records} records moved here from “${nameOf(x.id, x.value)}”`, ruleText(x.rule), x.rule === 'reviewed_document_attribution' ? reasons[`attr:${x.id}:${r.id}`] : '', x.documents, x.id));
  }
  for (const x of review.reattributed_out || []) {
    box.appendChild(reviewEntry(`${x.records} records moved to “${nameOf(x.id, x.value)}”`, ruleText(x.rule), x.rule === 'reviewed_document_attribution' ? reasons[`attr:${r.id}:${x.id}`] : '', x.documents, x.id));
  }
  for (const x of review.kept_apart || []) {
    const now = x.canonical !== x.id ? ` (now part of ${nameOf(x.canonical, x.canonicalValue)})` : '';
    const own = x.member !== r.id ? ` — our member ${nameOf(x.member, x.memberValue)}` : '';
    box.appendChild(reviewEntry(`Kept apart from ${nameOf(x.id, x.value)}${now}`, 'never-merge rule (config/identity_merges.json)' + own, reasons['apart:' + [String(x.member), String(x.id)].sort().join('|')], [], x.canonical));
  }
  return box;
}
// ---- Expand: pull a node's hidden partners (record.expand) and merged addresses into the view ----
const expansions = new Map();  // expanded node id -> {nodes, edges} it added
const expansionRequests = new Map();
function addNode(n) {
  data.nodes.push(n); nodeById.set(n.id, n); incident.set(n.id, []); PRESENT_TYPES.add(n.record.entity_type);
  n.homeX = n.x; n.homeY = n.y; n.expandedBy = new Set();
}
function addEdge(e) {
  data.edges.push(e); edgeById.set(e.id, e);
  incident.get(e.from)?.push(e); if (e.to !== e.from) incident.get(e.to)?.push(e);
}
const expandWidth = records => Math.min(8, 1 + 1.75 * Math.log10(Math.max(1, records)));
async function expandNode(id) {
  const n = nodeById.get(id);
  if (!n || expansions.has(id) || expansionRequests.has(id)) return;
  const request = {}; expansionRequests.set(id, request);
  let r;
  try { r = await fullRecord(n); }
  catch (error) { detailError(error); }
  if (expansionRequests.get(id) !== request) return;
  expansionRequests.delete(id);
  if (!r || nodeById.get(id) !== n || expansions.has(id)) return;
  const added = {nodes: [], edges: []}, newcomers = [];
  const stub = (nodeId, record) => {
    let other = nodeById.get(nodeId);
    if (!other) {
      other = {id: nodeId, label: record.name, x: n.x, y: n.y, record, color: typeColor(record), size: 7, count: 0};
      addNode(other); newcomers.push(other);
    }
    if (other.expandedBy) { other.expandedBy.add(id); added.nodes.push(nodeId); }
    return other;
  };
  for (const x of r.expand || []) {
    stub(x.id, {id: x.id, name: x.value || x.name, entity_type: x.entity_type, ...(x.label_quality ? {label_quality: x.label_quality} : {}), expanded_from: r.name,
                note: 'Pulled in by Expand: a communication partner outside the default view (complete header data).'});
    const e = {id: `expand:${id}:${x.id}:${x.relationship_type}`, from: x.outgoing ? id : x.id, to: x.outgoing ? x.id : id,
               color: '#64748b', width: expandWidth(x.records), arrow: true, title: `${relationshipLabel(x.relationship_type)} · ${x.records} records`,
               record: {id: `expand:${id}:${x.id}`, relationship_type: x.relationship_type, weight: x.records, date_first: x.date_first, date_last: x.date_last,
                        metadata: {record_count: x.records, meaning: 'Parsed sender to recipient header relation from the complete communication data (pulled in by Expand).'}}};
    if (!edgeById.has(e.id)) { addEdge(e); added.edges.push(e.id); }
  }
  // Satellites: the addresses and spellings merged into this node (records > 0), dashed.
  for (const m of (r.merged_identities || []).filter(m => m.records > 0).slice(0, 30)) {
    const kind = String(m.id).split('_')[0];
    stub('alias:' + m.id, {id: m.id, name: m.value || String(m.id), entity_type: ['email', 'phone'].includes(kind) ? 'contact' : 'participant', ...(['email', 'phone'].includes(kind) ? {subtype: kind} : {}),
                           alias_of: r.name, rule: m.rule, confidence: m.confidence, records: m.records, note: 'Merged into ' + r.name + ' (' + ruleText(m.rule) + ').'});
    const e = {id: `alias:${m.id}`, from: 'alias:' + m.id, to: id, color: '#94a3b8', width: 1, dashed: true, arrow: false, title: 'merged into · ' + ruleText(m.rule),
               record: {id: `alias:${m.id}`, relationship_type: 'merged_into', weight: m.records, metadata: {meaning: `Merged identity (${ruleText(m.rule)}, ${m.confidence}).`}}};
    if (!edgeById.has(e.id)) { addEdge(e); added.edges.push(e.id); }
  }
  const {f = 1, cx = 0, cy = 0} = state.compact || {};
  for (const p of ringPositions({x: n.x, y: n.y, radius: n.size || 7}, newcomers.map(m => ({id: m.id, radius: 7})), 18)) {
    const m = nodeById.get(p.id); m.x = p.x; m.y = p.y; m.homeX = cx + (p.x - cx) / f; m.homeY = cy + (p.y - cy) / f;
  }
  expansions.set(id, added);
  renderer.setGraph(data.nodes, data.edges);
  update();
  setStatus(`${newcomers.length} dots pulled in`);
}
function collapseNode(id) {
  expansionRequests.delete(id);
  const added = expansions.get(id);
  if (!added) return;
  expansions.delete(id);
  const dropEdges = new Set(added.edges);
  const dropNodes = new Set(added.nodes.filter(nid => { const m = nodeById.get(nid); m.expandedBy.delete(id); return !m.expandedBy.size; }));
  for (const e of data.edges) if (dropNodes.has(e.from) || dropNodes.has(e.to)) dropEdges.add(e.id);
  data.edges = data.edges.filter(e => !dropEdges.has(e.id)); data.nodes = data.nodes.filter(m => !dropNodes.has(m.id));
  for (const eid of dropEdges) edgeById.delete(eid);
  for (const nid of dropNodes) { nodeById.delete(nid); incident.delete(nid); }
  for (const [nid, list] of incident) incident.set(nid, list.filter(e => !dropEdges.has(e.id)));
  renderer.setGraph(data.nodes, data.edges);
  update();
}
function expandButton(n, r) {
  const partners = (r.expand || []).length, aliases = (r.merged_identities || []).filter(m => m.records > 0).length;
  if (!partners && !aliases && !expansions.has(n.id)) return element('div');
  const b = element('button', expansions.has(n.id) ? 'Collapse'
    : `Expand: ${partners} hidden partners${r.expand_total > partners ? ` (of ${r.expand_total})` : ''} · ${Math.min(aliases, 30)} merged addresses`);
  b.onclick = async () => { expansions.has(n.id) ? collapseNode(n.id) : await expandNode(n.id); const m = nodeById.get(n.id); if (m && state.detail?.node === n.id) showNode(m); };
  return b;
}
// ---- Export: a node, all its loaded links (shown or hidden by filters) and their documents, as Markdown ----
async function exportNode(n) {
  const r = await fullRecord(n), out = [`# ${r.name}`, '', `${r.entity_type} · exported ${new Date().toISOString().slice(0, 10)} from the Epstein-files relationship explorer.`,
    'Links are reading leads, not evidence of wrongdoing; check each document.', ''];
  const doc = d => {
    const url = d.pdf_url || documentUrl(data.urls, d.efta_number), page = Number.isInteger(d.page) ? d.page + 1 : null;
    const info = d.document_info || data.docinfo?.[d.efta_number];
    return `- ${d.efta_number}${page ? ' p.' + page : ''}${typeof url === 'string' && url.startsWith('https://') ? ' <' + pageUrl(url, d.page) + '>' : ''}${d.how ? ' — ' + d.how : ''}${info ? ' (' + info + ')' : ''}`;
  };
  const records = list => (list || []).map(x => `- ${x.kind}: ${x.ref}${typeof x.url === 'string' && x.url.startsWith('https://') ? ' <' + x.url + '>' : ''}`);
  if (r.note) out.push(r.note, '');
  if ((r.legal_status_flags || []).length) out.push(`Reported status (unverified): ${r.legal_status_flags.join(', ')}`, '');
  const names = (r.original_names || []).filter(x => x !== r.name);
  if (names.length) out.push(`Also written as: ${names.join(' · ')}`, '');
  const merged = r.merged_identities || [];
  if (merged.length) {
    out.push(`## Identities merged into this node (${r.merged_identity_count || merged.length})`, '');
    for (const m of merged) out.push(`- ${nameOf(m.id, m.value)} — ${m.records ?? '?'} records — ${ruleText(m.rule)} (${m.confidence})`);
    out.push('');
  }
  const d = r.documents || {};
  out.push(`## Documents about this node (${(d.n || 0).toLocaleString()} via its links)`, '', ...records(d.records), ...(d.docs || []).map(doc), ...(r.source_documents || []).map(doc), '');
  const links = (incident.get(n.id) || []).filter(e => e.record.relationship_type !== 'merged_into');
  out.push(`## Connections (${links.length})`, '');
  for (const e of [...links].sort((a, b) => (b.record.weight || 0) - (a.record.weight || 0))) {
    let er = await fullRecord(e);
    if (er.metadata && Number.isInteger(er.metadata.meaning_ref)) er = {...er, metadata: {...er.metadata, meaning: data.meanings[er.metadata.meaning_ref]}};
    const other = nodeById.get(e.from === n.id ? e.to : e.from)?.record, ed = er.documents || {};
    const dates = er.date_first || er.date_last ? `, ${String(er.date_first || '?').slice(0, 10)} to ${String(er.date_last || '?').slice(0, 10)}` : '';
    out.push(`### ${e.from === n.id ? '→' : '←'} ${other?.name} (${other?.entity_type}) — ${relationshipLabel(er.relationship_type)} (${er.weight ?? '?'} records${dates}; ${ed.n || 0} documents${ed.docs && ed.n > ed.docs.length ? ', ' + ed.docs.length + ' listed' : ''})`, '');
    if (er.metadata?.meaning) out.push(er.metadata.meaning, '');
    out.push(...(ed.docs && ed.docs.length ? ed.docs : er.metadata?.sample_documents || []).map(doc), ...records(ed.records), '');
  }
  const hidden = (r.expand || []).filter(x => !nodeById.has(x.id));
  if (hidden.length) {
    out.push(`## Communication partners outside the view (${hidden.length}${r.expand_total > hidden.length ? ' of ' + r.expand_total : ''})`, '');
    for (const x of hidden) out.push(`- ${x.outgoing ? '→' : '←'} ${x.value || x.name} — ${relationshipLabel(x.relationship_type)} — ${x.records} records`);
    out.push('');
  }
  out.push('Full document lists (up to 5,000 per list, 500 per link): unlock the viewer, open "All documents for this node" and use its Export buttons.');
  const a = element('a'); a.href = URL.createObjectURL(new Blob([out.join('\n') + '\n'], {type: 'text/markdown'}));
  a.download = String(r.name).replace(/[^\w.@-]+/g, '_').slice(0, 80) + '.md'; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
function showNode(n) {
  const request = state.detail = {node: n.id}; $('details').hidden = false;
  rawRecord = null;
  $('detail-title').textContent = n.record.name;
  $('detail-sub').textContent = 'Loading details…';
  $('neighbors').replaceChildren(); $('docs').replaceChildren(); $('raw').textContent = '';
  const render = r => {
    if (state.detail !== request || nodeById.get(n.id) !== n) return;
    $('detail-title').textContent = r.name;
    const flags = (r.legal_status_flags || []).length ? ` · reported: ${r.legal_status_flags.join(', ')} (unverified)` : '';
    $('detail-sub').textContent = `${typeName(r)} · ${n.count} connections in view${flags}`;
    const panel = $('neighbors'); panel.replaceChildren();
    if (r.note) panel.appendChild(element('p', r.note));
    if ((r.websites || []).length) panel.appendChild(element('p', `Website: ${r.websites.join(', ')} (domain matches the name; ownership not verified)`));
    panel.appendChild(expandButton(n, r));
    const exporter = element('button', 'Export node (Markdown)'); exporter.onclick = () => exportNode(n).catch(detailError); panel.appendChild(exporter);
    const rows = neighborRows(n.id, incident, state.visibleLinks, id => nodeById.get(id).record.name);
    panel.appendChild(element('h2', `${rows.length} connections`));
    for (const [other, row] of rows.slice(0, 200)) {
      const b = element('button', `${nodeById.get(other).record.name} — ${[...row.types].join(', ')} (${row.records})`);
      b.onclick = () => focusNode(other);
      panel.appendChild(b);
    }
    if (rows.length > 200) panel.appendChild(element('div', `…and ${rows.length - 200} more`));
    const docs = $('docs'), d = r.documents || {};
    docs.replaceChildren(pageLink(r.document_page, '', `All documents for this node${d.n ? ` (${d.n.toLocaleString()} via its links)` : ''}, grouped by link`));
    if (!r.document_page && d.n) docs.appendChild(element('div', `${d.n.toLocaleString()} documents via its links: select a line to see them, or unlock for the full list.`)).className = 'muted';
    docs.appendChild(recordLinks(d.records, d.records_n, 'Pages'));
    docs.appendChild(documentLinks(d.docs, 'Documents naming this node'));
    docs.appendChild(documentLinks(r.source_documents, 'Source documents'));
    // DOJ subpoenas naming this node as target (build_doc_info.py); the subpoena document itself is linked.
    if (Array.isArray(r.subpoenas)) docs.appendChild(documentLinks(r.subpoenas.map(x => ({...x,
      how: [x.date, x.category, x.target].filter(Boolean).join(' · ')})), 'DOJ subpoenas'));
    docs.appendChild(mergePanel(r));
    setRaw({connections_in_view: n.count, ...r});
  };
  if (n.detail === undefined) return render(revealRecord(n.record));
  return fullRecord(n).then(render).catch(error => { if (state.detail === request) detailError(error); });
}
function showEdge(e) {
  const request = state.detail = {edge: e.id}; $('details').hidden = false;
  rawRecord = null;
  $('detail-title').textContent = `${nodeById.get(e.from)?.record.name} → ${nodeById.get(e.to)?.record.name}`;
  $('detail-sub').textContent = 'Loading details…';
  $('neighbors').replaceChildren(); $('docs').replaceChildren(); $('raw').textContent = '';
  const render = record => {
    if (state.detail !== request || edgeById.get(e.id) !== e) return;
    let r = record;
    if (r.metadata && Number.isInteger(r.metadata.meaning_ref)) r = {...r, metadata: {...r.metadata, meaning: data.meanings[r.metadata.meaning_ref]}};
    const a = nodeById.get(e.from), b = nodeById.get(e.to);
    $('detail-title').textContent = `${a?.record.name} → ${b?.record.name}`;
    $('detail-sub').textContent = e.title || edgeTitle(r);
    $('neighbors').replaceChildren();
    const docs = $('docs'), d = r.documents || {};
    const anchor = 'e-' + String(r.id).replace(/[^A-Za-z0-9_-]/g, '_');
    // Viewer fallback links are added after the private per-node pages are built.
    // Their evidence is embedded here; those pages have no matching link anchor.
    docs.replaceChildren(...(r.relationship_type === 'same_document' ? []
      : [pageLink(a?.record.document_page, anchor, `All ${(d.n || 0).toLocaleString()} documents for this link`)]));
    if (d.docs && d.docs.length) docs.appendChild(documentLinks(d.docs, d.n > d.docs.length ? `Documents (${d.docs.length} of ${d.n.toLocaleString()})` : `Documents (${d.n})`));
    else docs.appendChild(documentLinks(r.metadata?.sample_documents, 'Sample documents'));
    docs.appendChild(recordLinks(d.records, d.records_n, 'Records'));
    setRaw(r);
  };
  if (e.detail === undefined) return render(e.record);
  return fullRecord(e).then(render).catch(error => { if (state.detail === request) detailError(error); });
}
let rawRecord = null;
function setRaw(record) {
  rawRecord = record;
  $('raw').textContent = $('raw-box').open ? JSON.stringify(record, null, 2) : '';
}
$('raw-box').ontoggle = () => { $('raw').textContent = $('raw-box').open && rawRecord ? JSON.stringify(rawRecord, null, 2) : ''; };
function clearDetails(message = 'Click a dot or line.') {
  rawRecord = null;
  state.detail = null; $('details').hidden = true;
  $('detail-title').textContent = 'Nothing selected';
  $('detail-sub').textContent = message;
  $('neighbors').replaceChildren(); $('docs').replaceChildren(); $('raw').textContent = '';
}
function typeName(r) { const sub = [r.exact_subtype, r.subtype].find(x => x && x !== "ner"), t = sub ? `${r.entity_type} · ${String(sub).replace(/_/g, ' ')}` : r.entity_type; return r.dual_status ? `${t}, also a survivor` : t; }
function tooltip(kind, item) {
  if (kind === 'edge') return item.title || edgeTitle(item.record);
  const flags = (item.record.legal_status_flags || []).length ? ' · status flags reported, unverified' : '';
  return `${item.record.name} (${typeName(item.record)}) · ${item.count} connections${flags}`;
}

// ---- actions -------------------------------------------------------------------
function focusNode(id) {
  const n = nodeById.get(id);
  if (!n || n.hidden) { setStatus('That entity is hidden by the current filters.'); return; }
  select([id]);
  renderer.focus(id, 1.2);
  showNode(n);
}
function find() {
  const n = matchNode($('search').value, data.nodes, id => !nodeById.get(id).hidden, id => state.eligible.has(id));
  if (!n) { setStatus('No unique match. Pick a name with its type and ID from the suggestions.'); return; }
  if (n.hidden) {
    if (!state.eligible.has(n.id)) { setStatus(`${n.record.name} is hidden by a Hide option.`); return; }
    state.hiddenTypes.delete(n.record.entity_type); state.hiddenTypes.delete(`${n.record.entity_type}:${n.record.subtype}`);  // shown again by its type buttons
    refreshTypeButtons(); update();
  }
  focusNode(n.id);
}
function findPath() {
  const visible = id => !nodeById.get(id).hidden, eligible = id => state.eligible.has(id);
  const a = matchNode($('path-from').value, data.nodes, visible, eligible), b = matchNode($('path-to').value, data.nodes, visible, eligible);
  if (!a || !b) { setStatus('Enter two unambiguous names (pick from the suggestions).'); return; }
  if (a.hidden || b.hidden) { setStatus('One endpoint is hidden by the current filters.'); return; }
  const path = shortestPath(a.id, b.id, incident, state.visibleLinks);
  if (!path) { setStatus(`No path between ${a.record.name} and ${b.record.name} over visible links.`); return; }
  const ids = [a.id];
  for (const e of path) ids.push(ids[ids.length - 1] === e.from ? e.to : e.from);
  select(ids, path.map(e => e.id));
  renderer.fit(ids, true);
  state.detail = {path: path.map(e => e.id), nodes: ids}; $('details').hidden = false;
  rawRecord = null;
  $('detail-title').textContent = `${path.length}-step path`;
  $('detail-sub').textContent = 'Over visible links only. A path is not evidence of contact.';
  const panel = $('neighbors'); panel.replaceChildren(); $('docs').replaceChildren(); $('raw').textContent = '';
  ids.forEach((id, i) => {
    const b = element('button', nodeById.get(id).record.name); b.onclick = () => focusNode(id); panel.appendChild(b);
    if (i < path.length) {
      const e = path[i], eb = element('button', `   ↳ ${relationshipLabel(e.record.relationship_type)} (${e.record.weight})`);
      eb.onclick = () => showEdge(e); panel.appendChild(eb);
    }
  });
}
// Gather a node's drawn neighbours in rings around it and freeze the layout.
function gatherNeighbors(id) {
  const center = nodeById.get(id);
  if (!center || center.hidden) return [];
  const ids = new Set();
  for (const e of incident.get(id) || []) {
    const other = e.from === id ? e.to : e.from, n = nodeById.get(other);
    if (!e.hidden && other !== id && n && !n.hidden) ids.add(other);
  }
  if (!ids.size || !Number.isFinite(center.x) || !Number.isFinite(center.y)) return [];
  setLayout(false, false);
  const placed = ringPositions({x: center.x, y: center.y, radius: center.size}, [...ids].map(i => ({id: i, radius: nodeById.get(i).size})));
  for (const p of placed) { const n = nodeById.get(p.id); n.x = p.x; n.y = p.y; }
  renderer.refresh();
  setStatus(`Gathered ${placed.length} neighbours; layout frozen.`);
  return placed.map(p => p.id);
}
function setLayout(on, settle = true) {
  if (on) { stopSpreading(); renderer.startLayout(state.spacing); } else renderer.stopLayout();
  $('layout-toggle').setAttribute('aria-pressed', String(on));
  $('layout-toggle').textContent = on ? 'Freeze layout' : 'Animate layout';
  if (!on && settle) spreadDots();
  setStatus();
}

// ---- wiring --------------------------------------------------------------------
const nodeChoices = new Set();
function fillNames() {
  nodeChoices.clear(); $('names').replaceChildren();
  for (const n of data.nodes) { const choice = entityChoice(n); nodeChoices.add(choice); const o = element('option'); o.value = choice; $('names').appendChild(o); }
}
fillNames();
for (const kind of [...new Set(data.edges.map(e => e.record.relationship_type))].sort()) {
  const o = element('option', relationshipLabel(kind)); o.value = kind; $('relationship').appendChild(o);
}
const typeButtons = new Map();
const allButton = element('button', 'All'); allButton.className = 'all';
allButton.onclick = () => {
  const allShown = !state.hiddenTypes.size;
  state.hiddenTypes = allShown ? new Set(Object.keys(COLORS)) : new Set();
  refreshTypeButtons(); update();
};
$('types').appendChild(allButton);
for (const [type, color] of Object.entries(COLORS)) {
  const label = type === 'participant' ? 'Participants' : type.replace(/_/g, ' ');
  const b = element('button', label); b.style.color = color;
  b.onclick = () => { state.hiddenTypes.has(type) ? state.hiddenTypes.delete(type) : state.hiddenTypes.add(type); refreshTypeButtons(); update(); };
  b.ondblclick = () => soloType(type);  // the two clicks before it cancel out
  typeButtons.set(type, b); $('types').appendChild(b);
  const o = element('option', `Draw links touching ${label}`); o.value = 'type:' + type; $('link-mode').appendChild(o);
}
// Show only one type (all its subtypes), or only one subtype of it.
function soloType(type, sub) {
  state.hiddenTypes = new Set([...Object.keys(COLORS).filter(t => t !== type), ...(sub ? (SUBTYPES[type] || []).filter(([s]) => s !== sub).map(([s]) => `${type}:${s}`) : [])]);
  refreshTypeButtons(); update();
}
let lastChip = {};
// Subtype chips of every shown type with more than one subtype.
function subtypeChips() {
  const box = $('subtypes'); box.replaceChildren();
  for (const [type, subs] of Object.entries(SUBTYPES)) {
    if (subs.length < 2 || CHIPLESS.has(type) || state.hiddenTypes.has(type)) continue;
    const row = element('div'); row.className = 'subrow';
    row.appendChild(element('span', type.replace(/_/g, ' '))).className = 'muted';
    for (const [sub, count] of subs) {
      const key = `${type}:${sub}`, b = element('button', `${sub} ${count.toLocaleString()}`);
      b.style.color = SHADES[key] || COLORS[type]; b.setAttribute('aria-pressed', String(!state.hiddenTypes.has(key)));
      // Chips are rebuilt on every click, so a double click is detected by timing, not dblclick.
      b.onclick = () => {
        if (lastChip.key === key && performance.now() - lastChip.at < 400) { lastChip = {}; return soloType(type, sub); }
        lastChip = {key, at: performance.now()};
        state.hiddenTypes.has(key) ? state.hiddenTypes.delete(key) : state.hiddenTypes.add(key); refreshTypeButtons(); update();
      };
      row.appendChild(b);
    }
    box.appendChild(row);
  }
}
function refreshTypeButtons() {
  // Types whose every node is hidden by a fixed filter get no button.
  const shown = new Set(filterLayers(data.nodes, [], '', {artifacts: true}).nodes.map(n => n.record.entity_type));
  for (const [type, b] of typeButtons) b.hidden = !shown.has(type);
  allButton.setAttribute('aria-pressed', String(!state.hiddenTypes.size));
  allButton.textContent = state.hiddenTypes.size ? 'All' : 'None';
  for (const [type, b] of typeButtons) b.setAttribute('aria-pressed', String(!state.hiddenTypes.has(type)));
  subtypeChips();
}
$('show-incidental').onchange = () => { state.hide.incidental = !$('show-incidental').checked; update(); };
for (const box of document.querySelectorAll('[data-hide]')) {
  state.hide[box.dataset.hide] = box.checked;
  box.onchange = () => { state.hide[box.dataset.hide] = box.checked; update(); };
}
for (const box of document.querySelectorAll('[data-status]')) {
  box.onchange = () => { box.checked ? state.hiddenStatuses.add(box.dataset.status) : state.hiddenStatuses.delete(box.dataset.status); update(); };
}
$('co-mentions').onchange = () => {
  state.hide.coMentions = $('co-mentions').value === 'hidden';
  state.hide.linkedCoMentions = $('co-mentions').value === 'new';
  update();
};
$('reset').onclick = () => {
  state.hiddenTypes = defaultHidden(); state.hiddenStatuses.clear();
  $('show-incidental').checked = false;
  state.hide = {artifacts: true, linkedCoMentions: true, incidental: true};
  state.relationship = ''; state.linkMode = ''; state.hover = null; state.spacing = 240;
  $('relationship').value = ''; $('link-mode').value = ''; $('co-mentions').value = 'new'; $('spacing').value = '240';
  for (const box of document.querySelectorAll('[data-hide]')) box.checked = !!state.hide[box.dataset.hide];
  for (const box of document.querySelectorAll('[data-status]')) box.checked = false;
  setLayout(false, false); refreshTypeButtons(); update();
};
$('relationship').onchange = () => { state.relationship = $('relationship').value; update(); };
$('link-mode').onchange = () => { state.linkMode = $('link-mode').value; applyLinks(); };
$('search').onkeydown = ev => { if (ev.key === 'Enter') find(); };
$('search').oninput = () => { if (nodeChoices.has($('search').value)) find(); };  // picked a suggestion
$('find-path').onclick = findPath;
$('clear').onclick = () => { select(); clearDetails(); };
$('fit').onclick = () => renderer.fit(null, true);
$('detail-close').onclick = () => { select(); clearDetails(); };
// The filters panel can be hidden to give the graph the full width; remembered per browser.
function setPanel(open) {
  document.querySelector('main').classList.toggle('panel-hidden', !open);
  $('panel-toggle').setAttribute('aria-pressed', String(open));
  try { localStorage.setItem('viewer.panel', open ? 'open' : 'hidden'); } catch {}
}
$('panel-toggle').onclick = () => setPanel(document.querySelector('main').classList.contains('panel-hidden'));
try { setPanel(localStorage.getItem('viewer.panel') !== 'hidden'); } catch {}
document.addEventListener('keydown', ev => {
  const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(ev.target.tagName);
  if (ev.key === 'Escape') {
    if (typing) ev.target.blur();
    else if (!$('details').hidden) { select(); clearDetails(); }
    return;
  }
  if (typing || ev.ctrlKey || ev.metaKey || ev.altKey) return;
  if (ev.key === '/') { ev.preventDefault(); $('search').focus(); $('search').select(); }
  else if (ev.key === '[') setPanel(document.querySelector('main').classList.contains('panel-hidden'));
  else if (ev.key === 'f' || ev.key === 'F') renderer.fit(null, true);
});
$('layout-toggle').onclick = () => setLayout(!renderer.layoutRunning);
$('spacing').oninput = () => { state.spacing = Number($('spacing').value) || 240; if (renderer.layoutRunning) renderer.startLayout(state.spacing); };

renderer.on('click', ({node, edge}) => {
  if (node) { select([node.id]); showNode(node); }
  else if (edge) { select([], [edge.id]); showEdge(edge); }
  else { select(); clearDetails(); }
});
renderer.on('hover', ({node}) => {
  state.hover = node ? node.id : null;
  if (state.linkMode === 'focus') applyLinks();
});
// Double-click a dot: gather its neighbours and draw only hovered / selected links; on empty space: draw all links.
function setLinkMode(mode) {
  if (state.linkMode === mode) return;
  state.linkMode = $('link-mode').value = mode;
  applyLinks();
}
renderer.on('doubleclick', ({node}) => {
  if (!node) return setLinkMode('');
  const placed = gatherNeighbors(node.id);  // gathers by the links drawn before the switch
  setLinkMode('focus');
  if (placed.length) setStatus(`Gathered ${placed.length} neighbours; layout frozen.`);
});

// ---- encrypted real names (scripts/viewer_secret.py) --------------------------
async function decryptSecret(password) {
  const bytes = b64 => Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const base = await crypto.subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey({name: 'PBKDF2', salt: bytes(SECRET.salt), iterations: SECRET.iterations, hash: 'SHA-256'},
    base, {name: 'AES-GCM', length: 256}, false, ['decrypt']);
  // AES-GCM authenticates: a wrong password throws instead of returning garbage.
  const plaintext = await crypto.subtle.decrypt({name: 'AES-GCM', iv: bytes(SECRET.iv)}, key, bytes(SECRET.data));
  const text = SECRET.compression === 'gzip'
    ? await new Response(new Blob([plaintext]).stream().pipeThrough(new DecompressionStream('gzip'))).text()
    : new TextDecoder().decode(plaintext);
  return JSON.parse(text);
}
// reveal: {id: {value, possible_duplicates}}; a node already showing its real value keeps its pseudonym.
function applyReveal(reveal, pages = {}) {
  Object.assign(activeReveal, reveal); Object.assign(activePages, pages);
  for (const n of data.nodes) {
    const r = n.record, real = activeReveal[r.id];
    if (real) {
      const suffix = n.label.startsWith(r.name) ? n.label.slice(r.name.length) : '';
      if (r.pseudonym === undefined) r.pseudonym = r.name;
      r.name = real.value; n.label = real.value + suffix;
    }
    if (real && real.possible_duplicates) r.possible_duplicates = real.possible_duplicates;
    if (n.detail === undefined) revealRecord(r);
    if (activePages[String(r.id)]) r.document_page = activePages[String(r.id)];
  }
}
function applySecret({reveal, pages, reasons: why}) {
  reasons = why || {};
  applyReveal(reveal, pages);
  fillNames();
  renderer.refresh();
  const selected = [...state.selectedNodes];
  if (selected.length === 1) showNode(nodeById.get(selected[0]));
  $('unlock').hidden = true; $('private-note').hidden = false;
  unlocked = true; state.hiddenTypes.delete('participant'); refreshTypeButtons(); update();  // names readable now
}
$('privacy-open').onclick = () => $('privacy').showModal();
$('unlock').hidden = !SECRET;
$('unlock').onsubmit = async ev => {
  ev.preventDefault();
  const box = $('password');
  setStatus('Decrypting…');
  try { applySecret(await decryptSecret(box.value)); setStatus('Real names shown (this page only; reload to hide).'); }
  catch { setStatus('Wrong password.'); }
  box.value = '';
};

refreshTypeButtons();
update();
renderer.fit();

