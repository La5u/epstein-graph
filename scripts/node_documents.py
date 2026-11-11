"""PRIVATE, local-only: one page per viewer node listing the documents behind it.

    called by scripts/private_contacts.py (writes data/private/node_docs/*.html)

Each page lists the identities merged into the node, then two document tables
(each document once, with links to the official PDF and a readable copy):
  header records  documents whose parsed From/To/Cc held the node or a merged
                  identity, with which identities (real values) and how often,
                  so each merge can be checked on the scan.
  full text       documents whose text contains the node's name or a merged
                  spelling (phrase search of the corpus, scripts/corpus.py).
  via links        per graph link of the node: the documents and data records
                  behind it (output/documents.db, scripts/build_documents.py),
                  anchored #e-<link id> so the viewer can open one link's list.
                  Links without documents are listed too (type, records, dates).
"Export" buttons download everything on the page as Markdown (for reading or
handing to an assistant) or JSON: identities, own documents, every link with
its documents and records, each with its official URL.
Each table stops at LIMIT documents. Pages hold real contact values, so they
stay under data/private/ like the private viewer that links to them.
"""
import csv
from contextlib import closing
import gzip
import hashlib
import html
import json
import hmac
import os
import tempfile
import re
import sqlite3
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import corpus
from adapters import efta
from derived_files import metadata_matches, remember_metadata

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 5000
MAX_SPELLINGS = 40  # full-text searches per node (merged identities by record count)


def private_write(path, text):
    """Replace atomically; neither old output nor temporary output is world-readable."""
    path = Path(path)
    if path.exists():
        path.chmod(0o600)
        if path.read_bytes() == text.encode('utf-8'):
            return
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def cache_stamp(entities, identity_map, lookup, key):
    """Cover labels, merge rules, search/render logic, sources and SQLite WALs.

    Large databases use filesystem fingerprints rather than scanning the corpus.
    HMAC keeps private labels (including low-entropy ones) out of the stamp.
    """
    from adapters import efta
    digest = hmac.new(key, digestmod=hashlib.sha256)
    digest.update(json.dumps([entities, identity_map, lookup, LIMIT, MAX_SPELLINGS],
                             sort_keys=True).encode())
    for path in (Path(__file__), VIEWER_LOGIC, Path(corpus.__file__), Path(efta.__file__), efta.URLS, ROOT / 'output/graph_relationships.json',
                 ROOT / 'output/communication_graph.db', ROOT / 'output/documents.db', corpus.PATH, CONTACTS):
        digest.update(str(path).encode())
        if path.suffix == '.db':
            for part in (path, Path(str(path) + '-wal')):
                stat = part.stat() if part.exists() else None
                digest.update(repr((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
                                    stat.st_ctime_ns) if stat else None).encode())
        else:
            digest.update(path.read_bytes() if path.exists() else b'<missing>')
    return digest.hexdigest()


def page_name(identity):
    return re.sub(r'[^A-Za-z0-9_]', '_', f'node_{identity}') + '.html'


def header_documents(db, identity):
    """[(efta, records)] for one raw endpoint, most records first, without the records
    build_communications reattributed to another identity by document."""
    moves = db.execute("SELECT 1 FROM sqlite_master WHERE name='reattributions'").fetchone()
    found = defaultdict(int)
    for column, side in (('source_id', 'source'), ('target_id', 'target')):
        moved_out = ' AND rowid NOT IN (SELECT evidence_rowid FROM reattributions WHERE side=?)' if moves else ''
        for efta, n in db.execute(f'SELECT efta_number, count(*) FROM evidence WHERE {column}=? AND efta_number IS NOT NULL{moved_out} GROUP BY 1',
                                  (identity, side) if moves else (identity,)):
            found[efta] += n
    return sorted(found.items(), key=lambda item: (-item[1], item[0]))


def moved_in_documents(db, identity):
    """[(efta, records)] reattributed to identity by document (first-name owners, reviewed attributions)."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='reattributions'").fetchone():
        return []
    return db.execute('SELECT e.efta_number, count(*) FROM reattributions r JOIN evidence e ON e.rowid=r.evidence_rowid '
                      'WHERE r.owner=? AND e.efta_number IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1', (identity,)).fetchall()


def text_documents(db, phrase):
    """[(efta, [pages])] whose text holds phrase (FTS5 phrase match, case-insensitive)."""
    tokens = re.findall(r'\w+', phrase)
    if not tokens or sum(len(t) for t in tokens) < 4:
        return []
    found = defaultdict(list)
    query = '"' + ' '.join(tokens) + '"'
    for efta, page in corpus.search_pages(db, query, LIMIT * 3):
        if len(found) >= LIMIT and efta not in found:
            break
        found[efta].append(page)
    return sorted(found.items())


LINK_LIMIT = 500  # documents listed per link


class LinkSections:
    """node -> [(anchor, heading, [(efta, page, how)], total, [(kind, ref, url)], link)], read from
    documents.db one node at a time (the whole table does not fit in memory; every lookup is indexed).
    link = {type, other, records, first, last} for graph links, None for the node's own sections."""

    def __init__(self, path, relationships, names):
        self.names, self.adjacent = names, defaultdict(list)
        self.path, self.connection, self.owner = path, None, None
        self.doc_counts = dict(self.db.execute('SELECT edge_id, count(*) FROM edge_documents GROUP BY edge_id')) if self.db else {}
        if self.connection:  # never carried across the fork into page workers
            self.connection.close()
            self.connection = None
        date = lambda value: None if value in (None, '', 'None') else str(value)[:10]
        for r in sorted(relationships, key=lambda r: (-self.doc_counts.get(str(r['id']), 0), -float(r.get('weight') or 0))):
            source, target = r['source_entity_id'], r['target_entity_id']
            for node, other in ((source, target), (target, source)):
                self.adjacent[str(node)].append((str(r['id']), r['relationship_type'], other, str(node) == str(source), r.get('weight'),
                                                 date(r.get('date_first')), date(r.get('date_last'))))

    @property
    def db(self):
        if self.owner != os.getpid() or self.connection is None:
            self.owner = os.getpid()
            self.connection = sqlite3.connect(f'file:{self.path}?mode=ro', uri=True) if Path(self.path).exists() else None
        return self.connection

    def get(self, node, default=None):
        return self[node] or default

    def __getitem__(self, node):
        if self.db is None:
            return []
        node, db, sections = str(node), self.db, []
        for kind, ref, url in db.execute('SELECT kind, ref, url FROM node_records WHERE node_id=? ORDER BY rowid', (node,)):
            sections.append(('profile', kind, [], 0, [(kind, ref, url)], None))
        own = db.execute('SELECT count(*) FROM node_documents WHERE node_id=? AND edge_id IS NULL', (node,)).fetchone()[0]
        if own:
            docs = db.execute('SELECT efta, page FROM node_documents WHERE node_id=? AND edge_id IS NULL ORDER BY efta LIMIT ?', (node, LINK_LIMIT))
            sections.append(('own', 'Documents naming this node', [(efta, page, 'names this node') for efta, page in docs], own, [], None))
        for edge, kind, other, outgoing, weight, first, last in self.adjacent.get(node, ()):
            docs = db.execute('SELECT efta, page, how FROM edge_documents WHERE edge_id=? ORDER BY efta LIMIT ?', (edge, LINK_LIMIT)).fetchall()
            records = db.execute('SELECT kind, ref, url FROM edge_records WHERE edge_id=? ORDER BY rowid LIMIT ?', (edge, LINK_LIMIT)).fetchall()
            name = self.names(other)
            sections.append(('e-' + re.sub(r'[^A-Za-z0-9_-]', '_', edge), f'{kind} · {name}', docs, self.doc_counts.get(edge, 0), records,
                             {'type': kind, 'other': name, 'outgoing': outgoing, 'records': weight, 'first': first, 'last': last}))
        return sections


def link_sections(path, relationships, names):
    return LinkSections(path, relationships, names)
    docs, records = defaultdict(list), defaultdict(list)
    doc_counts = defaultdict(int)
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db:
        for edge, efta, page, how in db.execute('SELECT edge_id, efta, page, how FROM edge_documents ORDER BY edge_id, efta'):
            doc_counts[edge] += 1
            if len(docs[edge]) < LINK_LIMIT:
                docs[edge].append((efta, page, how))
        for edge, kind, ref, url in db.execute('SELECT edge_id, kind, ref, url FROM edge_records ORDER BY rowid'):
            if len(records[edge]) < LINK_LIMIT:
                records[edge].append((kind, ref, url))
        for node, kind, ref, url in db.execute('SELECT node_id, kind, ref, url FROM node_records'):
            sections[node].append(('profile', kind, [], 0, [(kind, ref, url)], None))
        for node, efta, page in db.execute('SELECT node_id, efta, page FROM node_documents WHERE edge_id IS NULL ORDER BY efta'):
            if not sections[node] or sections[node][-1][0] != 'own':
                sections[node].append(('own', 'Documents naming this node', [], 0, [], None))
            sections[node][-1][2].append((efta, page, 'names this node'))
    for node in sections:
        sections[node] = [(a, h, d[:LINK_LIMIT], t or len(d), r, None) for a, h, d, t, r, _ in sections[node]]
    date = lambda value: None if value in (None, '', 'None') else str(value)[:10]
    for r in sorted(relationships, key=lambda r: (-doc_counts.get(str(r['id']), 0), -float(r.get('weight') or 0))):
        edge = str(r['id'])
        for node, other in ((r['source_entity_id'], r['target_entity_id']), (r['target_entity_id'], r['source_entity_id'])):
            heading = f"{r['relationship_type']} · {names(other)}"
            link = {'type': r['relationship_type'], 'other': names(other), 'outgoing': str(node) == str(r['source_entity_id']),
                    'records': r.get('weight'), 'first': date(r.get('date_first')), 'last': date(r.get('date_last'))}
            sections[str(node)].append(('e-' + re.sub(r'[^A-Za-z0-9_-]', '_', edge), heading,
                                        docs.get(edge, [])[:LINK_LIMIT], doc_counts.get(edge, 0), records.get(edge, [])[:LINK_LIMIT], link))
    return sections


PAGE_SCRIPT = r"""
const $ = id => document.getElementById(id), el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
const url = efta => efta in D.u ? D.t[D.u[efta]][0] + efta + D.t[D.u[efta]][1] : null;
PAGE_URL_JS
function cell(efta, page) {
  const td = el('td'), href = url(efta), label = efta + (page === null ? '' : ' p.' + (page + 1));
  if (href) { const a = el('a', label); a.href = pageUrl(href, page); a.target = '_blank'; a.rel = 'noopener'; td.append(a); } else td.append(label);
  if (/^EFTA\d{8}$/.test(efta) && href && href.startsWith('https://www.justice.gov/epstein/files/DataSet')) { const a = el('a', ' [ee]'); a.href = 'https://epsteinexposed.com/documents/efta-' + efta.slice(4); a.target = '_blank'; a.rel = 'noopener'; td.append(a); }
  return td;
}
function fill(table, section) {
  for (const [efta, page, note] of section.rows) { const tr = el('tr'); tr.append(cell(efta, page), el('td', D.notes[note], 'n')); table.append(tr); }
  for (const [kind, ref, href] of section.records) {
    const tr = el('tr'), td = el('td'), a = el('a', ref); a.href = href; a.target = '_blank'; a.rel = 'noopener'; td.append(a); tr.append(td, el('td', kind, 'n')); table.append(tr);
  }
}
document.title = D.title; $('title').textContent = D.title;
for (const [label, rule, n] of D.identities) { const tr = el('tr'); tr.append(el('td', label), el('td', rule, 'n'), el('td', n + ' documents')); $('identities').append(tr); }
for (const section of D.sections) {
  // Lists render when opened, so hub pages with thousands of rows stay light.
  const box = el('details'), summary = el('summary', section.heading + ' '), table = el('table');
  box.id = section.id; summary.append(el('span', `(${section.total} documents${section.total > section.rows.length ? ', showing ' + section.rows.length : ''}${section.records.length ? ', ' + section.records.length + ' records' : ''})`, 'n'));
  box.append(summary, table);
  box.ontoggle = () => { if (box.open && !table.rows.length) fill(table, section); };
  (section.link ? $('links') : $('own')).append(box);
}
// ---- Export: the whole page (all lists, not only opened ones) as one file ----
function rowsOf(section) {
  return section.rows.map(([efta, page, note]) => ({document: efta, page: page === null ? null : page + 1,
    url: url(efta) ? pageUrl(url(efta), page) : null, note: D.notes[note]}));
}
function exportData() {
  const own = D.sections.filter(s => !s.link), links = D.sections.filter(s => s.link);
  const block = s => ({heading: s.heading, documents_total: s.total, documents: rowsOf(s),
                       records: s.records.map(([kind, ref, href]) => ({kind, ref, url: href}))});
  return {node: D.title, exported: new Date().toISOString(),
          note: 'Private export of the Epstein-files relationship graph. Links are reading leads, not evidence of wrongdoing; check each document.',
          identities: D.identities.map(([label, rule, n]) => ({label, rule, header_documents: n})),
          documents_about_node: own.map(block),
          connections: links.map(s => ({...(typeof s.link === 'object' ? s.link : {}), ...block(s)}))};
}
function exportMarkdown() {
  const x = exportData(), out = [`# ${x.node}`, '', `Exported ${x.exported.slice(0, 10)}. ${x.note}`, '', '## Identities merged into this node', ''];
  for (const i of x.identities) out.push(`- ${i.label} — ${i.rule} — ${i.header_documents} header documents`);
  const docs = (s) => {
    for (const d of s.documents) out.push(`- ${d.document}${d.page ? ' p.' + d.page : ''}${d.url ? ' <' + d.url + '>' : ''}${d.note ? ' — ' + d.note : ''}`);
    for (const r of s.records) out.push(`- ${r.kind}: ${r.ref} <${r.url}>`);
  };
  out.push('', '## Documents about this node');
  for (const s of x.documents_about_node) { out.push('', `### ${s.heading} (${s.documents_total} documents${s.documents_total > s.documents.length ? ', ' + s.documents.length + ' listed' : ''})`, ''); docs(s); }
  out.push('', `## Connections (${x.connections.length})`);
  for (const c of x.connections) {
    const dates = c.first || c.last ? `, ${c.first || '?'} to ${c.last || '?'}` : '';
    out.push('', `### ${c.type ? (c.outgoing ? '→ ' : '← ') + c.other + ' — ' + c.type : c.heading} (${c.records ?? '?'} records${dates}; ${c.documents_total} documents${c.documents_total > c.documents.length ? ', ' + c.documents.length + ' listed' : ''})`, '');
    docs(c);
  }
  return out.join('\n') + '\n';
}
function download(text, type, extension) {
  const a = el('a'); a.href = URL.createObjectURL(new Blob([text], {type}));
  a.download = D.title.replace(/[^\w.@-]+/g, '_').slice(0, 80) + '.' + extension; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
$('export-md').onclick = () => download(exportMarkdown(), 'text/markdown', 'md');
$('export-json').onclick = () => download(JSON.stringify(exportData(), null, 1), 'application/json', 'json');
const target = location.hash && document.getElementById(location.hash.slice(1));
if (target) { target.open = true; target.scrollIntoView(); }
"""


VIEWER_LOGIC = ROOT / 'viewer/graph_logic.js'


@lru_cache(maxsize=1)
def page_url_js():
    """The viewer's pageUrl() (viewer/graph_logic.js), so node pages link pages the same way."""
    found = re.search(r'^function pageUrl\(.*?^}\n', VIEWER_LOGIC.read_text(), re.S | re.M)
    if not found:
        raise ValueError('pageUrl() not found in viewer/graph_logic.js')
    return found.group(0)


def render(title, identities, header, text, urls, links=()):
    """identities: [(label, rule, document count)]; header/text: {efta: [reasons]};
    links: [(anchor, heading, [(efta, page, how)], total, [(kind, ref, url)], link meta or None)].

    The page carries its rows as compact data (URL patterns stored once) and
    renders each list when it is opened: hub nodes list hundreds of thousands
    of documents."""
    templates, index, notes, note_index = [], {}, [], {}

    def note_id(text):
        if text not in note_index:
            note_index[text] = len(notes)
            notes.append(text)
        return note_index[text]

    def row(efta, page, text):
        url = urls.get(efta)
        if url and efta not in index:
            at = url.find(efta)
            pattern = [url[:at], url[at + len(efta):]] if at >= 0 else None
            if pattern:
                if pattern not in templates:
                    templates.append(pattern)
                index[efta] = templates.index(pattern)
        return [efta, page, note_id(text)]
    sections = []
    for heading, docs in (('Header records (sender/recipient)', header), ('Full-text mentions', text)):
        if docs:
            ordered = sorted(docs.items(), key=lambda item: (-len(item[1]), item[0]))
            sections.append({'id': heading.split()[0].lower(), 'heading': heading + ('' if len(docs) < LIMIT else f' (first {LIMIT})'),
                             'total': len(docs), 'link': False, 'records': [],
                             'rows': [row(e, None, '; '.join(r[:6]) + (' …' if len(r) > 6 else '')) for e, r in ordered]})
    for anchor, heading, docs, total, records, link in links:
        sections.append({'id': anchor, 'heading': heading, 'total': total, 'link': link or anchor.startswith('e-'),
                         'rows': [row(e, p, how) for e, p, how in docs],
                         'records': [list(r) for r in records if r[2].startswith('https://')]})
    data = json.dumps({'title': title, 'identities': identities, 'sections': sections, 't': templates, 'u': index, 'notes': notes},
                      ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    return (f'<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title>\n'
            '<style>body{font:13px system-ui;margin:2em}td{padding:1px 8px;vertical-align:top}.n{color:#666}'
            'summary{cursor:pointer;margin:4px 0}details table{margin:4px 0 10px 16px}</style>\n'
            '<h1 id="title"></h1><p><button id="export-md">Export (Markdown)</button> <button id="export-json">Export (JSON)</button></p>'
            '<p class="n">PRIVATE: real contact values. Open a list to show it. '
            '[ee] = epsteinexposed.com copy. Full-text fallbacks only show both names on one page.</p>\n'
            '<h2>Identities merged into this node</h2><table id="identities"></table>\n'
            '<h2>Documents about this node</h2><div id="own"></div>\n'
            '<h2>Via graph links</h2><div id="links"></div>\n'
            f'<script>const D = {data};{PAGE_SCRIPT.replace("PAGE_URL_JS", page_url_js())}</script>\n')


_db = _corpus = _contacts = None
CONTACTS = ROOT / 'output/contact_graph.db'


def _open(links=None, registry=None, out_dir=None):
    global _db, _corpus, _contacts, _links, _registry, _out_dir
    _links, _registry, _out_dir = links, registry, out_dir
    _db = sqlite3.connect(f'file:{ROOT / "output/communication_graph.db"}?mode=ro', uri=True)
    _corpus = corpus.connect()
    _contacts = sqlite3.connect(f'file:{CONTACTS}?mode=ro', uri=True) if CONTACTS.exists() else None


def written_documents(db, node, members):
    """[(efta, [pages], kind, labels)] where one of the node's email addresses / phone numbers
    (merged members, OCR variants) is written outside an email header line (contact_graph.db)."""
    if db is None:
        return []
    ids = {m for m in members if m.startswith(('email_', 'phone_'))}
    ids |= {i for i, in db.execute('SELECT id FROM nodes WHERE canonical = ?', (str(node),))}  # indexed, unlike an OR
    if 'owner' in {r[1] for r in db.execute('PRAGMA table_info(nodes)')}:  # values the node's name is written next to
        ids |= {i for i, in db.execute("SELECT id FROM nodes WHERE owner = ? AND kind != 'participant'", (str(node),))}
    found = defaultdict(lambda: [set(), '', set()])
    rows = (row for i in sorted(ids) for row in db.execute(
        'SELECT m.efta, m.page, n.kind, m.label FROM mentions m JOIN nodes n ON n.id = m.id WHERE m.id = ? AND m.header = 0', (i,)))
    for efta, page, kind, label in rows:
        item = found[efta]
        item[0].add(page)
        item[1] = kind
        if label and label not in ('email',):
            item[2].add(label)
    return sorted(((e, sorted(p), k, sorted(l)) for e, (p, k, l) in found.items()), key=lambda x: (-len(x[1]), x[0]))


def node_documents(job):
    """(node, title, [(identity, label, rule)], [spellings]) -> page data (runs in a worker)."""
    node, title, members, spellings = job
    identities, header = [], defaultdict(list)
    moved_in = ('', 'Records moved here by document', 'document attribution')
    for identity, label, rule in [*members, moved_in]:
        docs = moved_in_documents(_db, members[0][0]) if identity == '' else header_documents(_db, identity)
        if identity == '' and not docs:
            continue
        identities.append((label, rule, len(docs)))
        for efta, n in docs:
            if len(header) < LIMIT or efta in header:
                header[efta].append(f'{label} ×{n}')
    text = defaultdict(list)
    for spelling in spellings:
        for efta, ps in text_documents(_corpus, spelling):
            if len(text) < LIMIT or efta in text:
                text[efta].append(f'"{spelling}" p.' + ','.join(str(p + 1) for p in sorted(set(ps))[:5]))
    for efta, pages, kind, labels in written_documents(_contacts, node, [m for m, _, _ in members]):
        if len(text) < LIMIT or efta in text:
            what = 'address' if kind == 'email' else 'number'
            text[efta].append(f'{what} written{" (" + ", ".join(labels) + ")" if labels else ""} p.' + ','.join(str(p + 1) for p in pages[:5]))
    return node, title, identities, dict(header), dict(text)


def url_registry(urls=efta.URLS):
    """{document id: official url} of the whole URL list, loaded once for the many small page lookups.
    Lives here, not in corpus.py: every extraction step is traced to that file."""
    if not urls.exists():
        return {}
    with gzip.open(urls, 'rt', newline='') as f:
        return {row['document_id']: row['verification_url'] for row in csv.DictReader(f)
                if row['verification_url'].startswith(efta.TRUSTED)}


def document_urls(ids, registry):
    """URLs of one page's documents; those absent from the list come from the corpus table."""
    found = {i: registry[i] for i in ids if i in registry}
    found.update(efta.corpus_urls(set(ids) - set(found)))
    return found


def write_page(job):
    """Build and write one node's page in the worker; only its id goes back, so the parent's memory
    stays flat however far the workers run ahead of it. None when the node has nothing to list."""
    node, title, identities, header, text = node_documents(job)
    sections = _links.get(str(node), [])
    if not (header or text or sections):
        return None
    urls = document_urls({e for e in (*header, *text, *(d[0] for s in sections for d in s[2]))}, _registry)
    private_write(_out_dir / page_name(node), render(title, identities, header, text, urls, sections))
    return node


def write_pages(lookup, out_dir, workers=2):
    """Write one page per viewer node; returns {node id: relative page path}."""
    import gc
    from multiprocessing import get_context
    entities = json.loads((ROOT / 'output/graph_entities.json').read_text())
    relationships = json.loads((ROOT / 'output/graph_relationships.json').read_text())
    # The full entity layer has nearly a million nodes. Only viewer nodes
    # need private HTML pages; every node remains in the full graph/index.
    from visualize_graph import viewer_slice
    from contact_resolution import viewer_folds
    entities, relationships = viewer_folds(viewer_slice(entities, relationships), relationships, ROOT / 'output')
    identity_map = json.loads((ROOT / 'output/identity_map.json').read_text())
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    out_dir.chmod(0o700)
    # Tighten legacy outputs even when the cache is reused.
    for existing in out_dir.iterdir():
        if existing.is_file():
            existing.chmod(0o600)
    key_file = out_dir / '.cache-key'
    if not key_file.exists():
        private_write(key_file, os.urandom(32).hex())
    stamp = cache_stamp(entities, identity_map, lookup, bytes.fromhex(key_file.read_text()))
    stamp_file = out_dir / '.stamp.json'
    metadata = ROOT / 'output/.build/node_documents_stamp.json'
    cached = None
    try:
        stamp_text = stamp_file.read_text()
        cached = json.loads(stamp_text)
        pages = cached['pages']
        if ((cached.get('stamp') == stamp or metadata_matches(metadata, stamp, stamp_text)) and isinstance(pages, dict)
                and all(page == f'{out_dir.name}/{page_name(int(k) if k.isdigit() else k)}'
                        and (out_dir.parent / page).is_file() for k, page in pages.items())):
            return {(int(k) if k.isdigit() else k): v for k, v in pages.items()}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    members = defaultdict(list)
    # Apply the same contact ownership map as the viewer, including role
    # mailboxes that moved away from a header display-name person's cluster.
    node_ids = {str(e['id']): e['id'] for e in entities}
    membership = {member: (str(item['canonical']).removeprefix('kg:'), item['rule'])
                  for member,item in identity_map.items()}
    if CONTACTS.exists():
        with closing(sqlite3.connect(f'file:{CONTACTS}?mode=ro', uri=True)) as contacts:
            if contacts.execute("SELECT 1 FROM sqlite_master WHERE name='resolutions'").fetchone():
                membership.update((m,(c,r)) for m,c,r in contacts.execute('SELECT member,canonical,rule FROM resolutions WHERE member!=canonical'))
    for member,(canonical,rule) in membership.items():
        if canonical in node_ids:
            members[node_ids[canonical]].append((member,rule))
    value = lambda i: lookup.get(i, {}).get('value', i)
    jobs = []
    for entity in entities:
        node = entity['id']
        raw = f'kg:{node}' if isinstance(node, int) else node
        title = value(node) if not isinstance(node, int) else entity['name']
        own = [(raw, f'{title} (this node)', 'this node')]
        merged = sorted(((m, value(m), rule) for m, rule in members.get(node, [])), key=lambda m: m[1])
        spellings = [title] + [label for m, label, _ in merged if m in lookup]
        spellings = list(dict.fromkeys(s for s in spellings if s and not s.startswith(('email_', 'participant_', 'phone_'))))[:MAX_SPELLINGS]
        jobs.append((node, title, own + merged, spellings))
    jobs.sort(key=lambda job: -len(job[2]))  # biggest nodes first, so workers finish together
    titles = {job[0]: job[1] for job in jobs}
    links = link_sections(ROOT / 'output/documents.db', relationships,
                          lambda node: titles.get(node, str(node)))
    # Read-only tables are built once here and shared with the workers by fork (3.14 defaults to forkserver,
    # which would load them three times); freezing keeps the collector from copying those pages.
    registry = url_registry()
    del entities, identity_map, members
    gc.collect()
    gc.freeze()
    written = set()
    with get_context('fork').Pool(workers, initializer=_open, initargs=(links, registry, out_dir), maxtasksperchild=500) as pool:
        for node in pool.imap_unordered(write_page, jobs, chunksize=4):
            if node is not None:
                written.add(node)
    pages = {node: None for node in sorted(written, key=str)}
    result = {node: f'{out_dir.name}/{page_name(node)}' for node in pages}
    keep = {page_name(node) for node in pages}
    for old in out_dir.glob('node_*.html'):
        if old.name not in keep:
            old.unlink()
    if cached is not None and cached.get('pages') == {str(k): v for k, v in result.items()}:
        # Worker completion order is not output data. Retain the prior mapping
        # order (also serialized in viewer_secret.json) and private stamp bytes
        # when the same pages were regenerated. Validation metadata lives in
        # .build so a stamp change alone cannot change the derived artifacts.
        result = {(int(k) if k.isdigit() else k): v for k, v in cached['pages'].items()}
    else:
        stamp_text = json.dumps({'stamp': stamp, 'pages': {str(k): v for k, v in result.items()}})
        private_write(stamp_file, stamp_text)
    remember_metadata(metadata, stamp, stamp_text)
    return result
