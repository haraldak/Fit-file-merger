"""fit_merger.cli – Command-line interface for FIT File Merger.

Usage:
    python -m fit_merger.cli -o output.fit file1.fit file2.fit [file3.fit ...]
    fit-merge -o output.fit file1.fit file2.fit [file3.fit ...]
"""

import argparse
import os
import sys

from .core.merger import _find_last, _fv, fmt_time, get_fit_start_ts, merge_all
from .core.parser import FitParser, MESG_SESSION


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Merge two or more Garmin .fit activity files into one.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  fit-merge -o combined.fit morning.fit afternoon.fit\n"
            "  fit-merge -o full_day.fit part1.fit part2.fit part3.fit"
        ),
    )
    ap.add_argument("-o", "--output", required=True, metavar="OUTPUT",
                    help="Output .fit file path")
    ap.add_argument("files", nargs="+", metavar="FILE",
                    help="Two or more input .fit files (merged in start-time order)")
    args = ap.parse_args()

    if len(args.files) < 2:
        ap.error("At least two input files are required")

    for path in args.files:
        if not os.path.isfile(path):
            sys.exit(f"Error: file not found: {path}")

    out_abs = os.path.abspath(args.output)
    if any(os.path.abspath(p) == out_abs for p in args.files):
        sys.exit("Error: output path must differ from all input paths")

    # Load and sort by start time
    datasets = []
    for path in args.files:
        with open(path, "rb") as f:
            data = f.read()
        datasets.append((get_fit_start_ts(data), path, data))

    datasets.sort(key=lambda t: t[0])

    ordered_paths = [p for _, p, _ in datasets]
    ordered_data  = [d for _, _, d in datasets]

    if ordered_paths != args.files:
        print("Files reordered by start time:")
        for i, p in enumerate(ordered_paths, 1):
            print(f"  {i}. {os.path.basename(p)}")

    try:
        result = merge_all(ordered_data)
    except Exception as e:
        sys.exit(f"Error: {e}")

    with open(args.output, "wb") as f:
        f.write(result)

    # Per-file summary
    total_d = 0.0
    total_t = 0
    try:
        for i, (data, path) in enumerate(zip(ordered_data, ordered_paths), 1):
            recs = FitParser(data).parse()
            sess = _find_last(recs, MESG_SESSION)
            d = _fv(sess, 9) / 100_000
            t = _fv(sess, 8)
            total_d += d
            total_t += t
            print(f"  file {i}: {os.path.basename(path):<40}  {d:.2f} km  {fmt_time(t)}")
        print(f"  merged: {'':40}  {total_d:.2f} km  {fmt_time(total_t)}")
    except Exception:
        pass

    print(f"Written: {args.output}  ({len(result):,} bytes)")


if __name__ == "__main__":
    main()
