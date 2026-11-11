"""Publish a rebuilt file without invalidating readers when its bytes match."""
import hashlib
import json
import sys
from pathlib import Path


def replace_if_changed(temporary, target):
    """Consume temporary, keeping target's inode/mtime if it is byte-identical.

    Compare in bounded chunks, without filecmp's process-global stat cache:
    two successive temporary files can have the same size and timestamp.
    """
    temporary, target = Path(temporary), Path(target)
    if target.exists() and temporary.stat().st_size == target.stat().st_size:
        with temporary.open('rb') as new, target.open('rb') as old:
            while chunk := new.read(1024 * 1024):
                if chunk != old.read(len(chunk)):
                    break
            else:
                temporary.unlink()
                sys.audit('epstein.output', str(target))
                return False
    temporary.replace(target)
    return True


def metadata_matches(path, stamp, artifact):
    """A new input stamp validated the exact bytes of an existing artifact.

    Keep volatile validation metadata under output/.build, rather than changing
    a private artifact just to update its embedded cache stamp. No raw values
    are stored here; private callers supply HMAC input stamps.
    """
    try:
        return json.loads(path.read_text()) == [stamp, hashlib.blake2b(artifact.encode()).hexdigest()]
    except (OSError, ValueError):
        return False


def remember_metadata(path, stamp, artifact):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.partial.json')
    temporary.write_text(json.dumps([stamp, hashlib.blake2b(artifact.encode()).hexdigest()]))
    temporary.replace(path)
