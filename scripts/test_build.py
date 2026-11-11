"""Incremental build: the tracer sees file access, stamps detect real changes only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build

SCRIPTS = Path(__file__).resolve().parent


class TraceTests(unittest.TestCase):
    def test_records_reads_writes_probes_and_listings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'in.txt').write_text('input')
            (root / 'data').mkdir()
            (root / 'ro.db').write_bytes(b'')  # read-only SQLite needs an existing file
            script = root / 'step.py'
            script.write_text(
                'import sqlite3, sys\nfrom contextlib import closing\nfrom pathlib import Path\n'
                'root = Path(sys.argv[1])\n'
                '(root / "out.partial").write_text((root / "in.txt").read_text())\n'
                '(root / "out.partial").replace(root / "out.txt")\n'
                '(root / "maybe.txt").exists()\n'
                'list((root / "data").iterdir())\n'
                'closing(sqlite3.connect(f"file:{root}/ro.db?mode=ro", uri=True))\n'
                'sys.exit(3)\n')
            trace = root / 'trace.json'
            done = subprocess.run([sys.executable, '-B', str(SCRIPTS / '_trace.py'), str(trace), str(script), str(root)])
            self.assertEqual(done.returncode, 3)  # exit status passes through
            found = json.loads(trace.read_text())
            self.assertIn(str(root / 'in.txt'), found['reads'])
            self.assertIn(str(script), found['reads'])
            self.assertIn(str(root / 'ro.db'), found['reads'])
            self.assertIn(str(root / 'out.txt'), found['writes'])
            self.assertIn(str(root / 'maybe.txt'), found['probed'])
            self.assertIn(str(root / 'data'), found['listed'])


class StampTests(unittest.TestCase):
    def setUp(self):
        self.fresh()

    def fresh(self):
        """A built step: one input, one output, a listed dir and a probed path."""
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        patcher = patch.object(build, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.directory.cleanup)
        for name, text in (('in.txt', 'a'), ('out.txt', 'b'), ('requirements.txt', 'x')):
            (self.root / name).write_text(text)
        (self.root / 'listed').mkdir()
        self.step = build.Step('demo', ['demo.py'], None)
        self.fp = build.Fingerprints({})
        trace = {'reads': [str(self.root / 'in.txt'), '/usr/lib/outside.py'],
                 'writes': [str(self.root / 'out.txt'), str(self.root / 'gone.partial')],
                 'listed': [str(self.root / 'listed')], 'probed': [str(self.root / 'later.txt')]}
        self.stamp = build.make_stamp(self.step, trace, self.fp)

    def reason(self):
        return build.stale_reason(self.step, self.stamp, build.Fingerprints({}))

    def test_imported_module_bytecode_maps_to_its_source(self):
        # Imports read scripts/__pycache__/x.cpython-NN.pyc, stale under python -B: depend on x.py.
        self.assertEqual(build.relative(str(build.ROOT / 'scripts/__pycache__/build_contacts.cpython-314.pyc')), 'scripts/build_contacts.py')
        self.assertEqual(build.relative(str(build.ROOT / 'scripts/build_contacts.py')), 'scripts/build_contacts.py')

    def test_fresh_stamp_holds_and_ignores_outside_and_temporary_files(self):
        self.assertEqual(set(self.stamp['inputs']), {'in.txt'})
        self.assertEqual(set(self.stamp['outputs']), {'out.txt'})
        self.assertIsNone(self.reason())

    def test_touch_without_change_is_not_stale(self):
        os.utime(self.root / 'in.txt', ns=(1, 1))
        self.assertIsNone(self.reason())

    def test_large_derived_files_use_content_but_large_raw_sources_use_stat(self):
        with patch.object(build, 'HASH_LIMIT', 0):
            digest = self.fp.file('out.txt')
            self.assertFalse(digest.startswith('stat:'))
            os.utime(self.root / 'out.txt', ns=(1, 1))
            self.assertEqual(self.fp.file('out.txt'), digest)
            (self.root / 'data/raw').mkdir(parents=True)
            (self.root / 'data/raw/corpus.db').write_text('raw')
            self.assertTrue(self.fp.file('data/raw/corpus.db').startswith('stat:'))

    def test_legacy_stat_upgrades_without_forcing_unchanged_derived_outputs(self):
        st = (self.root / 'out.txt').stat()
        self.stamp['outputs']['out.txt'] = f'stat:{st.st_size}:{st.st_mtime_ns}'
        self.assertIsNone(self.reason())
        self.assertFalse(self.stamp['outputs']['out.txt'].startswith('stat:'))
        os.utime(self.root / 'out.txt', ns=(1, 1))
        self.assertIsNone(self.reason())
        (self.root / 'out.txt').write_text('different')
        self.assertEqual(self.reason(), 'output missing or modified: out.txt')

    def test_each_kind_of_change_is_stale(self):
        changes = [
            (lambda: (self.root / 'in.txt').write_text('changed'), 'input changed: in.txt'),
            (lambda: (self.root / 'out.txt').unlink(), 'output missing or modified: out.txt'),
            (lambda: (self.root / 'listed' / 'new').write_text(''), 'directory contents changed: listed/'),
            (lambda: (self.root / 'later.txt').write_text(''), 'path appeared: later.txt'),
            (lambda: (self.root / 'requirements.txt').write_text('y'), 'python or requirements changed'),
        ]
        for change, expected in changes:
            with self.subTest(expected):
                self.fresh()
                change()
                self.assertEqual(self.reason(), expected)

    def test_command_change_is_stale(self):
        self.step.argv = ['demo.py', '--view-edges', '9']
        self.assertEqual(self.reason(), 'command changed')



class PeakTests(unittest.TestCase):
    def test_resources_leave_two_cpus_and_inherit_lower_priority(self):
        with patch.object(build.os, 'sched_getaffinity', return_value={2, 3, 4, 5, 6, 7, 8, 9}), \
                patch.object(build.os, 'sched_setaffinity') as affinity, \
                patch.object(build.os, 'getpriority', return_value=0), patch.object(build.os, 'nice') as nice:
            build.configure_resources()
        affinity.assert_called_once_with(0, [2, 3, 4, 5, 6, 7])
        nice.assert_called_once_with(10)

    def test_resources_keep_one_cpu_and_existing_lower_priority(self):
        with patch.object(build.os, 'sched_getaffinity', return_value={5}), \
                patch.object(build.os, 'sched_setaffinity') as affinity, \
                patch.object(build.os, 'getpriority', return_value=15), patch.object(build.os, 'nice') as nice:
            build.configure_resources()
        affinity.assert_called_once_with(0, [5])
        nice.assert_called_once_with(0)

    def test_peak_sums_the_process_tree(self):
        # Three workers each hold ~80 MB at once; ru_maxrss alone would report one of them.
        code = ('import multiprocessing as mp, time\n'
                'def hold(_):\n    b = bytearray(80 * 2 ** 20); time.sleep(2.5); return len(b)\n'
                'if __name__ == "__main__":\n    mp.set_start_method("fork")\n'
                '    with mp.Pool(3) as p: p.map(hold, range(3))\n')
        returncode, peak = build.wait_peak(subprocess.Popen([sys.executable, '-c', code]), interval=0.2)
        self.assertEqual(returncode, 0)
        self.assertGreater(peak, 200)

if __name__ == '__main__':
    unittest.main()
