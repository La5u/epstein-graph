import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import build
from entity_cache_migration import migrate
from publish_entity_contacts import publish, FILES


class EntityPublicationTests(unittest.TestCase):
    def test_public_labels_keys_and_freed_sqlite_bytes_are_private_safe(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'private/entity_layer';source.mkdir(parents=True)
            raw='private.person©firm.test'
            with sqlite3.connect(source/'entity_graph.db') as db:
                db.executescript('CREATE TABLE entities(id PRIMARY KEY,name,entity_type,metadata);'
                                 'CREATE TABLE key_nodes(key TEXT PRIMARY KEY,entity_id);'
                                 'CREATE TABLE folds(key TEXT PRIMARY KEY,span,entity_id,target_type,reason);')
                for i,name in enumerate([raw,'private.person (a) firm . test']):
                    node={'id':str(i),'name':name,'entity_type':'organization','metadata':{'note':raw}}
                    db.execute('INSERT INTO entities VALUES (?,?,?,?)',(str(i),name,'organization',json.dumps(node)))
                    db.execute('INSERT INTO key_nodes VALUES (?,?)',(name,str(i)))
                    db.execute('INSERT INTO folds VALUES (?,?,?,?,?)',(name,name,str(i),'organization','test'))
            db.close()
            (source/'entity_nodes.json').write_text('[]')
            for name in FILES[2:]: (source/name).write_text('[]')
            result=publish(source,root/'output',root/'private')
            with sqlite3.connect(root/'output/entity_graph.db') as db:
                self.assertEqual(db.execute('SELECT count(*) FROM entities').fetchone()[0],2)
                self.assertEqual(db.execute('SELECT count(DISTINCT key) FROM key_nodes').fetchone()[0],2)
                self.assertEqual({r[0] for r in db.execute('SELECT entity_type FROM entities')},{'email'})
                self.assertTrue(all(r[0].startswith('email:') for r in db.execute('SELECT name FROM entities')))
            db.close()
            for name in FILES:
                data=(root/'output'/name).read_bytes()
                self.assertNotIn(raw.encode(),data)
                self.assertNotIn(raw.encode('unicode_escape'),data)
                self.assertNotIn(json.dumps(raw)[1:-1].encode(),data)
            lookup=json.loads((root/'private/entity_contact_lookup.json').read_text())
            self.assertEqual(lookup['0']['value'],'private.person@firm.test')
            self.assertEqual(lookup['0']['private_fields']['metadata']['note'],raw)
            self.assertEqual((root/'private/entity_contact_lookup.json').stat().st_mode&0o777,0o600)
            with sqlite3.connect(source/'entity_graph.db') as db:
                self.assertEqual(db.execute('SELECT name FROM entities WHERE id="0"').fetchone()[0],raw)
            db.close()

    def test_cache_migration_keeps_all_done_rows_and_rebases_only_valid_stamps(self):
        with tempfile.TemporaryDirectory() as d,patch.object(build,'ROOT',Path(d)):
            root=Path(d); (root/'output/.build').mkdir(parents=True)
            with sqlite3.connect(root/'output/.build/entity_mentions.db') as db:
                db.execute('CREATE TABLE done(page_id PRIMARY KEY)');db.execute('INSERT INTO done VALUES (123)')
            db.close()
            (root/'input').write_text('same extraction')
            for name in FILES: (root/'output'/name).write_text('original '+name)
            fp=build.Fingerprints({})
            stamp={'command':['scripts/build_entities.py'],'toolchain':build.toolchain(fp),
                   'inputs':{'input':fp.file('input')},'outputs':{'output/'+n:fp.file('output/'+n) for n in FILES},
                   'listed':{},'probed':{}}
            state={'steps':{'build_entities':stamp}}
            self.assertTrue(migrate(root,state,fp,build.stale_reason,build.Step))
            self.assertTrue(all((root/'data/private/entity_layer'/n).exists() for n in FILES))
            self.assertIn('data/private/entity_mentions.db',stamp['inputs'])
            with sqlite3.connect(root/'data/private/entity_mentions.db') as db:
                self.assertEqual(db.execute('SELECT page_id FROM done').fetchall(),[(123,)])
            db.close()
            self.assertFalse(migrate(root,state,fp,build.stale_reason,build.Step))


if __name__=='__main__': unittest.main()
