import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from derived_files import metadata_matches, remember_metadata, replace_if_changed


class DerivedFileTests(unittest.TestCase):
    def test_validation_metadata_rejects_changed_artifacts_inputs_and_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.build/validation.json'
            self.assertFalse(metadata_matches(path, 'stamp', 'private content'))
            remember_metadata(path, 'stamp', 'private content')
            self.assertTrue(metadata_matches(path, 'stamp', 'private content'))
            self.assertFalse(metadata_matches(path, 'new input', 'private content'))
            self.assertFalse(metadata_matches(path, 'stamp', 'changed content'))
            self.assertNotIn('private content', path.read_text())
            path.write_text('invalid JSON')
            self.assertFalse(metadata_matches(path, 'stamp', 'private content'))

    def test_identical_rebuild_preserves_inode_and_times(self):
        with tempfile.TemporaryDirectory() as directory:
            target, partial = (Path(directory) / name for name in ('final', 'partial'))
            target.write_bytes(b'x' * (2 ** 20 + 1))
            partial.write_bytes(target.read_bytes())
            before = target.stat()
            self.assertFalse(replace_if_changed(partial, target))
            after = target.stat()
            self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
            self.assertFalse(partial.exists())
            partial.write_bytes(b'x' * 2 ** 20 + b'y')
            self.assertTrue(replace_if_changed(partial, target))
            self.assertEqual(target.read_bytes()[-1:], b'y')

    def test_missing_target_and_same_stat_replacements(self):
        with tempfile.TemporaryDirectory() as directory:
            target, partial = (Path(directory) / name for name in ('final', 'partial'))
            for value in (b'a', b'b', b'c'):
                partial.write_bytes(value)
                os.utime(partial, ns=(1, 1))
                self.assertTrue(replace_if_changed(partial, target))
                self.assertEqual(target.read_bytes(), value)

    def test_trace_retains_unchanged_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, partial, trace = (root / name for name in ('final', 'partial', 'trace.json'))
            target.write_bytes(b'unchanged')
            partial.write_bytes(b'unchanged')
            scripts = Path(__file__).resolve().parent
            code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                    'from derived_files import replace_if_changed; '
                    'replace_if_changed(sys.argv[2], sys.argv[3])')
            script = root / 'producer.py'
            script.write_text(code)
            subprocess.run([sys.executable, '-B', str(scripts / '_trace.py'), str(trace), str(script),
                            str(scripts), str(partial), str(target)], check=True)
            found = json.loads(trace.read_text())
            self.assertIn(str(target), found['writes'])
            self.assertIn(str(target), found['reads'])
