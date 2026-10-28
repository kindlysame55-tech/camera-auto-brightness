"""Repeatable, hardware-free analysis benchmark; emits JSON to stdout."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import numpy as np
from camab.metrics import AnalysisConfig, analyze_frame


def benchmark(iterations=100):
    rng = np.random.default_rng(2077)
    results = []
    for width, height in ((320, 240), (1920, 1080)):
        frame = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
        cfg = AnalysisConfig()
        for _ in range(5):
            analyze_frame(frame, cfg)
        elapsed = []
        for _ in range(iterations):
            start = time.perf_counter()
            analyze_frame(frame, cfg)
            elapsed.append((time.perf_counter() - start) * 1000)
        tracemalloc.start()
        value = analyze_frame(frame, cfg)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        results.append({'size': [width, height], 'iterations': iterations,
                        'median_ms': round(statistics.median(elapsed), 4),
                        'p95_ms': round(float(np.percentile(elapsed, 95)), 4),
                        'peak_bytes': peak, 'analysis': value.as_dict()})
    return {'python': sys.version.split()[0], 'numpy': np.__version__, 'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations', type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(benchmark(max(5, args.iterations)), indent=2))
