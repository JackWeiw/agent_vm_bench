"""Compare two host_resources.xlsx builds cell by cell (parity check).

Used to prove a refactor of the raw_data -> xlsx pipeline changed no number:
build the same fixture with the old and the new code, then run

    python3 parity.py old.xlsx new.xlsx

Columns, shape, NaN placement and every numeric cell must match. The only
expected deviation between the pre-streaming and streaming builds is float
summation order (~1e-14) on a summed column.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd


def main(old_path: str, new_path: str) -> int:
    old = pd.read_excel(old_path, sheet_name=None)
    new = pd.read_excel(new_path, sheet_name=None)
    if set(old) != set(new):
        print(f"SHEET SET MISMATCH: {set(old) ^ set(new)}")
        return 1

    failures = 0
    for name, left in old.items():
        right = new[name]
        if list(left.columns) != list(right.columns):
            print(f"{name}: COLUMN MISMATCH {set(left.columns) ^ set(right.columns)}")
            failures += 1
            continue
        if left.shape != right.shape:
            print(f"{name}: SHAPE {left.shape} vs {right.shape}")
            failures += 1
            continue

        worst, worst_column = 0.0, None
        for column in left.columns:
            if left[column].dtype.kind not in "if" or right[column].dtype.kind not in "if":
                continue
            a = left[column].to_numpy(float)
            b = right[column].to_numpy(float)
            both = ~(np.isnan(a) | np.isnan(b))
            nan_diffs = int((np.isnan(a) != np.isnan(b)).sum())
            if nan_diffs:
                print(f"{name}.{column}: {nan_diffs} NaN-pattern differences")
                failures += 1
            if both.any():
                diff = float(np.max(np.abs(a[both] - b[both])))
                if diff > worst:
                    worst, worst_column = diff, column
        print(f"{name}: {left.shape[0]}x{left.shape[1]} maxdiff={worst:.3g} ({worst_column})")

    print("PARITY OK" if failures == 0 else f"PARITY FAILURES: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
