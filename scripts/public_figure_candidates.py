"""Review input for public_names.py: every person/participant in the viewer whose
real name matches a Wikidata person (label or alias; data/external/wikidata,
downloaded from the QLever Wikidata endpoint with a query that sends no names),
or who writes from a government domain, with the context needed to tell a
namesake from the same person (email domains, linked names, Wikidata
descriptions). Output: data/private/audit/public_figure_context.json, then
reviewed into data/private/public_figures.json.
    .venv/bin/python scripts/public_figure_candidates.py   (~4 GB RAM)
"""
import json, gzip, re, unicodedata, sys, base64
from collections import Counter
from collections import defaultdict,Counter
def norm(s):
    s=unicodedata.normalize('NFKD',str(s)); s=''.join(c for c in s if not unicodedata.combining(c)).casefold()
    s=re.sub(r"\b(mr|mrs|ms|dr|prof|sir|hon|jr|sr|ii|iii|iv)\b\.?",' ',s)
    return ' '.join(re.findall(r"[a-z]+(?:['-][a-z]+)*",s))
def nomid(n):
    t=n.split(); return ' '.join([t[0],t[-1]]) if len(t)>2 else n
W=defaultdict(list); info={}
rd=lambda f:(l.rstrip('\n').split('\t') for l in gzip.open(f,'rt'))
val=lambda x:re.sub(r'^"(.*)"(@en)?$',r'\1',x)
import glob, itertools
# Labels come in sitelink ranges (part_*.tsv.gz): one query for all 4.5M people times out on the server.
it=itertools.chain.from_iterable(itertools.islice(rd(f), 1, None) for f in sorted(glob.glob('data/external/wikidata/part_*.tsv.gz')))
for r in it:
    if len(r)<3 or not r[0].startswith('<http'): continue
    q=r[0].rsplit("/",1)[-1].rstrip(">"); info[q]=(val(r[1]),int(r[2] or 0),val(r[3]) if len(r)>3 else '')
    W[norm(info[q][0])].append(q)
it=rd('data/external/wikidata/humans_B.tsv.gz'); next(it)
for r in it:
    if len(r)<2 or not r[0].startswith('<http'): continue
    q=r[0].rsplit('/',1)[-1].rstrip('>'); W[norm(val(r[1]))].append(q)
M=defaultdict(set)
for k,qs in W.items():
    for q in qs: M[nomid(k)].add(q)
vn={x['id']:x for x in json.load(open('data/private/audit/viewer_nodes.json'))}
idm=json.load(open('output/identity_map.json'))
members=defaultdict(list)
for m,it in idm.items(): members[str(it['canonical'])].append(m)
look=json.load(open('data/private/contact_lookup.json'))
html=open('output/graph.html').read()
main=json.loads(gzip.decompress(base64.b64decode(re.search(r'id="viewer-data">([^<]+)',html)[1])))
nb=defaultdict(Counter)
for e in main['edges']:
    nb[e['from']][e['to']]+=1; nb[e['to']][e['from']]+=1
OFFICIAL=re.compile(r'(\.gov|\.mil|\.gouv\.fr|\.gov\.[a-z]{2}|\.gc\.ca|parliament\.uk|europa\.eu|un\.org|senate\.gov|house\.gov)$')
out=[]
for x in vn.values():
    if x['type'] not in('person','participant'): continue
    name=x['real'] or x['label']; n=norm(name); t=n.split()
    domains=Counter()
    for m in members.get(str(x['id']),[])+[str(x['id'])]:
        v=look.get(m,{}).get('value','') if m.startswith('email_') else ''
        if '@' in v: domains[v.split('@')[1].casefold()]+=1
    official=[d for d in domains if OFFICIAL.search(d)]
    qs=set(W.get(n,[])) or (M.get(nomid(n),set()) if len(t)>=2 else set())
    if len(t)<2 and not official: continue
    if not qs and not official: continue
    if any(len(w)==1 for w in t[:1]+t[-1:]) and not official: continue   # "Paul V", "John C"
    cands=sorted(qs,key=lambda q:-info.get(q,('',0,''))[1])[:6]
    neigh=[(vn[o]['real'] or vn[o]['label']) for o,_ in nb[x['id']].most_common(8) if o in vn]
    out.append(dict(id=x['id'],name=name,type=x['type'],links=x['deg'],records=x['rec'],email_domains=[d for d,_ in domains.most_common(5)],
                    official_domains=official,neighbors=neigh,
                    wikidata=[dict(qid=q,label=info[q][0],desc=info[q][2],sitelinks=info[q][1]) for q in cands if q in info]))
print(len(out),Counter(bool(o['wikidata']) for o in out),sum(bool(o['official_domains']) for o in out))
json.dump(out,open('data/private/audit/public_figure_context.json','w'),ensure_ascii=False)
