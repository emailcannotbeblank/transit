#!/usr/bin/env python3
"""
keybtree_summary.py - Overall summary of keybtree instrumentation events.

Usage:
    python3 keybtree_summary.py /tmp/keybtree_events_*.bin
    python3 keybtree_summary.py --tsc-ghz 2.5 /tmp/keybtree_events_*.bin
"""

import argparse
from collections import Counter

from keybtree_common import (
    load_events, fmt_duration, percentile, auto_detect_ghz, add_common_args,
)


def main():
    parser = argparse.ArgumentParser(description="Overall summary of keybtree events")
    add_common_args(parser)
    args = parser.parse_args()

    events = load_events(args.files)
    ghz = args.tsc_ghz
    if ghz is None:
        ghz = auto_detect_ghz()
        if ghz:
            print(f"  Auto-detected CPU frequency: {ghz:.3f} GHz (use --tsc-ghz to override)")

    total = len(events)
    fail_counts = Counter(e["fail_type"] for e in events)
    success = fail_counts.get(0, 0)
    wrlock_fail = fail_counts.get(1, 0)
    ver_miss = fail_counts.get(2, 0)
    split_fail = fail_counts.get(4, 0)
    null_arg = fail_counts.get(3, 0)

    print("\n" + "=" * 80)
    print("Overall Summary")
    print("=" * 80)

    print(f"  Total events:            {total}")
    print(f"  Success:                 {success} ({success/total*100:.1f}%)")
    print(f"  Wrlock fail (type=1):    {wrlock_fail} ({wrlock_fail/total*100:.1f}%)")
    print(f"  Version mismatch (t=2):  {ver_miss} ({ver_miss/total*100:.1f}%)")
    print(f"  Split fail (type=4):     {split_fail} ({split_fail/total*100:.1f}%)")
    print(f"  Null arg (type=3):       {null_arg} ({null_arg/total*100:.1f}%)")
    print(f"  Overall fail rate:       {(total-success)/total*100:.1f}%")

    durations = [e["duration"] for e in events]
    if durations:
        print(f"\n  Duration (all calls):")
        print(f"    avg:  {fmt_duration(sum(durations)/len(durations), ghz)}")
        print(f"    P50:  {fmt_duration(percentile(durations, 50), ghz)}")
        print(f"    P90:  {fmt_duration(percentile(durations, 90), ghz)}")
        print(f"    P99:  {fmt_duration(percentile(durations, 99), ghz)}")
        print(f"    P99.9:{fmt_duration(percentile(durations, 99.9), ghz)}")
        print(f"    max:  {fmt_duration(max(durations), ghz)}")

    fail_durations = [e["duration"] for e in events if e["fail_type"] == 1]
    if fail_durations:
        print(f"\n  Duration (wrlock_fail only):")
        print(f"    avg:  {fmt_duration(sum(fail_durations)/len(fail_durations), ghz)}")
        print(f"    P99:  {fmt_duration(percentile(fail_durations, 99), ghz)}")
        print(f"    max:  {fmt_duration(max(fail_durations), ghz)}")

    unique_nodes = set(e["old_node_addr"] for e in events)
    unique_tids = set(e["tid"] for e in events)
    print(f"\n  Unique nodes:            {len(unique_nodes)}")
    print(f"  Unique threads:          {len(unique_tids)}")

    if ghz:
        print(f"  TSC frequency:           {ghz} GHz")


if __name__ == "__main__":
    main()
