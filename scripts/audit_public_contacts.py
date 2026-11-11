"""Read-only contact leak audit, including compressed viewer/search payloads.

Public output contains counts/paths only. Any matched values stay in the
0600 local report under data/private/gdpr_review.
"""
from collections import Counter
import base64
import gzip
import json
from pathlib import Path
import re
import sqlite3

from contact_publication import EMAIL, OCR_EMAIL, PHONE, PHONE_CUE, publication_key, text_scrubber
from viewer_secret import _write_private

ROOT = Path(__file__).resolve().parents[1]
COLUMNS = {'key','value','data','text','content','name','label','labels','span','spelling','metadata','line','target','subpoena_target',
           'role_hint','reason','json','source','ref','url','phrase','rows',
           'email','emails','phone','phones','address','addresses','sender_email','recipient_email'}
TEXT_SUFFIXES = {'.json','.csv','.html','.md','.txt','.jsonl','.ndjson','.graphml','.cypher','.log'}


def viewer_texts(html):
    """Inflate public data/details; SECRET is encrypted and deliberately opaque."""
    for match in re.finditer(r'<script[^>]*id="(?:viewer-data|detail-\d+)"[^>]*>(.*?)</script>',html,re.S):
        yield gzip.decompress(base64.b64decode(match[1])).decode()


def contacts_in_text(text, scrub):
    if text.lstrip().startswith(('{','[')):
        try:
            structured=json.loads(text)
        except (ValueError,RecursionError):
            pass
        else:
            def strings(value):
                if isinstance(value,str):
                    yield value
                elif isinstance(value,list):
                    for item in value: yield from strings(item)
                elif isinstance(value,dict):
                    for key,item in value.items():
                        yield key
                        if isinstance(item,(str,int)) and str(item).isdigit() and scrub({key:item})!={key:item}:
                            yield json.dumps({key:item})
                        else:
                            yield from strings(item)
            seen=set()
            for value in strings(structured):
                # The numeric phone-field case is already established by
                # the structured scrub; do not parse the same object again.
                if value.startswith('{') and scrub(value)!=value:
                    if value not in seen:
                        seen.add(value);yield value
                else:
                    for contact in contacts_in_text(value,scrub):
                        if contact not in seen:
                            seen.add(contact);yield contact
            return
    text=re.sub(r'\\+u00(a9|ae|40)',lambda m:{'a9':'©','ae':'®','40':'@'}[m[1].lower()],text,flags=re.I)
    patterns = ([EMAIL] if '@' in text else []) + ([OCR_EMAIL] if any(m in text for m in ('@','©','®','(a)','(A)')) else []) + ([PHONE] if '+' in text or re.search(r'\d[\s()./-]\d',text) else [])
    if re.search(r'tel|phone|fax|mobile|switchboard|cell',text,re.I): patterns.append(PHONE_CUE)
    seen = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            value = match.group()
            if value not in seen and scrub(value) != value:
                seen.add(value)
                yield value


def audit(out, private):
    html = (out/'graph.html').read_text()
    match = re.search(r'const PUBLIC_REVEAL = (\{.*?\});',html,re.S)
    public = json.loads(match[1]) if match else {}
    scrub = text_scrubber(public, key=publication_key(private))
    counts, examples = Counter(), {}
    checked = Counter()
    def check(name,text):
        checked['text_chunks'] += 1
        leaks = set(contacts_in_text(text,scrub))
        if leaks:
            counts[name] += len(leaks)
            examples.setdefault(name,set()).update(leaks)
    print('scan graph.html decoded payloads',flush=True)
    for text in viewer_texts(html):
        check('graph.html decoded public payload',text)
    source=re.sub(r'(<script[^>]*id="(?:viewer-data|detail-\d+)"[^>]*>).*?(</script>)',r'\1\2',html,flags=re.S)
    source=re.sub(r'const SECRET = (?:\{.*?\}|null);', 'const SECRET = null;',source,flags=re.S)
    check('graph.html source',source)
    for path in sorted(out.rglob('*')):
        if not path.is_file():
            continue
        name = path.relative_to(out).as_posix()
        check('artifact paths', name)
        if path.name=='graph.html':
            continue
        if path.suffix=='.json':
            print('scan '+name,flush=True)
            checked['text_files'] += 1
            check(name,path.read_text())
        elif path.suffix in TEXT_SUFFIXES:
            print('scan '+name,flush=True)
            checked['text_files'] += 1
            with path.open(errors='replace') as stream:
                tail = ''
                while block := stream.read(1024*1024):
                    text = tail+block
                    check(name,text)
                    tail = text[-256:]
        elif path.suffix in {'.db','.sqlite','.sqlite3'}:
            print('scan '+name,flush=True)
            checked['databases'] += 1
            db = sqlite3.connect(f'file:{path}?mode=ro',uri=True)
            try:
                tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
                for table, in tables:
                    quote = lambda x: '"'+x.replace('"','""')+'"'
                    columns = [r[1] for r in db.execute('PRAGMA table_info('+quote(table)+')') if r[1] in COLUMNS]
                    if not columns:
                        continue
                    print('scan '+name+':'+table,flush=True)
                    for row in db.execute('SELECT '+','.join(map(quote,columns))+' FROM '+quote(table)):
                        for value in row:
                            if isinstance(value,str) and (any(m in value for m in ('@','©','®','(a)','(A)')) or '+' in value or re.search(r'\d[\s()./-]\d|tel|phone|fax|mobile|switchboard|cell',value,re.I)):
                                check(name+':'+table,value)
            finally:
                db.close()
    result = {'checked':dict(checked),'leaks':dict(counts),
              'examples':{k:sorted(v)[:30] for k,v in examples.items()}}
    _write_private(private/'gdpr_review/leak_audit.json',json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


def main():
    result = audit(ROOT/'output', ROOT/'data/private')
    print(json.dumps({k:v for k,v in result.items() if k!='examples'},indent=2))
    raise SystemExit(bool(result['leaks']))


if __name__=='__main__': main()
