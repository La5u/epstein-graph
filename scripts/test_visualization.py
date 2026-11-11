"""Exercise the actual viewer JS helpers with Node (no browser required)."""
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import os
import unittest
from unittest.mock import patch
from pathlib import Path

import visualize_graph


VIEWER = Path(__file__).resolve().parents[1] / 'viewer'


@unittest.skipUnless(shutil.which('node'), 'Node required for viewer tests')
class VisualizationTests(unittest.TestCase):
    def node(self, script):
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @staticmethod
    def app_stubs():
        return r"""
const assert=require('node:assert/strict');
class Element{constructor(){this.style={};this.dataset={};this.children=[];this.value='';this.checked=false;this.attributes={};this.textContent='';this.classes=new Set();
  this.classList={toggle:(c,on)=>on?this.classes.add(c):this.classes.delete(c),contains:c=>this.classes.has(c)};}
  appendChild(e){this.children.push(e);return e;}replaceChildren(...c){this.children=c;}setAttribute(k,v){this.attributes[k]=v;}}
const elements=new Map();
const box=(attr,name,checked=false)=>{const e=new Element();e.dataset[attr]=name;e.checked=checked;return e;};
const hides=[box('hide','isolates'),box('hide','artifacts',true)];
const statuses=['convicted','in_custody','under_investigation','deceased'].map(s=>box('status',s));
global.document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement(){return new Element();},
  querySelectorAll(q){return q==='[data-hide]'?hides:statuses;},querySelector(){return this.getElementById('main');},
  addEventListener(type,fn){this.keydown=fn;}};
global.GraphRenderer=class{constructor(container,{tooltip}){this.handlers={};this.tooltip=tooltip;this.layoutRunning=false;}
  setGraph(nodes,edges){this.nodes=nodes;this.edges=edges;}refresh(){}invalidateLayout(){}fit(ids){this.fitted=ids;}focus(id){this.focused=id;}
  startLayout(spacing){this.layoutRunning=true;this.spacing=spacing;}stopLayout(){this.layoutRunning=false;}on(name,fn){this.handlers[name]=fn;}};
"""

    def test_identity_review_attached_and_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'identity_review.json'
            path.write_text(json.dumps({'7': {'merged_records': {'a': 1, 'b': 9},
                                              'reattributed_in': [{'id': 'p', 'rule': 'reviewed_document_attribution', 'records': 3, 'documents': []}],
                                              'reattributed_out': [], 'kept_apart': []}}))
            with patch('corpus.document_urls', return_value={}):
                out = visualize_graph.with_identity_review([{'id': 7, 'merged_identities': [{'id': 'a'}, {'id': 'b'}]}, {'id': 8}], path)
        self.assertEqual([m['id'] for m in out[0]['merged_identities']], ['b', 'a'])  # most records first
        self.assertEqual(out[0]['identity_review']['reattributed_in'][0]['records'], 3)
        self.assertNotIn('identity_review', out[1])

    def test_expansion_lists_hidden_partners_strongest_first(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'communication_graph.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE edges(source_id, target_id, relationship_type, record_count, date_first, date_last)')
                db.execute('CREATE TABLE nodes(id, entity_type, label)')
                db.executemany('INSERT INTO edges VALUES (?,?,?,?,?,?)', [
                    ('kg:7', 'email_a', 'sent_to', 3, '2010', '2011'), ('participant_b', 'kg:7', 'cc_to', 9, '2012', '2013'),
                    ('kg:7', 'kg:8', 'sent_to', 50, '2010', '2010'),  # both in view: not an expansion
                    ('kg:7', 'kg:7', 'sent_to', 4, '2010', '2010'), ('kg:7', 'email_a', 'spoke_to', 99, '', '')])
                db.executemany('INSERT INTO nodes VALUES (?,?,?)', [('email_a', 'email', 'email_a'), ('participant_b', 'participant', 'participant_b')])
            out = visualize_graph.with_expansion([{'id': 7}, {'id': 8}], path, limit=1)
        self.assertEqual(out[0]['expand_total'], 2)
        self.assertEqual(out[0]['expand'], [{'id': 'participant_b', 'entity_type': 'participant', 'name': 'participant_b', 'relationship_type': 'cc_to',
                                             'records': 9, 'date_first': '2012', 'date_last': '2013', 'outgoing': False}])
        self.assertNotIn('expand', out[1])

    def test_expansion_uses_header_classes(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'communication_graph.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE edges(source_id, target_id, relationship_type, record_count, date_first, date_last)')
                db.execute('CREATE TABLE nodes(id, entity_type, label)')
                db.execute('CREATE TABLE node_classes(id, entity_type, label_quality)')
                db.executemany('INSERT INTO edges VALUES (?,?,?,?,?,?)', [
                    ('kg:7', 'participant_b', 'sent_to', 3, '', ''), ('kg:7', 'email_c', 'sent_to', 2, '', '')])
                db.executemany('INSERT INTO nodes VALUES (?,?,?)', [('participant_b', 'participant', 'b'), ('email_c', 'email', 'c')])
                db.executemany('INSERT INTO node_classes VALUES (?,?,?)', [('participant_b', 'participant', 'group_or_role'),
                                                                           ('email_c', 'organization', 'ok')])
            with patch('victim_policy.victim_ids', return_value={'email_c'}):
                out = visualize_graph.with_expansion([{'id': 7}], path)
        by_id = {x['id']: x for x in out[0]['expand']}
        self.assertEqual((by_id['participant_b']['entity_type'], by_id['participant_b']['label_quality']), ('participant', 'group_or_role'))
        self.assertEqual(by_id['email_c']['entity_type'], 'survivor')
        self.assertNotIn('label_quality', by_id['email_c'])

    def test_secret_reason_keys(self):
        from viewer_secret import reason_wanted
        wanted = {'1', '16', 'participant_a'}
        self.assertTrue(reason_wanted('apart:1|16', wanted))
        self.assertFalse(reason_wanted('apart:1|99', wanted))
        self.assertTrue(reason_wanted('attr:participant_a:16', wanted))
        self.assertTrue(reason_wanted('16', wanted))
        self.assertFalse(reason_wanted('17', wanted))

    def test_node_page_header_documents_follow_reattributions(self):
        import sqlite3
        from node_documents import header_documents, moved_in_documents
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE evidence(source_id, target_id, efta_number)')
        db.executemany('INSERT INTO evidence VALUES (?,?,?)', [('kg:1', 'participant_m', 'E1'), ('kg:1', 'participant_m', 'E2')])
        self.assertEqual(header_documents(db, 'participant_m'), [('E1', 1), ('E2', 1)])
        self.assertEqual(moved_in_documents(db, 'kg:5'), [])
        db.execute('CREATE TABLE reattributions(evidence_rowid, side, owner)')
        db.execute("INSERT INTO reattributions VALUES (2, 'target', 'kg:5')")
        self.assertEqual(header_documents(db, 'participant_m'), [('E1', 1)])
        self.assertEqual(header_documents(db, 'kg:1'), [('E1', 1), ('E2', 1)])  # the sender side did not move
        self.assertEqual(moved_in_documents(db, 'kg:5'), [('E2', 1)])

    def test_graph_logic(self):
        self.node(f'const L=require({json.dumps(str(VIEWER / "graph_logic.js"))});' + r"""
const assert=require('node:assert/strict');
const nodes=[1,2,3,4].map(id=>({id,record:{name:'N'+id,entity_type:id===1?'person':'aircraft'}}));
let nextId=0;
const edge=(from,to,type)=>({id:nextId++,from,to,record:{relationship_type:type}});
const edges=[edge(1,2,'a'),edge(2,1,'a'),edge(1,2,'b'),edge(1,3,'b'),edge(1,1,'a')];
assert.deepEqual([...L.connectionCounts(nodes,edges,'').values()],[2,1,1,0]);
assert.deepEqual([...L.connectionCounts(nodes,edges,'a').values()],[1,1,0,0]);
let view=L.entityVisibility(nodes,edges,new Set(),'a');
assert.equal(view.visible.size,4);assert.equal(view.visibleEdges.size,3);
view=L.entityVisibility(nodes,edges,new Set(['aircraft']),'');  // hidden types drop with their links
assert.deepEqual([...view.visible],[1]);assert.deepEqual([...view.visibleEdges],[4]);
// Links to type-hidden but eligible nodes come back as faded context (not self-loops, not ineligible ends).
assert.deepEqual(L.contextEdges(edges,view.visible,new Set([1,2,3]),'').map(e=>e.id),[0,1,2,3]);
assert.deepEqual(L.contextEdges(edges,view.visible,new Set([1,2]),'b').map(e=>e.id),[2]);
assert.equal(L.compactFactor(60,100),1);assert.equal(L.compactFactor(1,100),0.25);
assert.ok(L.compactFactor(20,100)>0.25&&L.compactFactor(20,100)<1);
const all=new Set(edges.map(e=>e.id)),typeOf=id=>nodes.find(n=>n.id===id).record.entity_type;
assert.equal(L.linkSelection(edges,all,'',new Set(),typeOf),all);
assert.deepEqual([...L.linkSelection(edges,all,'focus',new Set([3]),typeOf)],[3]);
assert.equal(L.linkSelection(edges,all,'type:aircraft',new Set(),typeOf).size,4);
assert.equal(L.linkSelection(edges,new Set([0]),'type:person',new Set(),typeOf).size,1);  // never beyond visible links
assert.equal(L.connectionSize(0,100),3);assert.equal(L.connectionSize(100,100),70);assert.equal(L.connectionSize(0,0),3);
nodes[0].record.legal_status_flags=['convicted'];
let f=L.filterStatuses(nodes,edges,new Set(['convicted']));
assert.deepEqual(f.nodes.map(n=>n.id),[2,3,4]);assert.equal(f.edges.length,0);
assert.equal(L.filterStatuses(nodes,edges,new Set()).edges.length,edges.length);
nodes[1].record.entity_type='participant';
f=L.filterLayers(nodes,edges,'',{unresolved:true});
assert.equal(f.nodes.length,3);assert.ok(f.edges.every(e=>e.from!==2&&e.to!==2));
nodes[2].record.label_quality='group_or_role';
assert.equal(L.filterLayers(nodes,edges,'',{artifacts:true}).nodes.length,3);
nodes[0].record.relevance='incidental';
assert.equal(L.filterLayers(nodes,edges,'',{incidental:true}).nodes.length,3);
assert.equal(L.filterLayers(nodes,edges,'',{incidental:false}).nodes.length,4);
delete nodes[0].record.relevance;
const co={id:99,from:1,to:4,record:{relationship_type:'co_mentioned_in_documents',metadata:{new_link:false}}};
assert.equal(L.filterLayers(nodes,[co],'',{coMentions:true}).edges.length,0);
assert.equal(L.filterLayers(nodes,[co],'',{linkedCoMentions:true}).edges.length,0);
assert.equal(L.filterLayers(nodes,[co],'',{}).edges.length,1);
const points=[{x:0,y:0,radius:34},{x:0,y:0,radius:7},{x:5,y:3,radius:20}];
L.separateDots(points);
for(let i=0;i<3;i++)for(let j=i+1;j<3;j++)assert.ok(Math.hypot(points[i].x-points[j].x,points[i].y-points[j].y)>=points[i].radius+points[j].radius);
const ring=L.ringPositions({x:0,y:0,radius:20},Array.from({length:52},(_,i)=>({id:i,radius:i%2?34:7})));
assert.equal(ring.length,52);
for(let i=0;i<52;i++)for(let j=i+1;j<52;j++)assert.ok(Math.hypot(ring[i].x-ring[j].x,ring[i].y-ring[j].y)>=68+20-1e-8);
const incident=new Map(nodes.map(n=>[n.id,edges.filter(e=>e.from===n.id||e.to===n.id)]));
assert.deepEqual(L.shortestPath(2,3,incident,all).map(e=>e.id),[0,3]);
assert.equal(L.shortestPath(2,3,incident,new Set([0])),null);
const rows=L.neighborRows(1,incident,all,id=>'N'+id);
assert.deepEqual(rows.map(r=>r[0]),[2,3]);assert.equal(rows[0][1].records,3);
edges[0].record.weight=7;edges[2].record.weight=4;
assert.equal(L.neighborRows(1,incident,all,id=>'N'+id)[0][1].records,12);
assert.equal(L.neighborRows(1,incident,new Set([0]),id=>'N'+id)[0][1].records,7);
const people=[{id:1,record:{name:'Company',entity_type:'organization'}},{id:2,record:{name:'Company',entity_type:'person'}},{id:3,record:{name:'Flagged Person Junior',entity_type:'person'}},{id:4,record:{name:'Flagged Person',entity_type:'person'}}];
const yes=()=>true;
assert.equal(L.matchNode('Company',people,yes,yes),null);  // never picks one of two
assert.equal(L.matchNode(L.entityChoice(people[1]),people,yes,yes).id,2);
assert.equal(L.matchNode('Flagged Person',people,id=>id!==4,yes).id,4);  // exact name wins even if filtered
assert.equal(L.matchNode('junior',people,yes,yes).id,3);
assert.equal(L.relationshipLabel('same_day_presence'),'same-day mentions');
assert.equal(L.relationshipLabel('victim_of'),'survivor of');
assert.equal(L.relationshipLabel('sent_to'),'sent to');
assert.equal(L.documentUrl({templates:[['https://x/','.pdf']],documents:{EFTA1:0}},'EFTA1'),'https://x/EFTA1.pdf');
assert.equal(L.pageUrl('https://x/EFTA1.pdf',4),'https://x/EFTA1.pdf#page=5');
assert.equal(L.pageUrl('https://x/IMAGES012/DOJ-OGR-00031411.jpg',31),'https://x/IMAGES012/DOJ-OGR-00031442.jpg');
assert.equal(L.pageUrl('https://x/EFTA1.pdf',undefined),'https://x/EFTA1.pdf');
""")

    def test_running_layout_tracks_visibility(self):
        renderer = (VIEWER / 'renderer.js').read_text()
        self.node(renderer + r"""
const assert=require('node:assert/strict');
global.requestAnimationFrame=()=>1;
const workers=[];
global.Worker=class{constructor(){workers.push(this);}postMessage(m){this.input=m;}terminate(){this.terminated=true;}};
const r=Object.create(GraphRenderer.prototype);
r.nodes=[1,2,3].map(id=>({id,x:id*10,y:0,size:7}));
r.nodeById=new Map(r.nodes.map(n=>[n.id,n]));
r.edges=[{id:1,from:1,to:2},{id:2,from:2,to:3}];
r.startLayout(320);
const first=r.worker;
assert.equal(first.input.positions.length,6);assert.equal(first.input.edges.length,4);
r.nodes[2].hidden=true;r.refresh();
assert.ok(first.terminated);assert.notEqual(r.worker,first);
assert.equal(r.worker.input.positions.length,4);assert.equal(r.worker.input.edges.length,2);
assert.equal(r.worker.input.k,320*.35);
const second=r.worker;r.refresh();assert.equal(r.worker,second);
// Late messages from a replaced worker cannot move nodes.
const before=r.nodes.map(n=>n.x);first.onmessage({data:new Float32Array(6).fill(999)});
assert.deepEqual(r.nodes.map(n=>n.x),before);
r.edges[0].hidden=true;r.refresh();assert.ok(second.terminated);assert.equal(r.worker.input.edges.length,0);
r.nodes[2].hidden=false;r.edges.forEach(e=>e.hidden=false);r.refresh();
assert.equal(r.worker.input.positions.length,6);assert.equal(r.worker.input.edges.length,4);
r.stopLayout();assert.equal(r.layoutRunning,false);
""")

    def test_app_filters_selection_and_layout(self):
        records = [(1, 'Flagged Person', 'person', ['convicted']), (2, 'Company', 'organization', []),
                   (3, 'Aircraft', 'vehicle', []), (4, 'Isolate', 'person', []), (5, 'Place', 'place', [])]
        subtypes = {3: 'aircraft', 5: 'address'}
        payload = {'nodes': [dict(id=i, label=name, x=i * 100.0, y=0.0, record=dict(id=i, name=name, entity_type=kind, legal_status_flags=flags,
                                                                                  **({'subtype': subtypes[i]} if i in subtypes else {})))
                             for i, name, kind, flags in records],
                   'edges': [dict(id=1, **{'from': 1, 'to': 2}, record=dict(relationship_type='sent_to', weight=1)),
                             dict(id=2, **{'from': 1, 'to': 3}, record=dict(relationship_type='associated_with', weight=1))]}
        stubs = self.app_stubs()
        logic = (VIEWER / 'graph_logic.js').read_text()
        app = (VIEWER / 'app.js').read_text()
        assertions = r"""
const N=id=>nodeById.get(id),E=id=>edgeById.get(id),status=()=>document.getElementById('status').textContent;
assert.equal(N(5).hidden,true);  // locations start hidden
assert.equal(typeButtons.get('participant').textContent,'Participants');
assert.equal(renderer.layoutRunning,false);assert.equal(state.hide.artifacts,true);assert.equal(state.hide.linkedCoMentions,true);
assert.match(status(),/4 dots · 2 links · 2 active filters/);
const sub=k=>{state.hiddenTypes.has(k)?state.hiddenTypes.delete(k):state.hiddenTypes.add(k);refreshTypeButtons();update();};
sub('place:address');assert.equal(N(5).hidden,false);
sub('place:address');assert.equal(N(5).hidden,true);
assert.ok(N(1).size>N(2).size);assert.equal(N(4).size,3);
statuses[0].checked=true;statuses[0].onchange();
assert.equal(N(1).hidden,true);assert.equal(E(1).hidden,true);assert.equal(N(2).count,0);
document.getElementById('search').value='Flagged Person';find();
assert.match(status(),/hidden by a Hide option/);
statuses[0].checked=false;statuses[0].onchange();
assert.equal(N(1).hidden,false);assert.equal(E(1).hidden,false);assert.equal(N(1).count,2);
typeButtons.get('vehicle').onclick();
assert.equal(N(3).hidden,true);assert.equal(E(2).hidden,true);assert.equal(E(1).hidden,false);
assert.equal(N(1).count,1);assert.equal(N(3).count,0);assert.equal(N(1).size,N(2).size);
focusNode(1);assert.match(document.getElementById('detail-sub').textContent,/1 connections/);
typeButtons.get('organization').onclick();
hides[0].checked=true;hides[0].onchange();
assert.equal(N(1).hidden,true);assert.equal(document.getElementById('detail-title').textContent,'Nothing selected');
assert.equal(document.getElementById('raw').textContent,'');assert.equal(document.getElementById('docs').children.length,0);
hides[0].checked=false;hides[0].onchange();typeButtons.get('organization').onclick();
document.getElementById('search').value='Aircraft';find();  // finding a hidden-type node shows its type again
assert.equal(N(3).hidden,false);assert.equal(renderer.focused,3);assert.equal(N(3).highlighted,true);
allButton.onclick();assert.equal(data.nodes.every(n=>!n.hidden),true);  // some hidden -> All
allButton.onclick();assert.equal(data.nodes.every(n=>n.hidden),true);   // all shown -> None
allButton.onclick();
typeButtons.get('place').onclick();
select();document.getElementById('link-mode').value='focus';document.getElementById('link-mode').onchange();
assert.match(status(),/0 links/);
assert.equal(E(1).hidden,true);assert.equal(E(2).hidden,true);
renderer.handlers.click({node:N(2)});
assert.equal(E(1).hidden,false);assert.equal(E(2).hidden,true);
assert.equal(document.getElementById('detail-title').textContent,'Company');
assert.equal(document.getElementById('details').hidden,false);
document.keydown({key:'Escape',target:{tagName:'CANVAS'}});assert.equal(document.getElementById('details').hidden,true);
assert.equal(document.getElementById('detail-title').textContent,'Nothing selected');
document.keydown({key:'[',target:{tagName:'CANVAS'}});assert.equal(document.querySelector('main').classList.contains('panel-hidden'),true);
document.keydown({key:'[',target:{tagName:'INPUT'}});assert.equal(document.querySelector('main').classList.contains('panel-hidden'),true);
document.getElementById('panel-toggle').onclick();assert.equal(document.querySelector('main').classList.contains('panel-hidden'),false);
renderer.handlers.click({node:N(2)});
renderer.handlers.hover({node:N(3)});assert.equal(E(2).hidden,false);
renderer.handlers.hover({node:null});renderer.handlers.click({});assert.equal(E(1).hidden,true);
document.getElementById('path-from').value='Company';document.getElementById('path-to').value='Aircraft';findPath();
assert.equal(E(1).hidden,false);assert.equal(E(2).hidden,false);assert.deepEqual(renderer.fitted,[2,1,3]);
document.getElementById('clear').onclick();assert.equal(E(1).hidden,true);
document.getElementById('link-mode').value='';document.getElementById('link-mode').onchange();
hides[0].checked=true;hides[0].onchange();assert.equal(N(4).hidden,true);
hides[0].checked=false;hides[0].onchange();assert.equal(N(4).hidden,false);
// Layout: toggle starts/stops; spacing restarts a running layout.
document.getElementById('layout-toggle').onclick();assert.equal(renderer.layoutRunning,true);
document.getElementById('spacing').value='400';document.getElementById('spacing').oninput();assert.equal(renderer.spacing,400);
// Double-click gathers drawn neighbours only, leaves others, and freezes the layout.
const before=data.nodes.map(n=>[n.id,n.x,n.y]);
renderer.handlers.doubleclick({node:N(1)});
assert.equal(renderer.layoutRunning,false);
const moved=before.filter(([id,x,y])=>N(id).x!==x||N(id).y!==y).map(([id])=>id);
assert.deepEqual(moved.sort(),[2,3]);
for(const id of [2,3])assert.ok(Math.hypot(N(id).x-N(1).x,N(id).y-N(1).y)>=N(1).size+N(id).size+20-1e-8);
assert.match(status(),/Gathered 2/);
// Double-click on a dot switches to drawing hovered / selected links only; on empty space back to all links.
assert.equal(document.getElementById('link-mode').value,'focus');assert.equal(E(1).hidden,true);
renderer.handlers.doubleclick({node:null});
assert.equal(document.getElementById('link-mode').value,'');assert.equal(E(1).hidden,false);
assert.match(renderer.tooltip('node',N(1)),/Flagged Person \(person\) · 2 connections · status flags/);
// Co-mention selector preserves all three modes, and filtering refreshes visible node details.
E(2).record.relationship_type='co_mentioned_in_documents';E(2).record.metadata={new_link:false};
focusNode(1);update();assert.equal(E(2).hidden,true);assert.match(document.getElementById('detail-sub').textContent,/1 connections/);
const co=document.getElementById('co-mentions');co.value='all';co.onchange();assert.equal(E(2).hidden,false);
assert.match(document.getElementById('detail-sub').textContent,/2 connections/);
renderer.handlers.click({edge:E(2)});co.value='hidden';co.onchange();
assert.equal(E(2).hidden,true);assert.equal(document.getElementById('detail-title').textContent,'Nothing selected');
E(2).record.metadata.new_link=true;co.value='new';co.onchange();assert.equal(E(2).hidden,false);
co.value='hidden';co.onchange();assert.equal(E(2).hidden,true);
// Reset restores filter, drawing and layout defaults and their controls.
statuses[0].checked=true;statuses[0].onchange();hides[0].checked=true;hides[0].onchange();
hides[1].checked=false;hides[1].onchange();
document.getElementById('relationship').value='sent_to';document.getElementById('relationship').onchange();
document.getElementById('link-mode').value='focus';document.getElementById('link-mode').onchange();
document.getElementById('layout-toggle').onclick();document.getElementById('reset').onclick();
assert.deepEqual([...state.hiddenTypes],['place:address','place:airport','place:institution','place:city','place:other','participant']);assert.equal(state.hiddenStatuses.size,0);
assert.deepEqual(state.hide,{artifacts:true,linkedCoMentions:true,incidental:true});assert.equal(co.value,'new');
assert.equal(state.relationship,'');assert.equal(state.linkMode,'');assert.equal(state.spacing,240);
assert.equal(renderer.layoutRunning,false);assert.equal(hides[0].checked,false);assert.equal(hides[1].checked,true);
assert.ok(statuses.every(b=>!b.checked));assert.match(status(),/2 active filters/);
N(4).record.suspected_parse_artifact=true;update();assert.equal(N(4).hidden,true);
hides[1].checked=false;hides[1].onchange();assert.equal(N(4).hidden,false);
N(4).record.entity_type='participant';update();assert.equal(N(4).hidden,true);  // participants start hidden until unlocked
typeButtons.get('participant').onclick();assert.equal(N(4).hidden,false);
N(4).record.entity_type='person';N(4).record.dual_status=true;state.hiddenTypes.add('person');update();assert.equal(N(4).hidden,false);  // half survivor: shown by participant
state.hiddenTypes.add('participant');update();assert.equal(N(4).hidden,false);  // ...and by survivor
state.hiddenTypes.add('survivor');update();assert.equal(N(4).hidden,true);for (const t of ['person','survivor']) state.hiddenTypes.delete(t);N(4).record.dual_status=false;update();
document.getElementById('path-from').value='Company';document.getElementById('path-to').value='Aircraft';findPath();
assert.equal(document.getElementById('detail-title').textContent,'2-step path');
typeButtons.get('vehicle').onclick();assert.equal(document.getElementById('detail-title').textContent,'Nothing selected');

"""
        script = (stubs + logic + f'const data={json.dumps(payload)};const COLORS={{"person":"#60a5fa","organization":"#34d399","vehicle":"#facc15","place":"#c084fc","participant":"#a3e635"}};const SECRET=null;const PUBLIC_REVEAL={{}};'
                  + app + assertions)
        self.node(script)

    def test_lazy_chunks_roundtrip_lru_and_retry(self):
        import base64
        import gzip
        import re
        from visualize_graph import lazy_payload
        nodes = [dict(id=i, label=f'N{i}', x=i, y=0, record={
            'id': i, 'name': f'N{i}', 'entity_type': 'person', 'note': '</script> & é',
            'merged_identities': [{'id': 'participant_p', 'value': 'Pseudonym'}],
            'expand': [{'id': 'email_x', 'name': 'Email_x'}],
            'identity_review': {'kept_apart': [{'id': 'participant_p'}]},
            'documents': {'n': 1, 'docs': [{'efta_number': 'EFTA00000001'}]}}) for i in range(520)]
        edges = [dict(id='e', **{'from': 0, 'to': 1}, title='redundant', color='#123456', record={
            'id': 'e', 'relationship_type': 'co_mentioned_in_documents', 'weight': 4,
            'metadata': {'new_link': False, 'meaning_ref': 0, 'sample_documents': [{'efta_number': 'EFTA00000001'}]},
            'documents': {'n': 1, 'docs': [{'efta_number': 'EFTA00000001', 'page': 2}]}})]
        boot, blocks = lazy_payload(nodes, edges, {'templates': [['https://www.justice.gov/', '.pdf']],
                                                  'documents': {'EFTA00000001': 0}}, ['Meaning'], {'EFTA00000001': 'Subpoena'})
        skeleton = json.loads(gzip.decompress(base64.b64decode(boot)))
        self.assertNotIn('urls', skeleton)
        self.assertNotIn('expand', skeleton['nodes'][0]['record'])
        self.assertEqual(skeleton['edges'][0]['record']['metadata'], {'new_link': False})
        self.assertNotIn('title', skeleton['edges'][0])
        self.assertNotIn('docs', skeleton['nodes'][0]['record']['documents'])
        self.assertNotIn('note', skeleton['nodes'][0]['record'])
        chunks = re.findall(r'id="detail-\d+">([^<]+)</script>', blocks)
        self.assertEqual(len(chunks), 3)
        self.assertNotIn('</script> & é', blocks)
        script = (VIEWER / 'graph_logic.js').read_text() + f'\nconst chunks={json.dumps(chunks)},boot={json.dumps(boot)};\n' + r"""
const assert=require('node:assert/strict');
(async()=>{
  assert.equal(JSON.parse(await decodeGzip(boot)).nodes.length,520);
  let reads=0;
  const store=new DetailStore(i=>{reads++;return chunks[i]},256,2);
  const [a,b]=await Promise.all([store.get(0),store.get(1)]);
  assert.equal(reads,1);assert.equal(a.note,'</script> & é');assert.equal(b.name,'N1');
  assert.equal(a.documents.docs[0].pdf_url,'https://www.justice.gov/EFTA00000001.pdf');
  assert.equal(a.documents.docs[0].document_info,'Subpoena');
  await store.get(256);await store.get(0);await store.get(520);
  assert.deepEqual([...store.cache.keys()],[0,2]);
  await store.get(256);assert.equal(reads,4);assert.equal(store.cache.size,2);
  const limited=new DetailStore(i=>chunks[i],256,4,1);await limited.get(0);
  assert.equal(limited.cache.size,0);assert.equal(limited.bytes,0);
  let fail=true;
  const retry=new DetailStore(i=>{if(fail)throw new Error('failed');return chunks[i]});
  await assert.rejects(retry.get(0),/failed/);assert.equal(retry.pending.size,0);
  fail=false;assert.equal((await retry.get(0)).name,'N0');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        # Use stdin: realistic chunks can exceed the OS argument length limit.
        result = subprocess.run(['node'], input=script, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lazy_panels_reveal_expand_export_and_races(self):
        from visualize_graph import gzip_json
        nodes = [dict(id=i, label=f'N{i}', x=i * 300, y=0,
                      detail=i, record={'id': i, 'name': f'N{i}', 'entity_type': 'person'}) for i in [0, 1]]
        edges = [dict(id='e', **{'from': 0, 'to': 1}, detail=2,
                      record={'id': 'e', 'relationship_type': 'sent_to', 'weight': 8})]
        docs = {'n': 1, 'docs': [{'efta_number': 'EFTA00000001', 'pdf_url': 'https://www.justice.gov/a.pdf', 'document_info': 'Subpoena'}]}
        records = [dict(nodes[0]['record'], note='Node detail', documents=docs, expand_total=1,
                        expand=[{'id': 'participant_x', 'name': 'Hidden', 'entity_type': 'participant',
                                 'relationship_type': 'sent_to', 'records': 3, 'outgoing': True}],
                        merged_identities=[{'id': 'email_x', 'value': 'Email_x', 'records': 2, 'rule': 'r'}],
                        identity_review={'kept_apart': [{'id': 'participant_x', 'canonical': 'participant_x', 'member': 0}]}),
                   dict(nodes[1]['record'], note='Second detail', documents=docs),
                   dict(edges[0]['record'], documents=docs, metadata={'meaning_ref': 0})]
        payload = {'nodes': nodes, 'edges': edges, 'meanings': ['Full edge meaning'], 'detailChunkSize': 1}
        chunks = [gzip_json([r]) for r in records]
        script = (self.app_stubs() + (VIEWER / 'graph_logic.js').read_text()
                  + f'\nconst data={json.dumps(payload)},COLORS={{person:"#fff",participant:"#aaa"}},SECRET=null,PUBLIC_REVEAL={{0:"email:123456abcdef",email_x:"public@example.test"}};'
                  + (VIEWER / 'app.js').read_text()
                  + f'\nconst chunks={json.dumps(chunks)};\n' + r"""
(async()=>{
  assert.equal(detailStore.cache.size,0);
  assert.equal(nodeById.get(0).label,'email:123456abcdef');
  let release;
  const read=detailStore.read;
  detailStore.read=i=>i===0?new Promise(resolve=>{release=()=>resolve(chunks[i])}):chunks[i];
  const old=showNode(nodeById.get(0));await Promise.resolve();
  await showNode(nodeById.get(1));assert.equal($('detail-title').textContent,'N1');
  release();await old;assert.equal($('detail-title').textContent,'N1');
  const closing=showNode(nodeById.get(0));clearDetails();await closing;
  assert.equal($('details').hidden,true);assert.equal(rawRecord,null);
  detailStore.read=i=>chunks[i];
  await showNode(nodeById.get(0));
  assert.equal(rawRecord.note,'Node detail');assert.equal($('raw').textContent,'');
  $('raw-box').open=true;$('raw-box').ontoggle();assert.match($('raw').textContent,/Node detail/);
  assert.equal(rawRecord.merged_identities[0].value,'public@example.test');
  applySecret({reveal:{participant_x:{value:'Real Hidden'},0:{value:'Real Node',fields:{note:'Private detail',metadata:{contact:'private@example.test'}}}},pages:{0:'node_docs/a.html'}});
  await showNode(nodeById.get(0));
  assert.equal(rawRecord.note,'Private detail');assert.equal(rawRecord.metadata.contact,'private@example.test');
  assert.equal(rawRecord.name,'Real Node');assert.equal(rawRecord.pseudonym,'N0');
  assert.equal(rawRecord.expand[0].value,'Real Hidden');assert.equal(rawRecord.identity_review.kept_apart[0].value,'Real Hidden');
  assert.equal(rawRecord.document_page,'node_docs/a.html');
  // Eviction/reload still applies the reveal, without mutating hot graph records.
  detailStore.cache.clear();detailStore.bytes=0;
  assert.equal((await fullRecord(nodeById.get(0))).merged_identities[0].value,'public@example.test');
  assert.equal(nodeById.get(0).record.expand,undefined);
  const pendingExpand=expandNode(0);collapseNode(0);await pendingExpand;assert.equal(expansions.size,0);
  await Promise.all([expandNode(0),expandNode(0)]);
  assert.equal(expansions.size,1);assert.equal(nodeById.get('participant_x').record.name,'Real Hidden');
  assert.equal(nodeById.get('alias:email_x').record.name,'public@example.test');
  collapseNode(0);assert.equal(data.nodes.length,2);assert.equal(data.edges.length,1);
  await showEdge(edgeById.get('e'));assert.equal(rawRecord.metadata.meaning,'Full edge meaning');
  assert.equal(rawRecord.documents.docs[0].pdf_url,'https://www.justice.gov/a.pdf');
  document.getElementById('path-from').value='Real Node';document.getElementById('path-to').value='N1';findPath();
  assert.equal($('detail-title').textContent,'1-step path');assert.equal(rawRecord,null);
  // Export must read full edge and node details even after every cached chunk is evicted.
  detailStore.cache.clear();detailStore.bytes=0;
  let exported;const create=URL.createObjectURL;URL.createObjectURL=blob=>{exported=blob;return 'blob:test'};
  Element.prototype.click=function(){};
  await exportNode(nodeById.get(0));URL.createObjectURL=create;
  const markdown=await exported.text();assert.match(markdown,/Full edge meaning/);
  assert.match(markdown,/EFTA00000001/);assert.match(markdown,/Subpoena/);assert.match(markdown,/Real Hidden/);
  assert.ok(detailStore.cache.size<=4);assert.ok(detailStore.bytes<=4*1024*1024);
})().catch(e=>{console.error(e);process.exitCode=1});
""")
        self.node(script)

    def test_edge_tooltips_match_builder(self):
        from visualize_graph import edge_title
        records = [dict(relationship_type=kind, weight=7, metadata={'shared_documents': 9, 'specificity_score': 1.25})
                   for kind in ['sent_to', 'cc_to', 'header_label_association', 'upstream_resolved_header_identity',
                                'co_mentioned_in_documents', 'same_day_presence', 'same_day_document_mentions',
                                'shares_exported_efta_reference', 'victim_of', 'victim_document_co_mention', 'unknown']]
        self.node((VIEWER / 'graph_logic.js').read_text() + '\nconst assert=require("node:assert/strict");'
                  + f'const records={json.dumps(records)},expected={json.dumps([edge_title(r) for r in records])};'
                  + 'assert.deepEqual(records.map(edgeTitle),expected);')

    def test_compressed_html_bootstrap_without_fetch(self):
        from visualize_graph import ASSETS, COLORS, lazy_payload, render
        nodes = [dict(id=1, label='Fixture', x=0, y=0, record={'id': 1, 'name': 'Fixture', 'entity_type': 'person',
                                                            'note': 'Loaded lazily'})]
        boot, chunks = lazy_payload(nodes, [], {'templates': [], 'documents': {}}, [], {})
        html = render((ASSETS / 'viewer.html').read_text(), {
            'RENDERER_JS': (ASSETS / 'renderer.js').read_text(), 'LOGIC_JS': (ASSETS / 'graph_logic.js').read_text(),
            'APP_JS': (ASSETS / 'app.js').read_text(), 'PAYLOAD': boot, 'DETAIL_CHUNKS': chunks,
            'COLORS_JSON': json.dumps(COLORS), 'SECRET_JSON': 'null', 'PUBLIC_REVEAL_JSON': '{}'})
        # Execute the real HTML script order, including decompression and the dynamic
        # app script. DOM/GL alone are stubbed; no network or fetch is available.
        self.node(self.app_stubs() + f'\nlet html={json.dumps(html)};\n' + r"""
const vm=require('node:vm'), StubRenderer=GraphRenderer;
global.fetch=()=>{throw new Error('Unexpected fetch')};
Element.prototype.remove=function(){if(this.id)elements.delete(this.id);this.textContent='';};
document.body=new Element();
document.body.appendChild=script=>{global.GraphRenderer=StubRenderer;vm.runInThisContext(script.textContent);return script;};
for(const match of html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)){
  const id=/id="([^"]+)"/.exec(match[1]);
  if(id){const block=document.getElementById(id[1]);block.id=id[1];block.textContent=match[2];}
  else vm.runInThisContext(match[2]);
}
html=null;
(async()=>{
  // Stream decompression completes on a later event-loop turn.
  for(let i=0;i<200&&!document.getElementById('status').textContent.includes('dots');i++)
    await new Promise(resolve=>setTimeout(resolve,5));
  assert.match(document.getElementById('status').textContent,/1 dots/);
  assert.equal(elements.has('viewer-data'),false);assert.equal(elements.has('viewer-app'),false);
  assert.equal(vm.runInThisContext('detailStore.cache.size'),0);
  await vm.runInThisContext('showNode(data.nodes[0])');
  assert.equal(vm.runInThisContext('rawRecord.note'),'Loaded lazily');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_secret_decrypt_supports_legacy_and_gzip(self):
        from viewer_secret import encrypt
        import gzip
        app = (VIEWER / 'app.js').read_text()
        decrypt = app[app.index('async function decryptSecret('):app.index('// reveal:')]
        raw = json.dumps({'reveal': {'p': {'value': 'Test Name'}}}).encode()
        legacy = encrypt(raw, 'test-password', iterations=1000)
        compressed = {**encrypt(gzip.compress(raw), 'test-password', iterations=1000), 'compression': 'gzip'}
        self.node('const assert=require("node:assert/strict");' + f'let SECRET;const secrets={json.dumps([legacy, compressed])};'
                  + decrypt + r"""
(async()=>{for(const secret of secrets){SECRET=secret;
  assert.deepEqual(await decryptSecret('test-password'),{reveal:{p:{value:'Test Name'}}});
  await assert.rejects(decryptSecret('wrong-password'));
}})().catch(e=>{console.error(e);process.exitCode=1});
""")


class RenderTests(unittest.TestCase):
    def test_document_index_subset_preserves_counts_samples_and_records(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'documents.db'
            with sqlite3.connect(path) as db:
                db.executescript('CREATE TABLE node_documents(node_id,efta,edge_id,page);'
                    'CREATE TABLE node_records(node_id,kind,ref,url);'
                    'CREATE TABLE edge_documents(edge_id,efta,page,how);'
                    'CREATE TABLE edge_records(edge_id,kind,ref,url);')
                db.executemany('INSERT INTO node_documents VALUES (?,?,?,?)',[
                    ('a','EFTA00000001',None,1),('a','EFTA00000002','e',2),('b','EFTA00000003',None,3)])
                db.execute("INSERT INTO node_records VALUES ('a','citation','ref','url')")
                db.execute("INSERT INTO edge_documents VALUES ('e','EFTA00000001',1,'email header')")
                db.execute("INSERT INTO edge_records VALUES ('e','citation','ref','url')")
            db.close()
            with patch('corpus.document_urls',return_value={}):
                edges,nodes=visualize_graph.document_index(path)
                limited_edges,limited_nodes=visualize_graph.document_index(path,node_ids={'a'},edge_ids={'e'})
                empty_edges,empty_nodes=visualize_graph.document_index(path,node_ids=set(),edge_ids=set())
            self.assertEqual(limited_edges,{'e':edges['e']})
            self.assertEqual(limited_nodes,{'a':nodes['a']})
            self.assertEqual(limited_nodes['a']['n'],2)
            self.assertEqual((empty_edges,empty_nodes),({},{}))

    def test_inserted_text_is_not_rescanned(self):
        from visualize_graph import render
        values = {'PAYLOAD': '{"name": "const colors=COLORS; VIS_JS"}', 'const colors=COLORS;': 'const colors={};', 'VIS_JS': 'js'}
        self.assertEqual(render('VIS_JS const data=PAYLOAD; const colors=COLORS;', values),
                         'js const data={"name": "const colors=COLORS; VIS_JS"}; const colors={};')


class PrivateOutputTests(unittest.TestCase):
    def test_atomic_private_write_and_failure(self):
        from node_documents import private_write
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private.html'
            path.write_text('old')
            path.chmod(0o644)
            replace = os.replace

            def inspect(source, target):
                self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                replace(source, target)

            with patch('node_documents.os.replace', side_effect=inspect):
                private_write(path, 'secret')
            self.assertEqual(path.read_text(), 'secret')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch('node_documents.os.replace', side_effect=OSError('failure')):
                with self.assertRaises(OSError):
                    private_write(path, 'replacement')
            self.assertEqual(path.read_text(), 'secret')
            self.assertEqual(list(Path(directory).iterdir()), [path])
            path.unlink()
            private_write(path, 'new secret')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cache_dependencies_and_legacy_permissions(self):
        import node_documents as docs
        from adapters import efta
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'output').mkdir()
            (root / 'data/raw').mkdir(parents=True)
            entities = [{'id': 1, 'name': 'Name'}]
            identities = {'email_a': {'canonical': 1, 'rule': 'rule'}}
            lookup = {'email_a': {'value': 'secret@example.test'}}
            (root / 'output/graph_entities.json').write_text(json.dumps(entities))
            (root / 'output/identity_map.json').write_text(json.dumps(identities))
            (root / 'output/graph_relationships.json').write_text('[]')
            db = root / 'output/communication_graph.db'
            corpus = root / 'data/raw/full_text_corpus.db'
            urls = root / 'urls.gz'
            logic = root / 'logic.py'
            corpus_logic = root / 'corpus.py'
            for path in (db, corpus, urls, logic, corpus_logic):
                path.write_text('initial')
            with patch.object(docs, 'ROOT', root), patch.object(efta, 'URLS', urls), patch.object(docs.corpus, 'PATH', corpus), \
                    patch.object(docs, '__file__', str(logic)), patch.object(docs.corpus, '__file__', str(corpus_logic)):
                stamp = lambda: docs.cache_stamp(entities, identities, lookup, b'local key')
                previous = stamp()
                for change in (
                    lambda: entities[0].update(name='Renamed'),
                    lambda: identities['email_a'].update(rule='new rule'),
                    lambda: lookup['email_a'].update(value='changed@example.test'),
                    lambda: db.write_text('new evidence'),
                    lambda: corpus.write_text('new corpus'),
                    lambda: Path(str(db) + '-wal').write_text('WAL'),
                    lambda: Path(str(corpus) + '-wal').write_text('WAL'),
                    lambda: urls.write_text('new urls'),
                    lambda: logic.write_text('new logic'),
                    lambda: corpus_logic.write_text('new corpus search logic'),
                ):
                    change()
                    current = stamp()
                    self.assertNotEqual(previous, current)
                    previous = current
                self.assertNotEqual(previous, docs.cache_stamp(entities, identities, lookup, b'rotated key'))
                (root / 'output/graph_entities.json').write_text(json.dumps(entities))
                (root / 'output/identity_map.json').write_text(json.dumps(identities))
                out = root / 'private/node_docs'
                out.mkdir(parents=True)
                page = out / docs.page_name(1)
                page.write_text('private evidence')
                key = b'local key'
                (out / '.cache-key').write_text(key.hex())
                cached = {'stamp': stamp(), 'pages': {'1': 'node_docs/node_1.html'}}
                (out / '.stamp.json').write_text(json.dumps(cached))
                for file in out.iterdir():
                    file.chmod(0o644)
                with patch('multiprocessing.get_context', side_effect=AssertionError('cache miss')):
                    self.assertEqual(docs.write_pages(lookup, out), {1: 'node_docs/node_1.html'})
                self.assertEqual(out.stat().st_mode & 0o777, 0o700)
                for file in out.iterdir():
                    self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                self.assertNotIn('example.test', (out / '.stamp.json').read_text())

    def test_private_person_email_never_plaintext_in_public_build(self):
        import sqlite3
        import viewer_secret
        from audit_public_contacts import viewer_texts
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);out=root/'output';out.mkdir();private=root/'data/private'
            person={'id':'participant_private','name':'Participant_abcdef','entity_type':'participant',
                    'merged_identities':[{'id':'email_private','value':'Email_abcdef','rule':'header_identity'}]}
            (out/'graph_entities.json').write_text(json.dumps([person]))
            (out/'graph_relationships.json').write_text('[]')
            with sqlite3.connect(out/'contact_graph.db') as db:
                db.execute('CREATE TABLE resolutions(member,canonical,rule,organisation)')
                db.execute('CREATE TABLE nodes(id,canonical,owner)')
                db.execute('CREATE TABLE mentions(id,efta,page)')
                db.execute("INSERT INTO resolutions VALUES ('email_private','participant_private','header_identity',NULL)")
            db.close()
            address='private.person@gmail.com';target=out/'graph.html'
            with patch.object(visualize_graph,'ROOT',root),patch.object(viewer_secret,'PRIVATE',private), \
                 patch.object(viewer_secret,'SECRET',private/'viewer_secret.json'):
                viewer_secret.write_secret({'email_private':{'value':address,'kind':'email'},
                    'participant_private':{'value':'Private Person'}},{})
                with contextlib.redirect_stdout(io.StringIO()):
                    visualize_graph.cli(['--public','--no-rebuild'],path=target)
            html=target.read_text()
            self.assertNotIn(address,html)
            self.assertTrue(all(address not in block for block in viewer_texts(html)))
            self.assertIn('const SECRET = null;',html)
            public=json.loads(html.split('const PUBLIC_REVEAL = ',1)[1].split(';</script>',1)[0])
            self.assertRegex(public['email_private'],r'^email:[a-f0-9]{12}$')
            self.assertIn(address,(private/'viewer_secret.json').read_text())

    def test_viewer_embeds_real_names_only_encrypted(self):
        import visualize_graph as viewer
        import viewer_secret
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'output').mkdir()
            entities = [{'id': 'email_a', 'name': 'Email_a1', 'entity_type': 'email',
                         'metadata': {'contact':'real@example.test'},
                         'merged_identities': [{'id': 'email_b', 'value': 'Email_b2', 'rule': 'r'},
                                               {'id': 'email_capped', 'rule':'r'}]},
                        {'id': 'phone_c', 'name': 'Phone_c3', 'entity_type': 'phone'},
                        {'id': 'participant_p', 'name': 'Participant_ab4', 'entity_type': 'participant',
                         'metadata': {'contact':'real@example.test'}}]
            (root / 'output/graph_entities.json').write_text(json.dumps(entities))
            (root / 'output/graph_relationships.json').write_text('[]')
            private = root / 'data/private'
            reveal = {'email_a': {'value': 'real@example.test', 'possible_duplicates': []},
                      'email_b': {'value': 'other@example.test', 'possible_duplicates': []},
                      'email_capped': {'value': 'capped@example.test', 'possible_duplicates': []},
                      'email_unused': {'value': 'unused@example.test', 'possible_duplicates': []},
                      'participant_p': {'value': 'Real Person Name', 'possible_duplicates': []}}
            target = root / 'output/graph.html'
            with patch.object(viewer, 'ROOT', root), patch.object(viewer_secret, 'PRIVATE', private), \
                    patch.object(viewer_secret, 'SECRET', private / 'viewer_secret.json'), \
                    patch.object(viewer_secret, 'PASSWORD', private / 'viewer_password'), \
                    patch.object(viewer_secret, 'ITERATIONS', 1000), patch.object(viewer, 'MAX_MERGED', 1), contextlib.redirect_stdout(io.StringIO()):
                viewer_secret.write_secret(reveal, {'email_a': 'node_docs/node_a.html'})
                self.assertEqual((private / 'viewer_secret.json').stat().st_mode & 0o777, 0o600)
                viewer.main(path=target, rebuild=False)  # no password yet: pseudonyms only
                self.assertIn('const SECRET = null;', target.read_text())
                viewer_secret._write_private(private / 'viewer_password', 'correct horse battery')
                viewer.main(path=target, rebuild=False)
                html = target.read_text()
                viewer.cli(['--no-rebuild', '--public'], path=target)
                self.assertIn('const SECRET = null;', target.read_text())
            # Unresolved/private emails are hashed in both build modes; unlock retains values.
            public = json.loads(html.split('const PUBLIC_REVEAL = ', 1)[1].split(';</script>', 1)[0])
            self.assertEqual(set(public), {'email_a','email_b'})
            for label in public.values():
                self.assertRegex(label, r'^email:[0-9a-f]{12}$')
            for value in ('real@example.test','other@example.test'):
                self.assertNotIn(value, html)
                self.assertNotIn(value, target.read_text())
            self.assertNotIn('unused@example.test', html)
            self.assertNotIn('capped@example.test', html)
            self.assertNotIn('email_capped', public)
            self.assertNotIn('Real Person Name', html)
            secret = json.loads(html.split('const SECRET = ', 1)[1].split('; const PUBLIC_REVEAL', 1)[0])
            app = (VIEWER / 'app.js').read_text()
            decrypt = app[app.index('async function decryptSecret('):app.index('function applySecret(')]
            for password, expected in (('correct horse battery', 'ok'), ('wrong password!!', 'rejected')):
                result = subprocess.run(['node', '-e', f'const SECRET={json.dumps(secret)};' + decrypt + f"""
decryptSecret({json.dumps(password)}).then(r=>console.log(JSON.stringify(r)),()=>console.log('"rejected"'));"""],
                                        capture_output=True, text=True, check=True)
                out = json.loads(result.stdout)
                if expected == 'rejected':
                    self.assertEqual(out, 'rejected')
                    continue
                self.assertEqual(set(out['reveal']), {'email_a', 'email_b', 'email_capped', 'participant_p'})  # private retains untruncated membership
                self.assertEqual(out['reveal']['email_a']['value'], 'real@example.test')
                self.assertEqual(out['reveal']['email_a']['fields']['metadata']['contact'], 'real@example.test')
                self.assertEqual(out['reveal']['participant_p']['value'], 'Real Person Name')
                self.assertEqual(out['reveal']['participant_p']['fields']['metadata']['contact'], 'real@example.test')
                self.assertEqual(out['reveal']['email_capped']['value'], 'capped@example.test')
                self.assertEqual(out['pages'], {'email_a': '../data/private/node_docs/node_a.html'})

    def test_dated_mentions_label_preserves_type(self):
        from visualize_graph import edge_title
        record = {'relationship_type': 'same_day_presence', 'weight': 2}
        self.assertIn('dated mentions', edge_title(record))
        self.assertIn('not verified presence', edge_title(record))
        self.assertEqual(record['relationship_type'], 'same_day_presence')


class ViewerFoldTests(unittest.TestCase):
    def test_own_website_shows_on_its_organization(self):
        nodes = [{'id': 'o', 'name': 'White & Case LLP', 'entity_type': 'organization'},
                 {'id': 'w', 'name': 'whitecase.com', 'entity_type': 'website'},
                 {'id': 'v', 'name': 'other.com', 'entity_type': 'website'}, {'id': 'p', 'name': 'P', 'entity_type': 'person'}]
        edge = lambda i, s, t, kind: {'id': i, 'source_entity_id': s, 'target_entity_id': t, 'relationship_type': kind}
        edges = [edge(1, 'w', 'o', 'website_of'), edge(2, 'v', 'o', 'website_of'), edge(3, 'v', 'p', 'co_mentioned_in_documents')]
        entities, relationships = visualize_graph.fold_own_websites(nodes, edges)
        self.assertEqual({e['id'] for e in entities}, {'o', 'v', 'p'})
        self.assertEqual(next(e for e in entities if e['id'] == 'o')['websites'], ['whitecase.com'])
        self.assertEqual([r['id'] for r in relationships], [2, 3])


class ViewerPrivacyTests(unittest.TestCase):
    def test_pseudonymous_node_does_not_carry_merged_real_spellings(self):
        merged = [{'id': 'entity_1', 'value': 'Real Person', 'rule': 'company_clean_or_exact', 'confidence': 'exact'}]
        hidden = visualize_graph.slim_node({'id': 'p', 'name': '84808c16a253e811ec1e83a3', 'merged_identities': merged})
        self.assertEqual(hidden['merged_identities'], [{'id': 'entity_1', 'rule': 'company_clean_or_exact', 'confidence': 'exact'}])
        self.assertNotIn('Real Person', json.dumps(visualize_graph.slim_node(
            {'id': 'c', 'name': 'Email_0e5cc7a0217101f36355c3f7', 'merged_identities': merged})))
        shown = visualize_graph.slim_node({'id': 'o', 'name': 'Zeff Design', 'merged_identities': merged})
        self.assertEqual(shown['merged_identities'][0]['value'], 'Real Person')

    def test_accusatory_role_needs_a_reported_conviction(self):
        node = lambda role, flags: {'id': 1, 'name': 'A Person', 'legal_status_flags': flags,
                                    'metadata': json.dumps({'person_type': role, 'occupation': 'X'})}
        self.assertNotIn('person_type', json.loads(visualize_graph.slim_node(node('enabler', []))['metadata']))
        self.assertEqual(json.loads(visualize_graph.slim_node(node('enabler', []))['metadata'])['occupation'], 'X')
        nested = {**node('enabler', []), 'original_records': [{'id': 1, 'metadata': json.dumps({'person_type': 'enabler', 'occupation': 'X'})}]}
        self.assertNotIn('person_type', json.loads(visualize_graph.slim_node(nested)['original_records'][0]['metadata']))
        self.assertNotIn('person_type', json.loads(visualize_graph.slim_node(node('perpetrator', ['deceased']))['metadata']))
        self.assertEqual(json.loads(visualize_graph.slim_node(node('perpetrator', ['convicted']))['metadata'])['person_type'], 'perpetrator')
        self.assertEqual(json.loads(visualize_graph.slim_node(node('associate', []))['metadata'])['person_type'], 'associate')


class ViewerSliceTests(unittest.TestCase):
    def test_entity_layer_nodes_need_an_exported_link(self):
        from visualize_graph import viewer_slice, ENTITY_LAYER_SOURCE
        entities = [{'id': 1}, {'id': 'company_x', 'relevance': 'incidental'},
                    {'id': 'entity_a', 'source': ENTITY_LAYER_SOURCE}, {'id': 'entity_b', 'source': ENTITY_LAYER_SOURCE}]
        relationships = [{'source_entity_id': 1, 'target_entity_id': 'entity_a'}]
        self.assertEqual([e['id'] for e in viewer_slice(entities, relationships)], [1, 'entity_a'])

    def test_company_nodes_require_any_exported_link_regardless_of_relevance(self):
        from visualize_graph import viewer_slice
        source = 'full-text legal name (scripts/extract_companies.py)'
        entities = [{'id': 1}, {'id': 'company_linked', 'source': source, 'relevance': 'incidental'},
                    {'id': 'company_shell', 'entity_type': 'shell_company'},
                    {'id': 'company_isolated', 'source': source, 'relevance': 'network'},
                    {'id': 2, 'entity_type': 'organization'}]
        relationships = [{'source_entity_id': 1, 'target_entity_id': 'company_linked'},
                         {'source_entity_id': 1, 'target_entity_id': 'company_shell'}]
        self.assertEqual([e['id'] for e in viewer_slice(entities, relationships)],
                         [1, 'company_linked', 'company_shell', 2])
        self.assertEqual(len(entities), 5)  # viewer slicing cannot remove full-graph nodes


if __name__ == '__main__':
    unittest.main()
