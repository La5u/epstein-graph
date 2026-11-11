import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from private_build import release


class PrivateBuildTests(unittest.TestCase):
    def test_release_keeps_raw_caches_logs_and_replaced_sqlite_bytes_private(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);work=root/'work';work.mkdir();private=root/'private';private.mkdir()
            raw='private.person@gmail.com'
            (work/'.build').mkdir();(work/'.build/raw.json').write_text(raw)
            (work/'debug.log').write_text(raw)
            (work/'old.partial.db').write_text(raw)
            (work/'graph.html').write_text('<script>const PUBLIC_REVEAL = {}; const SECRET = null;</script>')
            (work/'nodes.json').write_text(json.dumps({'label':raw,'tooltip':'Phone (212) 750-2408','x':600.5671875}))
            (work/'neo4j').mkdir();(work/'neo4j/nodes.csv').write_text('id,label\nid,'+raw+'\n')
            with sqlite3.connect(work/'nodes.db') as db:
                db.execute('CREATE TABLE entities(id PRIMARY KEY,name,metadata)')
                db.execute('INSERT INTO entities VALUES (?,?,?)',('id',raw,json.dumps({'note':raw})))
            db.close()
            release(work,root/'output',private)
            self.assertFalse((root/'output/.build').exists())
            self.assertFalse((root/'output/debug.log').exists())
            self.assertFalse((root/'output/old.partial.db').exists())
            for path in (root/'output').rglob('*'):
                if path.is_file(): self.assertNotIn(raw.encode(),path.read_bytes())
            self.assertEqual((root/'output/neo4j').stat().st_mode&0o777,0o755)
            self.assertIn('email:',(root/'output/nodes.json').read_text())
            self.assertEqual(json.loads((root/'output/nodes.json').read_text())['x'],600.5671875)
            with sqlite3.connect(work/'nodes.db') as db:
                self.assertEqual(db.execute('SELECT name FROM entities').fetchone()[0],raw)
            db.close()
            self.assertEqual(json.loads((private/'gdpr_review/leak_audit.json').read_text())['leaks'],{})


if __name__=='__main__': unittest.main()
