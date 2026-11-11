import json
import tempfile
import unittest
from pathlib import Path

from report_header_merges import grouped, object_items
from report_header_followup3 import compare


class HeaderMergeReportTests(unittest.TestCase):
    def test_evaluation_counts_group_progress_and_reviewed_vs_unflagged_nodes(self):
        snapshot = [dict(id=i, real=label, label=label, type='participant') for i, label in
                    [('a', 'Zelda'), ('b', 'Zelda Hargrave'), ('c', 'Zelda Other'), ('d', 'Ramon')]]
        findings = [dict(ids=['a', 'b', 'c'], good=['a', 'b', 'c'], labels=['Zelda', 'Zelda Hargrave', 'Zelda Other'],
                         **{'class': 'duplicate_name'})]
        before = {'resolved_map': {}}
        after = {'resolved_map': {'a': dict(target='b', rule='header_context:document'),
                                  'd': dict(target='b', rule='header_context:mailbox')},
                 'label_repairs': {}, 'compounds': []}
        report = compare(snapshot, findings, [findings[0]['labels']], before, after)
        self.assertEqual(report['summary']['recall'], {'partial': 1})
        self.assertEqual(report['rules']['header_context:document']['flagged'], 1)
        self.assertEqual(report['rules']['header_context:mailbox']['unflagged'], 1)
        self.assertEqual(report['reviewer_classes']['duplicate_name']['untouched'], 2)

    def test_contacts_stream_across_chunk_and_unicode_boundaries(self):
        values = {'email_a': {'kind': 'email', 'value': 'a@example.test', 'raw_spellings': ['x' * 100]},
                  'name_b': {'kind': 'participant', 'value': 'André {Roe}'}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'contacts.json'
            for ascii_only in (False, True):
                path.write_text(json.dumps(values, ensure_ascii=ascii_only, indent=1))
                for chunk in (1, 7, 65536):
                    self.assertEqual(dict(object_items(path, chunk)), values)
            path.write_text('{}')
            self.assertEqual(list(object_items(path, 1)), [])

    def test_groups_are_separate_by_reason_and_target(self):
        rows = [dict(member='a', target='z', reason='word_order'),
                dict(member='b', target='z', reason='word_order'),
                dict(member='c', target='z', reason='middle_name')]
        groups = grouped(rows)
        self.assertEqual([len(g['members']) for g in groups], [1, 2])
