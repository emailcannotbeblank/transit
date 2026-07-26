#!/usr/bin/env python3
# 作用：按 sandbox ID 关联生命周期报告与 CubeMaster、Cubelet、CubeShim
#       销毁日志，生成各销毁阶段的耗时明细及统计汇总（JSON 和 CSV）。
# 用法：
#   python3 tools/analyze_destroy_logs.py \
#     --report raw/my-observed-run/lifecycle.json \
#     --master-req raw/my-observed-run/logs/CubeMaster-cubemaster-req.log \
#     --cubelet-stat raw/my-observed-run/logs/Cubelet-Cubelet-stat.log \
#     --shim-req raw/my-observed-run/logs/CubeShim-cube-shim-req.log \
#     --shim-stat raw/my-observed-run/logs/CubeShim-cube-shim-stat.log \
#     --output-json raw/my-observed-run/destroy-breakdown.json \
#     --output-csv raw/my-observed-run/destroy-breakdown.csv
"""Join lifecycle timings with Cubelet and CubeShim destroy phase logs."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable


RESOURCE_ACTIONS = (
    "storage",
    "network",
    "cgroup",
    "volume",
    "netfile",
    "cube-sandbox-store",
    "images",
)


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def duration_ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() * 1000, 3)


def read_json_lines(path: Path) -> Iterable[dict[str, object]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def next_event(
    events: list[tuple[datetime, str]],
    text: str,
    after: datetime | None = None,
) -> datetime | None:
    for timestamp, content in events:
        if after is not None and timestamp < after:
            continue
        if content.startswith(text):
            return timestamp
    return None


def shim_stages(events: list[tuple[datetime, str]]) -> dict[str, float | None]:
    events.sort()
    kill_start = next_event(events, "kill req start")
    kill_finish = next_event(events, "kill req finish", kill_start)
    wait_start = next_event(events, "wait req start", kill_start)
    wait_finish = next_event(events, "wait req finish", wait_start)
    delete_start = next_event(events, "delete req start", wait_finish)
    delete_finish = next_event(events, "delete req finish", delete_start)
    shutdown_start = next_event(events, "shutdown req start", delete_finish)
    shutdown_finish = next_event(events, "shutdown req finish", shutdown_start)
    destroy_start = next_event(events, "destroy sandbox start", shutdown_start)
    wait_vm = next_event(events, "wait vm shutdown", destroy_start)
    wait_ch = next_event(events, "wait ch exit", wait_vm)
    destroy_finish = next_event(events, "destroy sandbox finish", wait_ch)
    return {
        "shim_kill_ms": duration_ms(kill_start, kill_finish),
        "shim_wait_task_ms": duration_ms(wait_start, wait_finish),
        "shim_delete_req_ms": duration_ms(delete_start, delete_finish),
        "shim_shutdown_req_ms": duration_ms(shutdown_start, shutdown_finish),
        "shim_destroy_sandbox_ms": duration_ms(destroy_start, destroy_finish),
        "shim_guest_destroy_rpc_approx_ms": duration_ms(destroy_start, wait_vm),
        "shim_wait_vm_shutdown_ms": duration_ms(wait_vm, wait_ch),
        "shim_vmm_join_ms": duration_ms(wait_ch, destroy_finish),
    }


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percent / 100
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def stats(values: Iterable[float | int | None]) -> dict[str, float | int] | None:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return {
        "count": len(clean),
        "min_ms": round(min(clean), 3),
        "mean_ms": round(statistics.fmean(clean), 3),
        "p50_ms": round(percentile(clean, 50), 3),
        "p95_ms": round(percentile(clean, 95), 3),
        "p99_ms": round(percentile(clean, 99), 3),
        "max_ms": round(max(clean), 3),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--master-req", type=Path, required=True)
    parser.add_argument("--cubelet-stat", type=Path, required=True)
    parser.add_argument("--shim-req", type=Path, required=True)
    parser.add_argument("--shim-stat", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    delete_samples = {
        sample["sandbox_id"]: sample
        for sample in report["phases"]["destroy"]["samples"]
    }
    sandbox_ids = set(delete_samples)

    master_ext_info: dict[str, dict[str, float]] = defaultdict(dict)
    for event in read_json_lines(args.master_req):
        sandbox_id = str(event.get("InstanceId", ""))
        content = event.get("LogContent")
        if (
            sandbox_id not in sandbox_ids
            or event.get("Action") != "DELETE"
            or not isinstance(content, str)
            or not content.startswith("Destroy_rsp:")
        ):
            continue
        try:
            payload = json.loads(content.removeprefix("Destroy_rsp:"))
        except json.JSONDecodeError:
            continue
        for key, raw_value in payload.get("ext_info", {}).items():
            try:
                decoded = base64.b64decode(raw_value).decode("ascii")
                master_ext_info[sandbox_id][str(key)] = float(decoded)
            except (ValueError, TypeError):
                try:
                    master_ext_info[sandbox_id][str(key)] = float(raw_value)
                except (ValueError, TypeError):
                    pass

    cubelet_values: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for event in read_json_lines(args.cubelet_stat):
        sandbox_id = str(event.get("InstanceId", ""))
        if sandbox_id not in sandbox_ids or event.get("Action") != "Destroy":
            continue
        callee = str(event.get("Callee", ""))
        cost = event.get("CostTime")
        if callee and isinstance(cost, (int, float)):
            cubelet_values[sandbox_id][callee].append(float(cost))

    shim_events: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for event in read_json_lines(args.shim_req):
        sandbox_id = str(event.get("InstanceId", ""))
        timestamp = event.get("Timestamp")
        content = event.get("LogContent")
        if (
            sandbox_id in sandbox_ids
            and isinstance(timestamp, str)
            and isinstance(content, str)
        ):
            shim_events[sandbox_id].append((parse_timestamp(timestamp), content))

    shim_stat_values: dict[str, list[float]] = defaultdict(list)
    for event in read_json_lines(args.shim_stat):
        sandbox_id = str(event.get("InstanceId", ""))
        cost = event.get("CostTime")
        if (
            sandbox_id in sandbox_ids
            and event.get("Action") == "Delete"
            and isinstance(cost, (int, float))
        ):
            shim_stat_values[sandbox_id].append(float(cost))

    rows: list[dict[str, object]] = []
    for sandbox_id in sorted(sandbox_ids):
        metrics = dict(master_ext_info[sandbox_id])
        metrics.update({
            key: round(max(values), 3)
            for key, values in cubelet_values[sandbox_id].items()
            if values
        })
        e2e_ms = float(delete_samples[sandbox_id]["latency_ms"])
        cubelet_service_ms = metrics.get("cubebox-service-unknown-container")
        resource_values = [
            metrics[key] for key in RESOURCE_ACTIONS if key in metrics
        ]
        row: dict[str, object] = {
            "sandbox_id": sandbox_id,
            "e2e_delete_ms": round(e2e_ms, 3),
            "cubelet_service_ms": cubelet_service_ms,
            "master_api_tail_approx_ms": round(e2e_ms - cubelet_service_ms, 3)
            if cubelet_service_ms is not None
            else None,
            "runtime_cubebox_ms": metrics.get("cubebox"),
            "resource_phase_max_ms": max(resource_values)
            if resource_values
            else None,
            "storage_ms": metrics.get("storage"),
            "network_ms": metrics.get("network"),
            "cgroup_ms": metrics.get("cgroup"),
            "volume_ms": metrics.get("volume"),
            "netfile_ms": metrics.get("netfile"),
            "sandbox_store_ms": metrics.get("cube-sandbox-store"),
            "cleanup_ms": metrics.get("cleanup"),
            "del_task_sandbox_ms": metrics.get("del-task-sandbox"),
            "del_sandbox_metadata_ms": metrics.get("del-sandbox-metadata"),
            "sandbox_del_container_ms": metrics.get("sandbox-del-container"),
            "shim_delete_container_stat_ms": max(
                shim_stat_values[sandbox_id],
                default=None,
            ),
        }
        row.update(shim_stages(shim_events[sandbox_id]))
        rows.append(row)

    numeric_keys = [
        key
        for key in rows[0]
        if key != "sandbox_id"
    ] if rows else []
    summary = {
        key: stats(row.get(key) for row in rows)
        for key in numeric_keys
    }
    output = {
        "run_id": report.get("run_id"),
        "sample_count": len(rows),
        "resource_phase_semantics": (
            "workflow step 2 runs actions in parallel; use max(action), not sum(action)"
        ),
        "samples": rows,
        "summary": summary,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=list(rows[0]) if rows else ["sandbox_id"],
        )
        writer.writeheader()
        writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
