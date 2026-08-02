# 分析沙箱并发 GC 原因

> 代码地址：/home/zry/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/orchestrator
> 问题：创建沙箱，运行浏览器渲染。单沙箱2s内完成，50并发场景，时间显著增加，平均时间20s。
> 抓火焰图，发现超过一半的时间在做GC。
> 看相关代码，分析触发GC原因

<details><summary>9 previous messages</summary>

> 我会先建立这个排查目标，然后从 orchestrator 的并发创建链路、浏览器渲染相关数据流，以及 Go/Node 运行时的内存分配热点入手；重点对照“单沙箱正常、50 并发 GC 占比过半”这一特征，找出会随并发放大的对象存活或分配模式。
>
> <details><summary>Ran 2 commands</summary>
>
> - Ran `pwd && rg --files -g 'AGENTS.md' -g '!vendor' -g '!node_modules' .. | head -80 && rg --files /home/zry/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/orchestrator | sed -n '1,240p'`
> - Ran `for p in /home/zry/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/orchestrator /home/zry/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages /home/zry/桌面/e2b/e2b-infra/e2b-infra-2026.09 /home/zry/桌面/e2b/e2b-infra /home/zry/桌面/e2b /home/zry/桌面; do test -f "$p/AGENTS.md" && echo "$p/AGENTS.md"; done
> sed -n '1,260p' internal/server/sandboxes.go
> sed -n '1,300p' internal/sandbox/sandbox.go
> sed -n '1,220p' benchmark_test.go`
>
> </details>
>
> 创建入口确认了两个重要边界：节点最多允许 30 个沙箱同时启动，并且每次恢复都会建立独立的内存后端。接下来我会沿 `template.Memfile → UFFD page fault → 远端块读取/解压 → Firecracker` 这条路径逐层检查，因为浏览器启动会快速触碰大量内存页，最容易把按页临时对象的分配速率放大到 GC 饱和。
>
> <details><summary>Explored 30 files, 3 searches, ran 3 commands</summary>
>
> <details><summary>Explored 25 files, 2 searches</summary>
>
> - Read `./internal/sandbox/sandbox.go`
> - Read `./internal/sandbox/uffd/memory_backend.go`
> - Read `./internal/sandbox/uffd/uffd.go`
> - Read `./internal/sandbox/fc/memory.go`
> - Read `./internal/sandbox/template/template.go`
> - Read `./internal/sandbox/template/storage_template.go`
> - Read `./internal/sandbox/template/file.go`
> - Read `./internal/sandbox/sandbox.go`
> - Read `./internal/sandbox/template/storage.go`
> - Read `./internal/sandbox/block/device.go`
> - Read `./internal/sandbox/block/streaming_chunk.go`
> - Read `./internal/sandbox/block/chunk.go`
> - Read `./internal/sandbox/block/cache.go`
> - Read `./internal/sandbox/block/overlay.go`
> - Read `./internal/sandbox/block/iov.go`
> - Read `./internal/sandbox/uffd/userfaultfd/userfaultfd.go`
> - Read `./internal/sandbox/uffd/userfaultfd/fd.go`
> - Read `./internal/sandbox/uffd/prefetch/prefetcher.go`
> - Read `./internal/sandbox/block/prefetch_tracker.go`
> - Read `./internal/sandbox/block/tracker.go`
> - Searched for `make\(\[\]byte|bytes\.NewBuffer|io\.ReadAll|Slice\(|ReadAt\(|sync\.Pool|runtime\.GC|debug\.FreeOSMemory|SetGCPercent|GOMEMLIMIT|GOGC` in `sandbox`
> - Read `./internal/sandbox/build/build.go`
> - Read `./internal/sandbox/build/storage_diff.go`
> - Read `./internal/sandbox/build/cache.go`
> - Read `./internal/sandbox/build/diff.go`
> - Read `./internal/sandbox/block/streaming_chunk.go`
> - Searched for `MemoryPrefetch|maxRequestsInProgress|MemoryChunkSize|BlockSize|block_size|page_size|PageSize|GOMEMLIMIT|GOGC|SetMemoryLimit|SetGCPercent` in `packages`
>
> </details>
>
> - Ran `git status --short && git log -8 --oneline --decorate -- internal/sandbox/uffd internal/sandbox/block internal/sandbox/sandbox.go
> rg -n --glob '!vendor/**' --glob '*.go' --glob '*.yaml' --glob '*.yml' --glob '*.sh' --glob 'Dockerfile*' "MemoryPrefetch(Max|Enabled)|memory-prefetch|maxRequestsInProgress|MemoryChunkSize|GOMEMLIMIT|GOGC|SetMemoryLimit|SetGCPercent|HugePages|hugepages" . ../shared ../api ../cluster 2>/dev/null | head -600
> rg -n --glob '!vendor/**' "MemoryPrefetchMaxFetchWorkers|MemoryPrefetchMaxCopyWorkers|ChunkerConfigFlag" ../shared .`
>
> - Ran `git diff -- internal/sandbox/uffd/userfaultfd/userfaultfd.go internal/sandbox/uffd/uffd.go internal/sandbox/sandbox.go internal/server/sandboxes.go internal/sandbox/block/cache.go internal/sandbox/fc/client.go internal/sandbox/fc/process.go ../shared/pkg/feature-flags/flags.go ../../iac/provider-gcp/nomad/jobs/orchestrator.hcl Dockerfile internal/cfg/model.go | sed -n '1,1600p'`
>
> <details><summary>Explored 7 files, a search</summary>
>
> - Searched for `startingSandboxes|waitForAcquire|maxStartingInstancesPerNode|ResumeSandbox|b.RunParallel|SetParallelism|Run\(` in `server`
> - Read `./internal/server/main.go`
> - Read `./benchmark_test.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/feature-flags/flags.go`
> - Read `./internal/template/build/config/config.go`
> - Read `./internal/template/build/phases/base/files.go`
> - Read `./internal/template/build/layer/create_sandbox.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/api/internal/orchestrator/create_instance.go`
>
> </details>
>
> - Ran `sed -n '1,100p' ../api/internal/sandbox/sandbox_features.go
> sed -n '220,270p' ../api/internal/orchestrator/create_instance.go
> sed -n '1,80p' ../shared/pkg/storage/header/header.go 2>/dev/null || true
> rg -n --glob '!vendor/**' "const \(|PageSize|HugepageSize|RootfsBlockSize" ../shared/pkg/storage/header
> sed -n '1,200p' internal/server/utils.go
> rg -n --glob '!vendor/**' "MAX_STARTING_INSTANCES_PER_NODE|GOMEMLIMIT|GOGC|memory" ../../helm ../../iac/provider-gcp/nomad/jobs/orchestrator.hcl ../../iac/provider-gcp/nomad-cluster/scripts ../../.github/actions/host-init 2>/dev/null | head -400`
>
> </details>
>
> 目前代码里已经出现一条很强的证据链：本地改动把“节点同时启动数”从 3 提到 30，同时把每沙箱内存预取 worker 从 16/8 提到 32/16。也就是说，启动阶段的瞬时并行度不是 10 倍，而是叠乘到数百/上千个任务；此外每个预取页还单独创建一次 OpenTelemetry span。我正在继续量化每页路径上的 goroutine、channel、map 和指标对象分配，判断哪个是主要 GC 分配源、哪个只是放大器。
>
> <details><summary>Explored 18 files, 8 searches, ran 10 commands</summary>
>
> - Ran `sed -n '80,220p' ../shared/pkg/storage/header/header.go
> sed -n '1,260p' internal/sandbox/block/metrics/main.go
> rg -n --glob '!vendor/**' "type .*Timer|TimerFactory|SlicesTimerFactory|RemoteReadsTimerFactory|func \(.*\) Begin\(|func \(.*\) Success\(" internal ../shared/pkg/telemetry ../shared/pkg/storage | head -300
> sed -n '1,260p' internal/sandbox/block/metrics/metrics.go
> rg -n --glob '!vendor/**' "NewTracerProvider|WithBatcher|NewBatchSpanProcessor|WithMaxQueueSize|WithMaxExportBatchSize|WithBatchTimeout|SpanExporter" ../shared/pkg/telemetry .
> sed -n '1,300p' ../shared/pkg/telemetry/tracing.go 2>/dev/null || true
> sed -n '1,320p' ../shared/pkg/telemetry/telemetry.go 2>/dev/null || true`
>
> <details><summary>Explored 6 files</summary>
>
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/telemetry/meters.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/telemetry/traces.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/telemetry/main.go`
> - Read `./internal/sandbox/block/streaming_chunk.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/storage/storage.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/storage/storage_cache_seekable.go`
>
> </details>
>
> - Ran `sed -n '1,120p' ../shared/pkg/storage/header/range.go 2>/dev/null || rg -n "func BlocksOffsets|func Block" ../shared/pkg/storage/header -n
> rg -n "func BlocksOffsets" ../shared/pkg/storage/header -n -C 8
> sed -n '1,330p' internal/sandbox/nbd/dispatch.go
> rg -n --glob '!vendor/**' "func \(.*OpenRangeReader|OpenRangeReader\(" ../shared/pkg/storage | head -200
> sed -n '240,310p' ../shared/pkg/storage/storage_minio.go
> sed -n '230,290p' ../shared/pkg/storage/storage_google.go
> sed -n '1,180p' internal/sandbox/rootfs/nbd.go
> sed -n '1,220p' internal/sandbox/nbd/pool.go`
>
> - Ran `rg -n "NewDispatch|\.Handle\(" internal/sandbox/nbd -g '*.go'
> sed -n '180,420p' internal/sandbox/nbd/path_direct.go
> sed -n '1,240p' internal/sandbox/nbd/path_direct.go
> rg -n --glob '!vendor/**' "NewNBDProvider\(" internal | cat
> rg -n --glob '!vendor/**' "make\(\[\]byte" internal ../shared/pkg/storage | sort | sed -n '1,300p'`
>
> - Ran `rg -n --glob '!vendor/**' "NBDConnectionsPerDevice|nbd-connections" ../shared/pkg/feature-flags .
> git diff -- ../shared/pkg/feature-flags/flags.go | sed -n '1,260p'
> sed -n '120,190p' ../shared/pkg/feature-flags/flags.go
> rg -n --glob '!vendor/**' "NBDConnectionsPerDevice" ../../helm ../../iac . 2>/dev/null | head -100
> nl -ba internal/server/sandboxes.go | sed -n '35,115p'
> nl -ba internal/sandbox/uffd/userfaultfd/userfaultfd.go | sed -n '20,90p;155,235p;310,380p'
> nl -ba internal/sandbox/uffd/prefetch/prefetcher.go | sed -n '70,145p;180,250p'
> nl -ba internal/sandbox/nbd/dispatch.go | sed -n '20,280p'`
>
> <details><summary>Explored 8 files, a search</summary>
>
> - Searched for `*pprof*` in `e2b-infra-2026.09`
> - Read `./internal/sandbox/block/cache.go`
> - Read `./internal/sandbox/block/overlay.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/storage/header/block.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/telemetry/meters.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/storage/storage_cache_seekable.go`
> - Read `./internal/sandbox/nbd/path_direct.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/feature-flags/flags.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/telemetry/traces.go`
>
> </details>
>
> - Ran `sed -n '1,180p' ../../iac/provider-gcp/nomad/jobs/orchestrator.hcl
> sed -n '70,120p' ../../helm/templates/orchestrator.yaml
> rg -n "limits|memory|cpu|GOMEMLIMIT|GOGC|MAX_STARTING|NBD" ../../iac/provider-gcp/nomad/jobs/orchestrator.hcl ../../helm/templates/orchestrator.yaml ../../helm/values.yaml ../../helm/*.yaml 2>/dev/null
> rg -n "type .*Provider|NewCached|cachedSeekable|newCached|CacheSeekable|Enable.*Cache|NFS" ../shared/pkg/storage --glob '!vendor/**' | head -300
> rg -n --glob '!vendor/**' "newCachedSeekable|cachedSeekable\{" ../shared/pkg/storage -C 6`
>
> <details><summary>Explored 6 files, a search</summary>
>
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/storage/storage_cache.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/storage/storage_cache_seekable.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/storage/storage_cache_seekable.go`
> - Searched for `MaxCacheWriterConcurrencyFlag|New.*Limiter|cacheWriter|writer.*limit` in `storage`
> - Read `./internal/sandbox/block/chunk.go`
> - Read `./internal/sandbox/block/streaming_chunk.go`
> - Read `./internal/sandbox/build/build.go`
>
> </details>
>
> - Ran `rg -n "func Generate" ../shared/pkg/id -C 5
> sed -n '1,120p' ../shared/pkg/id/id.go
> rg -n --glob '!vendor/**' "func NewSetOnce|type SetOnce" ../shared/pkg/utils -C 5
> sed -n '1,220p' ../shared/pkg/utils/setonce.go 2>/dev/null || true
> rg -n --glob '!vendor/**' "func \(s \*DiffStore\) Get|GetOrSet" internal/sandbox/build -C 10
> nl -ba internal/sandbox/build/storage_diff.go | sed -n '1,105p'
> nl -ba internal/sandbox/build/cache.go | sed -n '80,140p'
> rg -n "func \(.*JSONFlag|func \(.*IntFlag" ../shared/pkg/feature-flags --glob '!vendor/**' -C 5 | head -240`
>
> - Ran `sed -n '1,100p' ../shared/pkg/utils/set_once.go
> sed -n '1,180p' ../shared/pkg/feature-flags/client.go
> rg -n "func getFlag" ../shared/pkg/feature-flags -C 8
> sed -n '1,120p' ../shared/pkg/feature-flags/utils.go 2>/dev/null || true
> rg -n --glob '!vendor/**' "func \(.*Get\(|type Cache" $(go env GOPATH 2>/dev/null)/pkg/mod/github.com/jellydator/ttlcache*/cache.go | head -50 2>/dev/null || true`
>
> <details><summary>Explored 5 files, 6 searches</summary>
>
> - Searched for `net/http/pprof|pprof|runtime/metrics|gctrace|GODEBUG` in `.`
> - Searched for `http\.ListenAndServe|grpc.*reflection|NewServeMux|Register.*pprof` in `internal`
> - Read `./main.go`
> - Read `./internal/server/main.go`
> - Read `./internal/factories/http.go`
> - Read `./internal/service/machineinfo/main.go`
> - Searched for `pyroscope|profile|Profiler` in `.`
> - Searched for `NewMetrics\(` in `main.go`
> - Searched for `WrapInNFSCache` in `main.go`
> - Searched for `SharedChunkCache|SHARED_CHUNK` in `cfg`
> - Read `./main.go`
>
> </details>
>
> - Ran `rg -n --glob '!vendor/**' "type WaitMap|func NewWaitMap|func \(.*WaitMap" ../shared/pkg/utils -C 6
> sed -n '1,240p' ../shared/pkg/utils/wait_map.go 2>/dev/null || true
> rg -n --glob '!vendor/**' "type Promise|func NewPromise" ../shared/pkg/utils -C 5
> sed -n '1,180p' ../shared/pkg/utils/promise.go 2>/dev/null || true
> nl -ba internal/sandbox/build/build.go | sed -n '95,145p'
> nl -ba internal/sandbox/build/storage_diff.go | sed -n '40,75p'
> nl -ba internal/sandbox/build/cache.go | sed -n '80,125p'`
>
> - Ran `grep -n "func OnceValue" "$(go env GOROOT)/src/sync/oncefunc.go" -A80
> rg -n --glob '!vendor/**' "func \(.*resetDelete" internal/sandbox/build/cache.go -C 8
> sed -n '260,380p' internal/sandbox/build/cache.go
> rg -n --glob '!vendor/**' "DiffCache.*TTL|NewDiffStore|diffStore|cacheTTL|BuildCache" internal/sandbox/template internal/cfg main.go -C 8 | head -400`
>
> - Ran `sed -n '1,125p' internal/sandbox/template/cache.go
> rg -n "buildCacheTTL|buildCacheDelayEviction" internal/sandbox/template -C 5
> sed -n '1,140p' ../shared/pkg/utils/wait_map_test.go 2>/dev/null || true
> nl -ba ../shared/pkg/utils/wait_map.go
> nl -ba internal/sandbox/block/chunk.go | sed -n '65,95p;165,235p'`
>
> </details>
>
> 已经定位到一个比“worker 太多”更直接的热路径缺陷：每次 4 KiB rootfs 读都会重新构造一个 `StorageDiff` 候选（随机 cache 路径、`SetOnce`、channel、闭包和多段字符串），然后才去 25 小时 TTL 的共享缓存里发现“其实已有对象”。浏览器加载 Chrome 和动态库会产生海量 4 KiB 读，这批无效短命对象正好会形成火焰图里的 GC 主体；并发度改动则把它集中放大。我再做一次编译/测试验证，并整理按影响排序的结论和验证开关。
>
> <details><summary>Explored 14 files, ran 3 commands</summary>
>
> - Ran `go test ./internal/sandbox/block ./internal/sandbox/build ./internal/sandbox/uffd/userfaultfd ./internal/sandbox/nbd`
>
> - Ran `go test ./internal/sandbox/build -run '^$' -gcflags='-m=2' 2>&1 | rg 'build.go:118|build.go:119|storage_diff.go:42|storage_diff.go:57|storage_diff.go:59|cache.go:101|escapes to heap' | rg 'internal/sandbox/build/(build|storage_diff|cache)\.go' | head -220`
>
> - Ran `go test ./internal/sandbox/nbd ./internal/sandbox/block -run '^$' -gcflags='-m=2' 2>&1 | rg 'internal/sandbox/(nbd/dispatch|block/(cache|overlay|chunk))\.go' | rg 'make\(\[\]byte|BlocksOffsets|BytesNotAvailableError|escapes to heap|cannot inline' | head -260`
>
> <details><summary>Explored 14 files</summary>
>
> - Read `./Makefile`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/iac/provider-gcp/nomad/jobs/orchestrator.hcl`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/helm/templates/orchestrator.yaml`
> - Read `./internal/sandbox/template/cache.go`
> - Read `./internal/sandbox/block/metrics/main.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/telemetry/traces.go`
> - Read `./internal/sandbox/uffd/uffd.go`
> - Read `./internal/sandbox/sandbox.go`
> - Read `./internal/sandbox/uffd/userfaultfd/userfaultfd.go`
> - Read `./internal/sandbox/block/chunk.go`
> - Read `~/桌面/e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/storage/storage_cache_seekable.go`
> - Read `./internal/sandbox/nbd/dispatch.go`
> - Read `./internal/server/main.go`
> - Read `./internal/server/utils.go`
>
> </details>
>
> </details>
>
> <details><summary>Plan</summary>
>
> 已用调用链、当前工作区与基线 diff、编译器逃逸分析交叉验证：GC 由按块重复对象构造、NBD 常驻/临时大缓冲、预期 miss 的错误分配、逐页遥测和过高的多层并发共同触发；已形成验证与修复优先级。
>
> - [x] 梳理 orchestrator 目录结构与并发沙箱创建/渲染调用链
> - [x] 定位高频、大对象、长生命周期分配及 GC 触发条件
> - [x] 结合并发放大机制形成根因与证据链
> - [x] 给出验证方式与修复优先级
>
> </details>
</details>

结论：GC 不是由 Firecracker 客体内存本身触发，而是 orchestrator 在浏览器的高密度磁盘/内存访问下制造了大量 Go 堆对象；当前并发参数又把分配洪峰放大。最主要的代码问题是“每个 4 KiB rootfs 块都重复构造缓存对象”。

### 根因排序

1. 每个块读取都创建一次无效 `StorageDiff`

[rootfs 读取入口](./e2b/e2b-infra/e2b-infra-2026.09/packages/orchestrator/internal/sandbox/build/build.go:40)最终都会调用 [`getBuild()`](./e2b/e2b-infra/e2b-infra-2026.09/packages/orchestrator/internal/sandbox/build/build.go:118)。

它没有先按 key 查询缓存，而是每次都执行 [`newStorageDiff()`](./e2b/e2b-infra/e2b-infra-2026.09/packages/orchestrator/internal/sandbox/build/storage_diff.go:42)，创建：

- `StorageDiff`
- 随机 cache 文件名和多段字符串
- `SetOnce`
- channel、`sync.OnceFunc` 闭包
- TTL cache 参数

然后才调用 [`DiffStore.Get()`](./e2b/e2b-infra/e2b-infra-2026.09/packages/orchestrator/internal/sandbox/build/cache.go:101)，发现真正的对象早已在 25 小时缓存中。

rootfs block size 是 4 KiB，Chrome 启动、动态库加载、字体和页面资源读取会让这个路径执行几十万甚至数百万次。50 并发时，这些候选对象全部很快死亡，形成典型的 GC allocation storm。

编译器逃逸分析也确认 `StorageDiff`、`SetOnce`、channel 闭包、UUID 字符串等全部逃逸到堆上。

2. 每个沙箱常驻约 16 MiB Go 堆 NBD buffer

每个 NBD 设备默认建立 4 条连接；每条连接的 [`Dispatch.Handle()`](./e2b/e2b-infra/e2b-infra-2026.09/packages/orchestrator/internal/sandbox/nbd/dispatch.go:125)分配一个 4 MiB buffer，生命周期与沙箱一致：

- 单沙箱：`4 × 4 MiB = 16 MiB`
- 同时启动 30 个：约 `480 MiB`
- 运行 50 个：约 `800 MiB`

此外每个 NBD read 又在 [`cmdRead()`](./e2b/e2b-infra/e2b-infra-2026.09/packages/orchestrator/internal/sandbox/nbd/dispatch.go:234)创建：

- `make([]byte, request.Length)`
- 一个 channel
- 两层 goroutine 和闭包

浏览器渲染是 rootfs I/O 密集型负载，因此既抬高 live heap，又产生大量临时 buffer。

3. 正常 cache miss 被实现成逐块错误对象

[`Overlay.ReadAt()`](./e2b/e2b-infra/e2b-infra-2026.09/packages/orchestrator/internal/sandbox/block/overlay.go:31)按 4 KiB 遍历块。对于未写入的 COW 块，本应是正常分支，但当前路径会：

- 创建 `BytesNotAvailableError`
- 在 [`Cache.ReadAt()`](./e2b/e2b-infra/e2b-infra-2026.09/packages/orchestrator/internal/sandbox/block/cache.go:161)中用 `fmt.Errorf` 再包装
- 用 `errors.As` 解包
- `BlocksOffsets()` 每次重新分配 `[]int64`

逃逸分析确认 `BytesNotAvailableError`、`errors.As` 参数和 `sync.Map.Store` 的 key/value 都进入堆。也就是说，读取正常的只读 rootfs 数据会持续制造错误对象。

4. 逐页遥测进一步放大分配

每次 [`FullFetchChunker.Slice()`](./e2b/e2b-infra/e2b-infra-2026.09/packages/orchestrator/internal/sandbox/block/chunk.go:124)都创建 stopwatch，并记录 histogram、bytes、count 三次指标。

内存预取则为每一页创建 [`prefault page` span](./e2b/e2b-infra/e2b-infra-2026.09/packages/orchestrator/internal/sandbox/uffd/userfaultfd/userfaultfd.go:279)，而 tracer 配置为 [`AlwaysSample`](./e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/telemetry/traces.go:43)。这会产生 span、属性数组和 BatchSpanProcessor 队列对象。

5. 多层并发参数发生叠乘

当前工作区相对 tag 基线有这些改动：

- 同时启动数：3 → 30
- 内存预取 fetch worker：16 → 32
- copy worker：8 → 16
- 超限请求：立即失败 → 最长等待 300 秒

部署文件也明确设置了 `MAX_STARTING_INSTANCES_PER_NODE=30`。

因此 30 个同时启动的沙箱，仅预取就可能同时产生：

- 960 个 fetch worker
- 480 个 copy worker
- 合计约 1440 个 worker

UFFD 的 page-fault 上限还是“每沙箱 4096”，不是节点全局上限，理论节点上限可达到 `30 × 4096` 个在途任务。

50 个请求中剩余 20 个还会在 semaphore 中排队，所以测得的平均时间同时包含 GC slowdown 和队列等待。

6. 冷缓存时还有 4–8 MiB/块的大对象突发

共享 chunk cache miss 时，[`cachedSeekable.ReadAt()`](./e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/storage/storage_cache_seekable.go:111)会复制一份约 4 MiB 的 `shadowBuff` 并异步写回，且该路径没有全局并发限制。

若启用 streaming chunker，[`OpenRangeReader()`](./e2b/e2b-infra/e2b-infra-2026.09/packages/shared/pkg/storage/storage_cache_seekable.go:135)先分配 4 MiB buffer，关闭时再复制一份，峰值约 8 MiB/冷块。

### 为什么会从 2 秒变成 20 秒

高并发启动产生大量短命堆对象 → GC 周期急剧增加 → UFFD page fault 和 NBD rootfs 响应线程承担 GC assist → Firecracker/Chrome 等待缺页和磁盘响应 → 沙箱启动更慢、并发任务存活更久 → 堆和 goroutine 继续增加。

这是一个正反馈环；火焰图中 GC 超过 50%与上述路径吻合。

### 建议处理顺序

1. 将 `DiffStore.Get` 改为按 key 先查、仅 miss 时通过 lazy factory 创建 `StorageDiff`。这是最重要的根治项。
2. 临时把节点启动并发降到 5–10，恢复背压，避免同时制造分配洪峰。
3. 将 NBD connections 从 4 降到 1 做 A/B；50 沙箱的常驻 Go 堆理论上可直接下降约 600 MiB。
4. 复用 NBD request buffer，去掉每个 read 的双层 goroutine/channel。
5. 正常 cache miss 不再走 `error + fmt.Errorf + errors.As`；改为 `(data, found, err)`。
6. 用整数循环替代 `BlocksOffsets()` 的临时切片，并用 bitset/分片 bitmap 替代逐 4 KiB `sync.Map`。
7. 聚合或采样逐页指标，移除每页 `prefault` span；对 UFFD/prefetch 增加节点级并发限制。
8. 给共享缓存异步写回增加有界队列，避免多个 4 MiB buffer 同时滞留。

`GOGC` 或 `GOMEMLIMIT`只能作为保护措施，不能解决这些分配源。建议下一次 profile 同时查看 `alloc_space`；预计热点会出现 `build.newStorageDiff`、`utils.NewSetOnce`、`id.Generate`、`nbd.(*Dispatch).cmdRead`、`BytesNotAvailableError` 和 OpenTelemetry 聚合路径。

本次未修改代码。纯 Go build 包测试通过；UFFD/NBD 测试因当前环境缺少 userfaultfd 权限和 hugepage 资源而无法运行。

目标用量：243,318 tokens，耗时约 8 分 38 秒。
