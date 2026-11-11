import json
import tempfile
import unittest
from pathlib import Path

import emails as email_source
from adapters import efta_emails


class EmailAdapterTests(unittest.TestCase):
    def test_common_views_over_source_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data/raw').mkdir(parents=True)
            efta_emails.create(root / 'data/raw/email_metadata.db', root / 'data/raw/communications.db',
                               headers=[(1, 'EFTA001', 0, 'A Person', 'a@x.test', json.dumps(['b@x.test']), '[]', '2010-01-01', 0),
                                        (2, 'EFTA002', 1, None, None, '[]', '[]', None, 1)],
                               messages=[(7, 'EFTA001', '2010-01-01', 0)],
                               parties=[(7, 'from', 'A Person', 'a@x.test', 3), (7, 'to', None, 'b@x.test', None)],
                               identities=[(3, 'A Person', 0.95, 'exact')])
            with email_source.use_root(root):
                db = email_source.headers()
                self.assertEqual(db.execute('SELECT id, doc_id, sender_name, to_json, date FROM headers WHERE noise=0').fetchall(),
                                 [(1, 'EFTA001', 'A Person', '["b@x.test"]', '2010-01-01')])
                db.close()
                db = email_source.parties()
                rows = db.execute('''SELECT e.doc_id, p.role, p.address, r.name, r.method FROM parties p
                                     JOIN messages e ON e.id = p.message_id LEFT JOIN identities r ON r.id = p.identity_id''').fetchall()
                db.close()
                self.assertEqual(rows, [('EFTA001', 'from', 'a@x.test', 'A Person', 'exact'), ('EFTA001', 'to', 'b@x.test', None, None)])
                self.assertEqual(list(email_source.external()), [])  # no data/external
                self.assertEqual(email_source.stamp_paths()[0], root / 'data/raw/email_metadata.db')
        with self.assertRaises(Exception):  # read-only: views never write through
            email_source.headers(root / 'missing.db').execute('SELECT 1 FROM headers').fetchall()


if __name__ == '__main__':
    unittest.main()
