"""Spring layout in C (scripts/fastlayout.c): Barnes-Hut repulsion, OpenMP threads.

Drop-in for networkx.spring_layout(graph, seed, iterations, k, weight='weight')
on large graphs: same forces, cooling and stop rule, but O(n log n) per
iteration. Compiled with the system C compiler on first use (cached in
output/.build by source hash); spring_layout returns None when no compiler is
available so callers can fall back to networkx.
"""
import ctypes
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(__file__).with_suffix('.c')
_lib = None


def _load():
    global _lib
    if _lib is None:
        compiler = shutil.which('gcc') or shutil.which('cc')
        if compiler is None:
            return None
        digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:16]
        target = ROOT / f'output/.build/fastlayout-{digest}.so'
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(f'.{os.getpid()}.so')
            subprocess.run([compiler, '-O3', '-fopenmp', '-shared', '-fPIC', str(SOURCE), '-o', str(partial), '-lm'], check=True)
            partial.replace(target)
        lib = ctypes.CDLL(str(target))
        dbl, i32 = np.ctypeslib.ndpointer(np.float64, flags='C'), np.ctypeslib.ndpointer(np.int32, flags='C')
        lib.fr_layout.argtypes = [ctypes.c_int, dbl, ctypes.c_int, i32, i32, dbl, ctypes.c_double, ctypes.c_int,
                                  ctypes.c_double, ctypes.c_double, ctypes.c_uint64, ctypes.c_int]
        lib.fr_layout.restype = ctypes.c_int
        _lib = lib
    return _lib


def spring_layout(graph, seed=42, iterations=50, k=None, weight='weight', theta=0.8, threshold=1e-4, threads=None):
    """Positions {node: array([x, y])} rescaled like networkx (centred, max |coordinate| 1), or None without a compiler."""
    lib = _load()
    if lib is None:
        return None
    nodes = list(graph)
    n = len(nodes)
    if n == 0:
        return {}
    index = {node: i for i, node in enumerate(nodes)}
    offsets, targets, weights = [0], [], []
    for node in nodes:
        for other, data in graph.adj[node].items():
            if other != node:
                targets.append(index[other])
                weights.append(data.get(weight, 1.0))
        offsets.append(len(targets))
    pos = np.zeros(2 * n)
    k = 1 / np.sqrt(n) if k is None else k
    lib.fr_layout(n, pos, 0, np.array(offsets, np.int32), np.array(targets, np.int32), np.array(weights, np.float64),
                  k, iterations, theta, threshold, seed, threads or os.cpu_count() or 1)
    pos = pos.reshape(n, 2)
    pos -= pos.mean(axis=0)
    lim = np.abs(pos).max()
    if lim > 0:
        pos /= lim
    return dict(zip(nodes, pos))
