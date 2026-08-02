# 分析沙箱并发性能瓶颈

> 项目地址：/home/zry/桌面/e2b/e2b-infra/e2b-infra-2026.09
> 问题：
> 创建沙箱，运行浏览器渲染，单沙箱用时2s，16沙箱时，平均每个沙箱用时10s，显著增加。
> 定位：32并发沙箱时，
> 1、抓firecracker进程，发现采样数较少，接近2s的采样数量（perf record）
> 2、抓orchestrator火焰图，发现超过50%的时间用于GC。
> 3、查看内存分配情况，使用top 20,发现有大量分配内存来自于：orchestrator/internal/sandbox/nbd.(*Dispatch).cmdRead.fun1
> 目标：阅读代码，分析原因。

<details><summary>分析记录</summary>

> 我会先建立这个排查目标，然后从仓库约束、`nbd.Dispatch.cmdRead` 的实现和调用链入手，把内存分配、GC 与并发退化串起来；当前先做只读分析，不改代码。
>
> 已经定位到热点实现：`cmdRead` 会为每个 NBD 读请求启动 goroutine。接下来我会逐段核对它在每次请求中分配了什么、并发数如何受控，以及后端读取是否又产生额外复制。
>
> 静态编译的逃逸分析已确认：热点函数名 `cmdRead.func1` 正对应 `performRead`，其中 `make([]byte, length)` 明确逃逸到堆；同时每个请求还会分配 channel、闭包并创建两层 goroutine。现在我在核对这些瞬时对象为何会在高并发下积压，而不只是“总分配量大”。
>
> 结论已经闭环：这是 orchestrator 用户态 NBD 读路径的“高分配率 + 无界在途请求 + 响应串行化”共同造成的 GC/排队放大；另外还有每沙箱默认 16 MiB 的常驻 Dispatch 缓冲，以及每个 4 KiB COW miss 走 error/`errors.As` 带来的小对象 churn。Firecracker 采样少与它等待 NBD I/O 的现象一致。
</details>

## 目录
- [1. 结论](#1-结论)
- [2. 代码证据](#2-代码证据)
  - [2.1 每次读请求都按请求长度分配新堆内存](#21-每次读请求都按请求长度分配新堆内存)
  - [2.2 在途请求没有任何并发上限](#22-在途请求没有任何并发上限)
  - [2.3 读取并发，但响应写入串行](#23-读取并发但响应写入串行)
  - [2.4 默认每个沙箱又把并发放大 4 倍](#24-默认每个沙箱又把并发放大-4-倍)
  - [2.5 每个 Dispatch 还有 4 MiB 常驻堆缓冲](#25-每个-dispatch-还有-4-mib-常驻堆缓冲)
  - [2.6 COW cache miss 走异常路径，制造大量小对象](#26-cow-cache-miss-走异常路径制造大量小对象)
- [3. 为什么是非线性退化](#3-为什么是非线性退化)
- [4. 建议的验证顺序](#4-建议的验证顺序)

## 1. 结论

主要瓶颈在 orchestrator 的用户态 NBD 读路径：

> 每个 NBD 读请求都新建堆缓冲区和两层 goroutine，同时代码没有限制在途请求数；大量请求完成后又在 socket 写锁处排队，导致缓冲区存活时间变长。并发沙箱数增加后，分配速率和存活堆迅速放大，引发频繁 GC 和 GC assist，最终形成排队—GC—更慢排队的正反馈。

这能同时解释：

- `cmdRead.func1` 位于内存分配 Top。
- orchestrator 超过 50% CPU 时间用于 GC。
- Firecracker 的有效 CPU 采样仍接近约 2 秒，但墙钟时间达到约 10 秒——剩余时间主要在等待 NBD 根盘 I/O。

## 2. 代码证据

### 2.1 每次读请求都按请求长度分配新堆内存

`dispatch.go` 中 `performRead` 的实现：

```go
performRead := func(...) error {
    errchan := make(chan error, 1)
    data := make([]byte, length)

    go func() {
        _, err := d.prov.ReadAt(ctx, data, int64(from))
        errchan <- err
    }()
    ...
}
```

Go 编译器的逃逸分析明确输出：

```text
make([]byte, length) escapes to heap in (*Dispatch).cmdRead.func1
func literal escapes to heap
```

所以 pprof 中的 `cmdRead.func1` 就是这里的 `performRead`。对于 guest 读取的每一个字节，orchestrator 至少会产生同等数量的瞬时堆分配：

```text
一次启动的最低数据缓冲分配量 ≈ guest 通过 NBD 读取的总字节数
```

此外，每个请求还会产生：

- 一个 buffered channel。
- 一个 `performRead` 闭包。
- 一个负责异步响应的外层 goroutine。
- 一个执行 `ReadAt` 的内层 goroutine。

### 2.2 在途请求没有任何并发上限

`dispatch.go` 收到请求后直接调用 `cmdRead`，而 `cmdRead` 启动 goroutine 后立即返回。

`pendingResponses` 只是关机时等待请求结束，不是限流器。代码自身没有 worker pool、semaphore 或最大 pending 数，实际上限只能依赖内核 NBD 队列和 socket 背压。

### 2.3 读取并发，但响应写入串行

`dispatch.go` 中：

```go
d.writeLock.Lock()
defer d.writeLock.Unlock()

d.fp.Write(d.responseHeader)
d.fp.Write(chunk)
```

多个读请求可以并发完成，但同一连接的响应必须等待 `writeLock`。等待写锁的 goroutine会继续持有自己的 `data` 缓冲区。

因此下游稍微变慢时，会出现：

```text
写 socket 变慢
  → 等待 writeLock 的请求增加
  → 更多 data 缓冲区同时存活
  → GC 压力增加
  → orchestrator 进一步变慢
```

### 2.4 默认每个沙箱又把并发放大 4 倍

为一个 NBD 设备创建多条连接，默认值是 4：

因此：

- 16 个沙箱：默认 64 个 Dispatch。
- 32 个沙箱：默认 128 个 Dispatch。

### 2.5 每个 Dispatch 还有 4 MiB 常驻堆缓冲

`dispatch.go`：

```go
buffer := make([]byte, 4*1024*1024)
```

编译器也确认该缓冲区逃逸到堆。按默认 4 条连接计算：

- 每沙箱固定约 16 MiB。
- 16 沙箱固定约 256 MiB。
- 32 沙箱固定约 512 MiB。

这还没有计算正在处理的读请求数据、channel、闭包和 goroutine。

### 2.6 COW cache miss 走异常路径，制造大量小对象

正常的只读 rootfs 数据并不存在于沙箱私有 COW cache 中。`overlay.go` 会逐 block 查询 cache；未命中时：

- `cache.go` 返回 `BytesNotAvailableError`。
- `Cache.ReadAt` 用 `fmt.Errorf("%w")` 包装。
- `Overlay.ReadAt` 再通过 `errors.As` 判断。

编译器确认 `BytesNotAvailableError` 和 `errors.As` 的目标对象会逃逸到堆。对于通常为 4 KiB 的 rootfs block，这意味着正常读路径每 4 KiB 都可能制造多枚小对象。它不是 `alloc_space` 最大来源，但会显著增加对象数量和 GC/调度开销。

## 3. 为什么是非线性退化

单沙箱时，分配很快被回收，socket 写入也很少排队。

并发增加后：

1. NBD 连接数量按沙箱数 ×4 增长。
2. 浏览器同时产生大量根盘读请求。
3. 每个请求分配独立缓冲和两层 goroutine。
4. GC 抢占 orchestrator CPU，并向业务 goroutine施加 GC assist。
5. provider 读取和 socket 响应变慢。
6. 请求缓冲存活更久，进一步增加 GC 压力。

因此 16 并发不是简单保持 2 秒，而可能跨过系统的排队拐点后膨胀到约 10 秒。

Firecracker 的根盘确实指向这个 NBD 设备，所以 Firecracker 等待块设备响应时不会产生多少 CPU perf 样本，现有采样结果与上述结论吻合。

## 4. 建议的验证顺序

1. 记录 `cmdLength` 分布、每个 Dispatch 的 pending 数和总 goroutine 数。
2. 同时观察 `alloc_bytes/sec`、`HeapAlloc`、GC assist CPU 和 NBD 请求延迟。
3. A/B 将 `nbd-connections-per-device` 从 4 调到 1；如果 GC 和尾延迟明显下降，可直接确认并发放大关系。
4. 再 A/B 为 `cmdRead` 增加有限 worker/in-flight 上限并复用 worker 缓冲区。
5. 缩小每连接 4 MiB 的接收缓冲，并将 COW miss 从 error 控制流改成 `(data, found, err)`。

调整 `GOGC` 可能暂时降低 GC 频率，但只会用更多内存换时间，不会消除上述根因。
