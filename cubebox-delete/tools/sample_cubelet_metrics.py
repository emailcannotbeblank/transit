#!/usr/bin/env python3
# 作用：按指定间隔从 Cubelet Prometheus 接口采集关键 gauge 和进程指标，
#       将带 UTC 时间戳的每次采样结果逐行写入 JSON Lines 文件。
# 用法：
#   python3 tools/sample_cubelet_metrics.py \
#     --url http://127.0.0.1:9998/v1/metrics \
#     --duration 8 --interval 0.1 \
#     --output cubelet-samples.jsonl
"""Sample selected Cubelet Prometheus gauges into JSON Lines."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


METRICS = {
    "cube_cubebox_scheduler_mvm_num",
    "cube_cubebox_scheduler_mvm_running_num",
    "cube_cubebox_scheduler_quota_cpu_usage_milli",
    "cube_cubebox_scheduler_quota_mem_mb_usage",
    "cube_cubebox_scheduler_realtime_create_num",
    "cube_cubebox_scheduler_realtime_destroy_num",
    "go_goroutines",
    "process_cpu_seconds_total",
    "process_open_fds",
    "process_resident_memory_bytes",
}


def parse_metrics(body: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in body.splitlines():
        if not line or line.startswith("#") or " " not in line:
            continue
        name, raw_value = line.rsplit(None, 1)
        if "{" in name:
            continue
        if name not in METRICS:
            continue
        try:
            values[name] = float(raw_value)
        except ValueError:
            pass
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:9998/v1/metrics",
    )
    parser.add_argument("--interval", type=float, default=0.05)
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args()

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.duration
    sequence = 0
    with output.open("w", encoding="utf-8") as stream:
        while time.monotonic() < deadline:
            cycle_started = time.monotonic()
            sample: dict[str, object] = {
                "sequence": sequence,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "epoch_ns": time.time_ns(),
            }
            try:
                with urllib.request.urlopen(args.url, timeout=2) as response:
                    sample["http_status"] = response.status
                    sample["metrics"] = parse_metrics(response.read().decode())
            except Exception as exc:
                sample["error"] = f"{type(exc).__name__}: {exc}"
            stream.write(json.dumps(sample, ensure_ascii=False) + "\n")
            stream.flush()
            sequence += 1
            remaining = args.interval - (time.monotonic() - cycle_started)
            if remaining > 0:
                time.sleep(remaining)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
