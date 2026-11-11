"""Headless Chromium smoke test of the viewer; never uses or changes your browser profile.

Install requirements-dev.txt; requires a system Chromium executable. Runs
headless and requires a real GPU; software rendering is rejected.
"""
import json
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
GPU = ['--enable-gpu', '--use-angle=gl-egl', '--ignore-gpu-blocklist']
READY = "document.getElementById('status').textContent.includes('layout frozen') && !state.spreading"
MEASURE = '''()=>{
  const visible=data.nodes.filter(n=>!n.hidden);
  let overlaps=0,maxOverlap=0;
  for(let i=0;i<visible.length;i++)for(let j=i+1;j<visible.length;j++){
    const a=visible[i],b=visible[j],depth=a.size+b.size-Math.hypot(a.x-b.x,a.y-b.y);
    if(depth>0){overlaps++;maxOverlap=Math.max(maxOverlap,depth);}
  }
  return {visible:visible.length,overlaps,maxOverlap};
}'''
ISOLATION = '''()=>{
  const nodes=data.nodes.filter(n=>!n.hidden&&!n.ghost);
  const counts=connectionCounts(nodes,data.edges.filter(e=>state.visibleLinks.has(e.id)));
  return {visible:nodes.length,isolates:nodes.filter(n=>!counts.get(n.id)).map(n=>({id:n.id,reason:n.record.document_isolation_reason}))};
}'''
# Known gaps in the source evidence; new isolated dots must fail the smoke.
ISOLATION_EXCEPTIONS = {364, 505, 'email_1d4101ff1a1e3490770efd00',
                        'email_e576eb0214e8cd5084534c77', 'place_sanela-jenkins-apartment'}


def launch(p):
    executable = shutil.which('chromium-browser') or shutil.which('chromium')
    if not executable:
        raise RuntimeError('System Chromium not found; install it or configure executable_path')
    return p.chromium.launch(executable_path=executable, headless=True, args=GPU,
                             ignore_default_args=['--enable-unsafe-swiftshader'])


def gather_smoke():
    """Real pointer double-click on a small fixture built from the current viewer sources."""
    from visualize_graph import ASSETS, COLORS, render, lazy_payload

    fixture = {'nodes': [dict(id=i, label=f'Node {i}', x=x, y=y, record=dict(id=i, name=f'Node {i}', entity_type='person'))
                         for i, x, y in [(1, 0, 0), (2, 500, 0), (3, -500, 0), (4, 0, 400)]]
                        + [dict(id='email_x', label='Email_x', x=0, y=-400, record=dict(id='email_x', name='Email_x', entity_type='email')),
                           dict(id='email_private', label='Email_private', x=600, y=-400, record=dict(id='email_private', name='Email_private', entity_type='email'))],
               'edges': [dict(id=i, **{'from': 1, 'to': i + 1}, record=dict(relationship_type='associated_with', weight=1)) for i in [1, 2]]}
    payload, chunks = lazy_payload(fixture['nodes'], fixture['edges'], {'templates': [], 'documents': {}}, [], {})
    html = render((ASSETS / 'viewer.html').read_text(), {
        'RENDERER_JS': (ASSETS / 'renderer.js').read_text(), 'LOGIC_JS': (ASSETS / 'graph_logic.js').read_text(),
        'APP_JS': (ASSETS / 'app.js').read_text(), 'PAYLOAD': payload, 'DETAIL_CHUNKS': chunks, 'COLORS_JSON': json.dumps(COLORS), 'SECRET_JSON': 'null',
        'PUBLIC_REVEAL_JSON': json.dumps({'email_x': 'info@example.test', 'email_private': 'email:123456abcdef'})})
    with sync_playwright() as p:
        browser = launch(p)
        page = browser.new_page(viewport={'width': 1600, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content(html)
        page.wait_for_function(READY)
        page.wait_for_timeout(300)
        before = page.evaluate('data.nodes.map(n=>[n.x,n.y])')
        point = page.evaluate('''()=>{const p=renderer.screenPosition(1),r=document.getElementById('graph').getBoundingClientRect();
          return {x:p.x+r.left,y:p.y+r.top};}''')
        page.mouse.dblclick(point['x'], point['y'], delay=60)
        page.wait_for_function("document.getElementById('status').textContent.includes('Gathered 2')")
        after = page.evaluate('data.nodes.map(n=>[n.x,n.y])')
        assert after[0] == before[0] and after[3] == before[3], 'centre or unrelated node moved'
        assert after[1] != before[1] and after[2] != before[2], 'neighbours not gathered'
        assert page.evaluate("nodeById.get('email_x').label") == 'info@example.test', 'organisational email not shown'
        assert page.evaluate("nodeById.get('email_private').label") == 'email:123456abcdef', 'private email hash not shown'
        page.evaluate("applyReveal({email_private:{value:'private@example.test'}})")
        assert page.evaluate("nodeById.get('email_private').label") == 'private@example.test', 'unlock did not replace the hash'
        assert page.evaluate('!renderer.layoutRunning')
        assert page.evaluate("document.getElementById('link-mode').value") == 'focus', 'double-click did not switch to focus links'
        corner = page.evaluate('''()=>{const r=document.getElementById('graph').getBoundingClientRect();
          return {x:r.right-15,y:r.bottom-15};}''')
        assert page.evaluate(f"renderer.nodeAt({corner['x']}-document.getElementById('graph').getBoundingClientRect().left,"
                             f"{corner['y']}-document.getElementById('graph').getBoundingClientRect().top)") is None
        page.mouse.dblclick(corner['x'], corner['y'], delay=60)  # empty corner of the canvas
        page.wait_for_function("document.getElementById('link-mode').value === ''")
        assert not errors, errors
        browser.close()
    print('Gather smoke passed (real double-click, centre and unrelated nodes fixed, layout frozen, link mode toggled).')


def main():
    with sync_playwright() as p:
        browser = launch(p)
        page = browser.new_page(viewport={'width': 1600, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto((ROOT / 'output/graph.html').as_uri())
        page.wait_for_function(READY, timeout=120000)
        layout = page.evaluate(MEASURE)
        gpu = page.evaluate("()=>{const gl=renderer.gl,e=gl.getExtension('WEBGL_debug_renderer_info');return e?gl.getParameter(e.UNMASKED_RENDERER_WEBGL):'';}")
        assert gpu and not any(s in gpu.lower() for s in ('swiftshader','llvmpipe','softpipe','software')), f'Real GPU required: {gpu}'
        assert page.evaluate('detailStore.cache.size === 0 && detailStore.pending.size === 0'), 'Details inflated during startup'
        default_isolation = page.evaluate(ISOLATION)
        # Focus-link mode must follow search, path and clearing.
        focus_links = page.evaluate('''()=>{
          const edge=data.edges.find(e=>state.visibleLinks.has(e.id)&&e.from!==e.to);
          const correct=()=>{const expected=linkSelection(data.edges,state.visibleLinks,'focus',focusIds(),()=>null);
            return data.edges.every(e=>e.ghost||e.hidden===!expected.has(e.id));};  // ghost: faded context of the focus
          const mode=document.getElementById('link-mode');mode.value='focus';mode.onchange();
          focusNode(edge.from);const jump=correct()&&!edge.hidden;
          document.getElementById('search').value=entityChoice(nodeById.get(edge.to));find();
          const search=correct()&&!edge.hidden;
          document.getElementById('path-from').value=entityChoice(nodeById.get(edge.from));
          document.getElementById('path-to').value=entityChoice(nodeById.get(edge.to));findPath();
          const path=correct()&&!edge.hidden;
          document.getElementById('clear').onclick();
          const clear=correct()&&data.edges.every(e=>e.hidden);
          mode.value='';mode.onchange();
          return {jump,search,path,clear};
        }''')
        # Every line and dot should show its documents or records in the details panel.
        documents = page.evaluate('''async ()=>{
          const shown=item=>{const box=document.getElementById('docs');return box.querySelectorAll('a').length>0;};
          const edges=data.edges.filter(e=>e.record.documents).slice(0,200), nodes=data.nodes.filter(n=>n.record.documents).slice(0,200);
          let edgeOk=0,nodeOk=0;
          for(const e of edges){await showEdge(e);if(shown())edgeOk++;}
          for(const n of nodes){await showNode(n);if(shown())nodeOk++;}
          clearDetails();
          return {edges_with_index:data.edges.filter(e=>e.record.documents).length, edges:data.edges.length,
                  nodes_with_index:data.nodes.filter(n=>n.record.documents).length, nodes:data.nodes.length,
                  edge_panels_with_links:edgeOk+'/'+edges.length, node_panels_with_links:nodeOk+'/'+nodes.length,
                  ok:edgeOk===edges.length};
        }''')
        # Expand pulls a node's hidden partners and merged addresses into the view; Collapse removes them again.
        expand = page.evaluate('''async ()=>{
          let n;
          for(const candidate of data.nodes.filter(n=>!n.hidden&&n.record.expand_total>=3)){
            const r=await fullRecord(candidate);
            if((r.expand||[]).length>=3&&(r.merged_identities||[]).some(m=>m.records>0)){n=candidate;break;}
          }
          if(!n) return {found:false};
          const before={nodes:data.nodes.length,edges:data.edges.length};
          await expandNode(n.id);await showNode(nodeById.get(n.id));
          const added=expansions.get(n.id), fresh=added.nodes.filter(id=>id.startsWith?.('alias:')||nodeById.get(id).record.expanded_from);
          const shown=fresh.filter(id=>{const m=nodeById.get(id);return !m.hidden||NON_NAME_QUALITY.has(m.record.label_quality)||!typeShown(m.record,state.hiddenTypes);}).length;  // Hide non-names and type filters (participants until unlock) still apply to pulled-in partners
          const button=document.getElementById('neighbors').querySelector('button').textContent;
          collapseNode(n.id);
          return {found:true,name:n.record.name,added:fresh.length,shown,button,
                  restored:data.nodes.length===before.nodes&&data.edges.length===before.edges&&!expansions.size};
        }''')
        page.locator('#advanced > summary').click()
        page.locator('[data-status="deceased"]').check()
        deceased = page.evaluate("()=>{const p=data.nodes.filter(n=>(n.record.legal_status_flags||[]).includes('deceased'));return {count:p.length,all_hidden:p.every(n=>n.hidden)};}")
        page.locator('[data-hide="isolates"]').check()
        isolates_hidden = page.evaluate('data.nodes.every(n=>n.hidden||n.count>0)')
        page.locator('#layout-toggle').click()
        page.wait_for_timeout(1000)
        moving = page.evaluate('renderer.layoutRunning')
        page.locator('#layout-toggle').click()
        frozen = page.evaluate('!renderer.layoutRunning')
        lazy = page.evaluate('''async ()=>{
          const first=data.nodes[0],second=data.nodes[1];
          const old=showNode(first),current=showNode(second);await Promise.all([old,current]);
          const latest=state.detail.node===second.id&&document.getElementById('detail-title').textContent===second.record.name;
          const pending=showNode(first);clearDetails();await pending;
          return {latest,closed:document.getElementById('details').hidden,cache:detailStore.cache.size,
            bytes:detailStore.bytes,all_skeletons:data.nodes.every(n=>!n.record.expand&&!n.record.merged_identities)
              &&data.edges.every(e=>!e.record.documents?.docs)};
        }''')
        page.screenshot(path=str(ROOT / 'output/graph_smoke.png'))
        # Check the actual password unlock, after the public screenshot and all
        # tests that report labels. Isolation reports contain IDs only.
        page.evaluate("()=>{state.hiddenStatuses.clear();state.hide={artifacts:true,linkedCoMentions:true,incidental:true};state.hiddenTypes=defaultHidden();state.relationship='';state.linkMode='';update();}")
        password = ROOT / 'data/private/viewer_password'
        unlocked_isolation = None
        if password.exists():
            page.evaluate('async password=>applySecret(await decryptSecret(password))', password.read_text().strip())
            unlocked_isolation = page.evaluate(ISOLATION)
        result = {'javascript_errors': errors, 'gpu': gpu, 'settled_frozen_layout': layout,
                  'deceased_filter': deceased, 'expand': expand, 'documents': documents, 'isolates_hidden': isolates_hidden, 'focus_links': focus_links,
                  'layout_toggle_works': moving and frozen, 'lazy_details': lazy, 'used_isolated_headless_browser': True,
                  'document_isolation': {'default': default_isolation, 'unlocked': unlocked_isolation},
                  'warning': 'Tests dot overlap at open for this viewport/data snapshot, not a permanent guarantee for every zoom, drag or filter state.'}
        (ROOT / 'output/browser_smoke.json').write_text(json.dumps(result, indent=2) + '\n')
        browser.close()
    assert not errors, 'Browser JavaScript errors'
    assert layout['overlaps'] == 0
    assert deceased['all_hidden'] and isolates_hidden and moving and frozen
    assert all(focus_links.values()), 'Focus links out of sync'
    assert lazy['latest'] and lazy['closed'] and lazy['all_skeletons'] and lazy['cache'] <= 4 and lazy['bytes'] <= 4 * 1024 * 1024, lazy
    assert documents['ok'], 'Lines without document links in the details panel'
    for snapshot in (default_isolation, unlocked_isolation):
        if snapshot is not None:
            # Known source gaps, plus a few whose only document partners are hidden by default.
            other = [n for n in snapshot['isolates'] if n['id'] not in ISOLATION_EXCEPTIONS]
            assert all(n.get('reason') for n in snapshot['isolates']) and len(other) <= 15 and all(
                n['reason'] == 'document partners are hidden by this view' for n in other), snapshot
    assert expand['found'] and expand['added'] and expand['shown'] == expand['added'] and expand['restored'], f'Expand failed: {expand}'
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    import sys
    if '--compact-only' in sys.argv or '--gather-only' in sys.argv:
        gather_smoke()
    else:
        main()
