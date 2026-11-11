"""Document fallback ranking, identity safety, indexed evidence and viewer filters."""
import base64
import contextlib
import gzip
import io
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import viewer_documents as vd


def node(identity, kind='person', **kw):
    return {'id': identity, 'name': str(identity), 'entity_type': kind, **kw}


def edge(a, b, kind='sent_to', **kw):
    return {'id': f'{a}-{b}', 'source_entity_id': a, 'target_entity_id': b, 'relationship_type': kind, **kw}


class DocumentFallbackTests(unittest.TestCase):
    def test_hidden_endpoints_self_loops_artifacts_and_unlock(self):
        nodes = [node(1), node(2, 'participant'), node(3, 'place', subtype='address'),
                 node(4, suspected_parse_artifact=True), node(5, label_quality='not_a_name'),
                 node(6, 'participant', dual_status=True), node(7, 'place', subtype='airport')]
        edges = [edge(1, 1), edge(1, 2), edge(1, 3), edge(1, 4), edge(1, 5), edge(6, 7)]
        self.assertEqual(vd.isolates(nodes, edges), {1, 6})
        self.assertEqual(vd.isolates(nodes, edges, 'unlocked'), {6})
        self.assertEqual(vd.isolates(nodes, edges, 'all_types'), set())
        self.assertIn(1, vd.targets(nodes, edges))
        self.assertNotIn(4, vd.targets(nodes, edges))
        self.assertEqual(vd.isolates([node(1), node(2)], [edge(1, 2, 'co_mentioned_in_documents', metadata={'new_link': False})]), {1, 2})

    def test_own_website_is_not_a_document_partner(self):
        nodes = [node('o', 'organization'), node('w', 'website'), node('p')]
        edges = [edge('w', 'o', 'website_of')]
        self.assertEqual(vd.targets(nodes, edges), {'o', 'p'})
        self.assertEqual(vd.targets(nodes, edges + [edge('o', 'p')]), set())

    def test_top_three_shared_documents_visibility_preference_and_dedup(self):
        nodes = [node(i) for i in range(1, 6)] + [node(6, 'participant'), node(7, label_quality='not_a_name')]
        docs = {1: {'a', 'b', 'c', 'd'}, 2: {'a'}, 3: {'a', 'b'}, 4: {'a', 'b', 'c'},
                5: {'c'}, 6: {'a', 'b', 'c', 'd'}, 7: {'a', 'b', 'c', 'd'}}
        edges, added = vd.add_links(nodes, [], docs, {1, 4})
        first = [r for r in added if r['source_entity_id'] == 1]
        self.assertEqual([r['target_entity_id'] for r in first], [4, 3, 2])
        self.assertEqual(first[0]['weight'], 3)
        self.assertEqual([d['efta_number'] for d in first[0]['documents']['docs']], ['a', 'b', 'c'])
        self.assertEqual(len({r['id'] for r in added}), len(added))
        self.assertEqual(vd.add_links(nodes, edges, docs, {1, 4})[1], [])
        self.assertTrue(all(r['source_entity_id'] != r['target_entity_id'] for r in edges))
        # No document overlap can never become an invented edge.
        self.assertEqual(vd.add_links([node(1), node(2)], [], {1: {'x'}, 2: {'y'}}, {1})[1], [])

    def test_unique_surname_requires_evidence_and_preserves_provenance(self):
        nodes = [node(1, name='Smith'), node(2, name='Ada Smith', original_names=['Ada Smith']),
                 node(3, name='Jones'), node(4, name='Alice Jones'), node(5, name='Bob Jones'),
                 node(6, name='Brown'), node(7, name='Ann Brown'), node(8, name='Andrew'), node(9, name='Prince Andrew'),
                 node(10, name='Veylor ?'), node(11, name='Nerissa Veylor')]
        docs = {i: {'a'} for i in range(1, 12)}
        docs[6] = {'b'}
        nodes, edges, merged = vd.merge_surnames(nodes, [edge(1, 3), edge(1, 2), edge(6, 7)], docs)
        self.assertEqual(merged, {1: 2})
        self.assertEqual(edges[0]['source_entity_id'], 2)
        self.assertEqual(len(edges), 2)  # absorbed alias link removed
        canonical = next(e for e in nodes if e['id'] == 2)
        self.assertIn('Smith', canonical['original_names'])
        self.assertEqual(canonical['merged_identities'][-1]['source_documents'], [{'efta_number': 'a'}])

    def test_full_indexes_and_canonical_header_attribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with sqlite3.connect(out / 'documents.db') as db:
                db.execute('CREATE TABLE node_documents(node_id, efta)')
                db.executemany('INSERT INTO node_documents VALUES (?,?)', [('1', 'a'), ('1', 'a'), ('2', 'b')])
            with sqlite3.connect(out / 'comention_graph.db') as db:
                db.execute('CREATE TABLE mentions(entity_id, efta_number)')
                db.execute('INSERT INTO mentions VALUES (1,"c")')
            with sqlite3.connect(out / 'communication_graph.db') as db:
                db.execute('CREATE TABLE evidence(source_id, target_id, canonical_source, canonical_target, efta_number, source_table)')
                db.executemany('INSERT INTO evidence VALUES (?,?,?,?,?,?)', [
                    ('kg:1', 'raw', 'kg:2', 'email_x', 'd', 'headers'), ('kg:1', 'raw', 'kg:2', 'email_x', 'd', 'headers'),
                    ('kg:1', 'raw', 'kg:2', 'email_x', None, 'jmail:123')])
            (out / 'victim_documents.json').write_text(json.dumps({'2': ['e']}))
            (out / 'vehicle_evidence.json').write_text(json.dumps([{'entity_id': 'boat', 'documents': ['f']}]))
            docs = vd.read_documents([node(1), node(2), node('email_x'), node('boat')], out)
        self.assertEqual(docs[1], {'a', 'c'})
        self.assertEqual(docs[2], {'b', 'd', 'e', 'jmail:123'})
        self.assertEqual(docs['email_x'], {'d', 'jmail:123'})
        self.assertEqual(docs['boat'], {'f'})

    def test_fts_only_missing_lists_with_private_lookup_and_limit(self):
        nodes = [node(1, name='Jane Doe #20'), node(2, name='Smith'), node('email_x', 'contact')]
        docs = {1: set(), 2: {'known'}, 'email_x': set()}
        with patch('corpus.search_pages', return_value=[('doc', 0)]) as search:
            vd.missing_documents(nodes, {1, 2, 'email_x'}, docs, {'email_x': 'contact@example.test'}, object())
        self.assertEqual(search.call_count, 2)
        self.assertEqual(search.call_args_list[0].args[1], '"Jane Doe 20"')
        self.assertEqual(search.call_args_list[1].args[1], '"contact example test"')
        self.assertEqual(search.call_args.args[2], vd.FTS_LIMIT * 4)

    def test_existing_private_pages_extract_only_own_document_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            data = {'title': 'private person', 'identities': [['private@example.test']], 'sections': [
                {'id': 'header', 'rows': [['a', None, 0]]}, {'id': 'full-text', 'rows': [['b', None, 0]]},
                {'id': 'e-via-link', 'rows': [['unrelated', None, 0]]}]}
            (path / 'node_1.html').write_text('<script>const D = ' + json.dumps(data) + ';\nsecret notes')
            docs = vd.page_documents([node(1)], {1: set()}, path)
        self.assertEqual(docs, {1: {'a', 'b'}})

    def test_non_pdf_source_records_keep_clickable_urls_and_counts(self):
        item = vd.evidence(['EFTA00000001', 'jmail:message'], limit=10)
        self.assertEqual(item['n'], 1)
        self.assertEqual(item['records_n'], 1)
        self.assertEqual(item['records'][0]['url'], 'https://jmail.world/thread/message')
        self.assertEqual(len(item['docs']), 1)

    def test_augmentation_writes_report_beside_requested_graph_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'output'
            out.mkdir()
            with sqlite3.connect(out / 'documents.db') as db:
                db.execute('CREATE TABLE node_documents(node_id, efta)')
                db.executemany('INSERT INTO node_documents VALUES (?,?)', [('1', 'EFTA00000001'), ('2', 'EFTA00000001')])
            with patch.object(vd.corpus, 'PATH', Path(tmp) / 'absent'), patch('corpus.document_urls', return_value={}), contextlib.redirect_stdout(io.StringIO()):
                nodes, edges = vd.augment([node(1, name='Private test label'), node(2)], [], out=out)
            report = (out / 'viewer_document_links.json').read_text()
        self.assertEqual(len(edges), 1)
        self.assertEqual(vd.isolates(nodes, edges), set())
        self.assertEqual(json.loads(report)['after']['default'], 0)
        self.assertNotIn('Private test label', report)

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_built_viewer_isolation_regression(self):
        """Use the embedded payload and actual JS filters, including hidden co-mentions."""
        path = vd.ROOT / 'output/graph.html'
        if not path.exists():
            self.skipTest('viewer has not been built')
        html = path.read_text()
        match = re.search(r'id="viewer-data">([^<]+)', html)
        if not match:
            self.skipTest('viewer has no lazy payload')
        data = json.loads(gzip.decompress(base64.b64decode(match[1])))
        logic = (vd.ROOT / 'viewer/graph_logic.js').read_text()
        program = logic + r'''
const fs = require('node:fs'), assert = require('node:assert/strict');
const data = JSON.parse(fs.readFileSync(0,'utf8'));
assert.equal(relationshipLabel('same_document'), 'in the same document');
assert.notEqual(EDGE_COLORS.same_document, undefined);
const eligible = filterLayers(data.nodes, data.edges, '', {artifacts:true,linkedCoMentions:true,incidental:true});
// Label review made the curated agency visible as an organization; its existing
// source-gap reason records that indexed/limited exact-phrase searches found no documents.
const exceptions = new Set([364,505,'email_1d4101ff1a1e3490770efd00','email_e576eb0214e8cd5084534c77','place_sanela-jenkins-apartment','place_mc2-model-agency']);
for (const unlocked of [false,true]) {
  const hidden = new Set(['address','airport','institution','city','other'].map(s=>'place:'+s));
  if (!unlocked) hidden.add('participant');
  const view = entityVisibility(eligible.nodes,eligible.edges,hidden,'');
  const nodes = eligible.nodes.filter(n=>view.visible.has(n.id));
  const counts = connectionCounts(nodes,eligible.edges.filter(e=>view.visibleEdges.has(e.id)));
  const isolated = nodes.filter(n=>!counts.get(n.id));
  // Known source gaps, plus at most a few nodes whose only document partners are hidden by
  // default (incidental organizations, unlinked full-text entities); each must carry its reason.
  const other = isolated.filter(n=>!exceptions.has(n.id));
  assert.ok(other.length <= 15 && other.every(n=>n.record.document_isolation_reason==='document partners are hidden by this view'),
            JSON.stringify({unlocked, isolated:isolated.map(n=>n.id)}));
  assert.ok(isolated.every(n=>n.record.document_isolation_reason), 'Unexplained isolated dot');
  console.log(JSON.stringify({unlocked,visible:nodes.length,isolated:isolated.map(n=>({id:n.id,reason:n.record.document_isolation_reason}))}));
}
'''
        run = subprocess.run(['node', '-e', program], input=json.dumps(data), text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == '__main__':
    unittest.main()
