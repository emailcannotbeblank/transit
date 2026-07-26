# 0725 CubeSandbox 销毁测试交付

## 结论入口

- `00_执行与数据分析报告.md`：部署状态、严格用例结果、阶段拆解、资源与残留分析。
- `01_采集方法与复现示例.md`：材料中 5 种主工具的逐项示例，以及全量观测复现命令。

## 可执行工具

| 文件 | 用途 |
|---|---|
| `tools/lifecycle_destroy_case.py` | 先完成全部创建，再用独立 barrier 同时销毁 |
| `tools/run_observability_example.sh` | 生命周期、日志、Prom、pprof、系统资源、CubeCoW、收敛检查一键采集 |
| `tools/analyze_destroy_logs.py` | 按 sandbox ID 合并 E2E、Master、Cubelet、Shim 数据 |
| `tools/analyze_system_metrics.py` | 汇总 iostat/pidstat/mpstat/vmstat |
| `tools/sample_cubelet_metrics.py` | 高频采集 Cubelet 关键 Prometheus gauge |
| `tools/cubelet_storage_metrics.go` | 采集 CubeCoW 对象/容量指标 |
| `tools/cubelet_destroy.go` | 节点级测试资源幂等 Destroy |
| `tools/network_release.go` | 失败创建时幂等释放 network-agent allocation |

## 请求配置

- `configs/cubemaster_request.json`
- `configs/cubelet_request.json`

Cubelet 配置中的 `SizeLimit` 大小写是有意的，不能改成 Master HTTP JSON 使用的 `size_limit`。

## 结果目录

| 路径 | 内容 |
|---|---|
| `processed/strict-runs-summary.json` | 3 轮严格 barrier 的机器可读汇总 |
| `processed/tool-examples-summary.json` | 5 种既有工具的机器可读汇总 |
| `raw/deployment/` | systemd、版本、健康、端口、节点/模板 |
| `raw/strict-barrier-n10/` | 第一轮全量观测，Prom 20 ms |
| `raw/strict-barrier-baseline-n10/` | 无伴随观测的严格基线 |
| `raw/strict-barrier-n10-repeat/` | 推荐主结果，全量观测，Prom 100 ms |
| `raw/tool-examples/` | 5 种既有工具的真实输出 |
| `raw/final-state/` | API/Master/Cubelet/Redis/network/process 最终收敛 |
| `raw/cleanup/` | 两个失败配置调试对象的显式清理证据 |

## 主结果

严格 N=10、先创建后统一销毁：

```text
success              10/10
DELETE mean          126.907 ms
DELETE P95           129.637 ms
DELETE P99           129.940 ms
runtime cubebox mean 114.711 ms
resource max mean      3.358 ms
Master/API tail mean   8.071 ms
residual                  0
```

当前证据指向 Shim/VM 退出链路，尚未显示 storage、network、CPU 或磁盘在 N=10 饱和。
