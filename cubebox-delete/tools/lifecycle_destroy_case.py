#!/usr/bin/env python3
# 作用：并发创建指定数量的 CubeSandbox，等待所有创建完成后通过 barrier
#       同时发起销毁，统计创建/销毁时延，并验证所有 sandbox 均已消失。
# 用法：需预先安装 e2b-code-interpreter，并提供 CubeSandbox API 配置。
#   E2B_API_URL=<api-url> E2B_API_KEY=<api-key> \
#   CUBE_TEMPLATE_ID=<template-id> \
#     python3 tools/lifecycle_destroy_case.py \
#       --count 10 --settle-seconds 1 \
#       --output raw/my-strict-run/lifecycle.json
"""Barrier-based CubeSandbox create/destroy lifecycle test.

The measured destroy phase starts only after every successful create has
completed and all destroy workers are waiting at the same barrier.  The JSON
report keeps per-request wall-clock timestamps so component logs can be joined
by sandbox ID and time window.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Generic, Iterable, TypeVar

from e2b_code_interpreter import Sandbox


T = TypeVar("T")
R = TypeVar("R")


@dataclass
class Sample:
    seq: int
    sandbox_id: str = ""
    ok: bool = False
    started_at: str = ""
    started_epoch_ns: int = 0
    ended_at: str = ""
    ended_epoch_ns: int = 0
    latency_ms: float = 0.0
    error: str = ""


@dataclass
class SandboxItem:
    seq: int
    sandbox_id: str
    sandbox: Sandbox


@dataclass
class Phase(Generic[R]):
    name: str
    barrier_released_at: str
    barrier_released_epoch_ns: int
    wall_ms: float
    samples: list[Sample]
    results: list[R]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def latency_stats(values: Iterable[float]) -> dict[str, float | int] | None:
    clean = list(values)
    if not clean:
        return None
    return {
        "count": len(clean),
        "min_ms": round(min(clean), 3),
        "mean_ms": round(statistics.fmean(clean), 3),
        "p50_ms": round(percentile(clean, 50), 3),
        "p90_ms": round(percentile(clean, 90), 3),
        "p95_ms": round(percentile(clean, 95), 3),
        "p99_ms": round(percentile(clean, 99), 3),
        "max_ms": round(max(clean), 3),
    }


def run_barrier_phase(
    name: str,
    inputs: list[T],
    seq_of: Callable[[T], int],
    sandbox_id_of: Callable[[T], str],
    operation: Callable[[T], R],
) -> Phase[R]:
    if not inputs:
        return Phase(name, utc_now(), time.time_ns(), 0.0, [], [])

    released_epoch_ns = [0]
    released_at = [""]
    released_perf_ns = [0]

    def release() -> None:
        released_epoch_ns[0] = time.time_ns()
        released_at[0] = utc_now()
        released_perf_ns[0] = time.perf_counter_ns()

    barrier = threading.Barrier(len(inputs) + 1, action=release)

    def worker(item: T) -> tuple[Sample, R | None]:
        sample = Sample(
            seq=seq_of(item),
            sandbox_id=sandbox_id_of(item),
        )
        barrier.wait()
        started_perf_ns = time.perf_counter_ns()
        sample.started_epoch_ns = time.time_ns()
        sample.started_at = utc_now()
        try:
            result = operation(item)
            result_id = getattr(result, "sandbox_id", "")
            if result_id:
                sample.sandbox_id = str(result_id)
            sample.ok = True
            return sample, result
        except Exception as exc:
            sample.error = f"{type(exc).__name__}: {exc}"
            return sample, None
        finally:
            sample.ended_epoch_ns = time.time_ns()
            sample.ended_at = utc_now()
            sample.latency_ms = round(
                (time.perf_counter_ns() - started_perf_ns) / 1_000_000,
                3,
            )

    samples: list[Sample] = []
    results: list[R] = []
    with ThreadPoolExecutor(
        max_workers=len(inputs),
        thread_name_prefix=f"cube-{name}",
    ) as executor:
        futures = [executor.submit(worker, item) for item in inputs]
        barrier.wait()
        for future in as_completed(futures):
            sample, result = future.result()
            samples.append(sample)
            if result is not None:
                results.append(result)
            state = "OK" if sample.ok else "FAIL"
            detail = f" {sample.error}" if sample.error else ""
            print(
                f"[{name} {sample.seq:03d}] {state:<4} "
                f"{sample.latency_ms:9.3f} ms {sample.sandbox_id}{detail}"
            )

    samples.sort(key=lambda value: value.seq)
    wall_ms = (time.perf_counter_ns() - released_perf_ns[0]) / 1_000_000
    return Phase(
        name=name,
        barrier_released_at=released_at[0],
        barrier_released_epoch_ns=released_epoch_ns[0],
        wall_ms=round(wall_ms, 3),
        samples=samples,
        results=results,
    )


def phase_report(phase: Phase[object]) -> dict[str, object]:
    successful = [sample for sample in phase.samples if sample.ok]
    starts = [sample.started_epoch_ns for sample in phase.samples]
    attempted = len(phase.samples)
    return {
        "attempted": attempted,
        "successful": len(successful),
        "failed": attempted - len(successful),
        "success_rate": round(len(successful) / attempted, 6)
        if attempted
        else 0,
        "barrier_released_at": phase.barrier_released_at,
        "barrier_released_epoch_ns": phase.barrier_released_epoch_ns,
        "worker_start_skew_ms": round((max(starts) - min(starts)) / 1_000_000, 3)
        if starts
        else 0,
        "wall_ms": phase.wall_ms,
        "throughput_per_second": round(
            len(successful) / (phase.wall_ms / 1000),
            3,
        )
        if phase.wall_ms
        else 0,
        "latency": latency_stats(sample.latency_ms for sample in successful),
        "samples": [asdict(sample) for sample in phase.samples],
    }


def health(api_url: str) -> dict[str, object]:
    started = time.perf_counter()
    with urllib.request.urlopen(
        api_url.rstrip("/") + "/health",
        timeout=5,
    ) as response:
        body = json.loads(response.read())
    return {
        "status_code": response.status,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "body": body,
    }


def listed_sandbox_ids(request_timeout: float) -> set[str]:
    paginator = Sandbox.list(limit=1000, request_timeout=request_timeout)
    sandbox_ids: set[str] = set()
    while paginator.has_next:
        for item in paginator.next_items():
            sandbox_id = getattr(item, "sandbox_id", "")
            if sandbox_id:
                sandbox_ids.add(str(sandbox_id))
    return sandbox_ids


def wait_until_absent(
    expected_ids: set[str],
    request_timeout: float,
    verify_timeout: float,
) -> tuple[set[str], list[str]]:
    errors: list[str] = []
    deadline = time.monotonic() + verify_timeout
    residual = set(expected_ids)
    while residual and time.monotonic() < deadline:
        try:
            residual = expected_ids & listed_sandbox_ids(request_timeout)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        if residual:
            time.sleep(0.25)
    return residual, errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create N sandboxes, wait for all creates, then barrier-release "
            "all destroy calls and verify that every sandbox disappears."
        )
    )
    parser.add_argument("-n", "--count", type=int, default=10)
    parser.add_argument(
        "--template",
        default=os.getenv("CUBE_TEMPLATE_ID", ""),
    )
    parser.add_argument("--settle-seconds", type=float, default=1.0)
    parser.add_argument("--sandbox-timeout", type=int, default=300)
    parser.add_argument("--request-timeout", type=float, default=120)
    parser.add_argument("--verify-timeout", type=float, default=10)
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")
    if not args.template:
        parser.error("set --template or CUBE_TEMPLATE_ID")
    for name in ("E2B_API_URL", "E2B_API_KEY"):
        if not os.getenv(name):
            parser.error(f"missing environment variable {name}")
    return args


def main() -> int:
    args = parse_args()
    api_url = os.environ["E2B_API_URL"]
    run_id = f"destroy-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    report_started_at = utc_now()
    preflight = health(api_url)

    def create(seq: int) -> SandboxItem:
        sandbox = Sandbox.create(
            template=args.template,
            timeout=args.sandbox_timeout,
            metadata={"destroy-benchmark-run-id": run_id},
            request_timeout=args.request_timeout,
        )
        sandbox_id = str(sandbox.sandbox_id)
        if not sandbox_id:
            raise RuntimeError("create returned an empty sandbox ID")
        return SandboxItem(seq=seq, sandbox_id=sandbox_id, sandbox=sandbox)

    create_phase = run_barrier_phase(
        "create",
        list(range(1, args.count + 1)),
        seq_of=lambda seq: seq,
        sandbox_id_of=lambda _seq: "",
        operation=create,
    )
    sandboxes = sorted(create_phase.results, key=lambda item: item.seq)
    created_ids = {item.sandbox_id for item in sandboxes}
    print(f"all creates returned; settling for {args.settle_seconds:.3f}s")
    time.sleep(max(0, args.settle_seconds))

    def destroy(item: SandboxItem) -> SandboxItem:
        if not item.sandbox.kill(request_timeout=args.request_timeout):
            raise RuntimeError("SDK kill returned false")
        return item

    destroyed_ids: set[str] = set()
    try:
        destroy_phase = run_barrier_phase(
            "destroy",
            sandboxes,
            seq_of=lambda item: item.seq,
            sandbox_id_of=lambda item: item.sandbox_id,
            operation=destroy,
        )
        destroyed_ids = {
            sample.sandbox_id
            for sample in destroy_phase.samples
            if sample.ok
        }
    finally:
        for item in sandboxes:
            if item.sandbox_id in destroyed_ids:
                continue
            try:
                item.sandbox.kill(request_timeout=args.request_timeout)
                print(f"[cleanup] removed {item.sandbox_id}", file=sys.stderr)
            except Exception as exc:
                print(
                    f"[cleanup] failed {item.sandbox_id}: {exc}",
                    file=sys.stderr,
                )

    residual_ids, verify_errors = wait_until_absent(
        created_ids,
        args.request_timeout,
        args.verify_timeout,
    )
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": report_started_at,
        "finished_at": utc_now(),
        "config": {
            "api_url": api_url,
            "template": args.template,
            "count": args.count,
            "settle_seconds": args.settle_seconds,
            "sandbox_timeout": args.sandbox_timeout,
            "request_timeout": args.request_timeout,
        },
        "preflight": preflight,
        "phases": {
            "create": phase_report(create_phase),
            "destroy": phase_report(destroy_phase),
        },
        "verification": {
            "created_ids": sorted(created_ids),
            "destroyed_success_ids": sorted(destroyed_ids),
            "residual_ids": sorted(residual_ids),
            "list_errors": verify_errors,
        },
    }

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"report: {output}")

    create_ok = len(sandboxes) == args.count
    destroy_ok = len(destroyed_ids) == len(sandboxes)
    verified = not residual_ids and not verify_errors
    print(
        f"result: create={len(sandboxes)}/{args.count}, "
        f"destroy={len(destroyed_ids)}/{len(sandboxes)}, "
        f"residual={len(residual_ids)}"
    )
    return 0 if create_ok and destroy_ok and verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
