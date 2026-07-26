#!/usr/bin/env python3
# 作用：读取指定目录中的 iostat、pidstat、mpstat 和 vmstat 采样文件，
#       排除不适用的首条平均样本后，将关键系统资源指标汇总为 JSON。
# 用法：
#   python3 tools/analyze_system_metrics.py \
#     raw/my-observed-run/system \
#     --output raw/my-observed-run/system/summary.json
#   省略 --output 时，汇总结果会输出到标准输出。
"""Summarize sysstat/vmstat files produced by run_observability_example.sh."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def rounded(value: float) -> float:
    return round(value, 3)


def summarize(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": rounded(min(values)),
        "mean": rounded(statistics.fmean(values)),
        "max": rounded(max(values)),
    }


def parse_iostat(path: Path) -> dict[str, dict[str, dict[str, float | int]]]:
    # iostat -x columns after Device:
    names = [
        "r_s",
        "r_kib_s",
        "rrqm_s",
        "rrqm_pct",
        "r_await_ms",
        "rareq_kib",
        "w_s",
        "w_kib_s",
        "wrqm_s",
        "wrqm_pct",
        "w_await_ms",
        "wareq_kib",
        "d_s",
        "d_kib_s",
        "drqm_s",
        "drqm_pct",
        "d_await_ms",
        "dareq_kib",
        "f_s",
        "f_await_ms",
        "avg_queue",
        "util_pct",
    ]
    blocks = -1
    samples: dict[str, dict[str, list[float]]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("Device"):
            blocks += 1
            continue
        fields = line.split()
        if blocks <= 0 or len(fields) != len(names) + 1:
            continue
        try:
            values = [float(value) for value in fields[1:]]
        except ValueError:
            continue
        device = fields[0]
        device_samples = samples.setdefault(
            device, {name: [] for name in names}
        )
        for name, value in zip(names, values):
            device_samples[name].append(value)

    selected = {}
    for device, metrics in samples.items():
        if device == "loop0" or device.startswith("nvme"):
            selected[device] = {
                name: summarize(metrics[name])
                for name in (
                    "r_kib_s",
                    "w_kib_s",
                    "r_await_ms",
                    "w_await_ms",
                    "f_await_ms",
                    "avg_queue",
                    "util_pct",
                )
            }
    return selected


def parse_pidstat(path: Path) -> dict[str, dict[str, dict[str, float | int]]]:
    values: dict[str, dict[str, list[float]]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) < 21 or not fields[0][0:1].isdigit():
            continue
        try:
            cpu = float(fields[7])
            rss_kib = float(fields[12])
        except ValueError:
            continue
        command = fields[-1]
        command_values = values.setdefault(command, {"cpu_pct": [], "rss_kib": []})
        command_values["cpu_pct"].append(cpu)
        command_values["rss_kib"].append(rss_kib)
    return {
        command: {metric: summarize(samples) for metric, samples in metrics.items()}
        for command, metrics in values.items()
    }


def parse_mpstat(path: Path) -> dict[str, dict[str, float | int]]:
    busy: list[float] = []
    iowait: list[float] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) != 12 or fields[1] != "all":
            continue
        try:
            iowait.append(float(fields[5]))
            busy.append(100.0 - float(fields[-1]))
        except ValueError:
            continue
    return {"all_cpu_busy_pct": summarize(busy), "all_cpu_iowait_pct": summarize(iowait)}


def parse_vmstat(path: Path) -> dict[str, dict[str, float | int]]:
    rows: list[list[float]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) != 18:
            continue
        try:
            rows.append([float(value) for value in fields])
        except ValueError:
            continue
    # vmstat's first row is an average since boot, not an interval sample.
    rows = rows[1:]
    return {
        "run_queue": summarize([row[0] for row in rows]),
        "blocked_tasks": summarize([row[1] for row in rows]),
        "io_read_kib_s": summarize([row[8] for row in rows]),
        "io_write_kib_s": summarize([row[9] for row in rows]),
        "cpu_iowait_pct": summarize([row[15] for row in rows]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("system_dir", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()

    result = {
        "notes": [
            "iostat and vmstat first since-boot samples are excluded",
            "one-second sampling is coarser than this sub-second lifecycle run",
        ],
        "iostat": parse_iostat(args.system_dir / "iostat.txt"),
        "pidstat": parse_pidstat(args.system_dir / "pidstat.txt"),
        "mpstat": parse_mpstat(args.system_dir / "mpstat.txt"),
        "vmstat": parse_vmstat(args.system_dir / "vmstat.txt"),
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
