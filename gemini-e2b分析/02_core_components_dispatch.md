# 核心组件解析：Dispatch 与并发控制

## 目录
- [1. Dispatch 是什么](#1-dispatch-是什么)
- [2. 锁是什么级别的](#2-锁是什么级别的)
  - [2.1 竞争协程数量](#21-竞争协程数量)
- [3. Handle 的调用与阻塞机制](#3-handle-的调用与阻塞机制)
  - [3.1 Handle 什么时候调用](#31-handle-什么时候调用)
  - [3.2 Handle 本身在哪里阻塞](#32-handle-本身在哪里阻塞)
  - [3.3 data := make(\[\]byte, length) 不在 Handle goroutine 中](#33-data--makebyte-length-不在-handle-goroutine-中)
  - [3.4 分配前有没有阻塞](#34-分配前有没有阻塞)
  - [3.5 假设有 1000 个 NBD READ](#35-假设有-1000-个-nbd-read)
  - [3.6 “1000 个页面缺失”不一定产生1000个 NBD 请求](#36-1000-个页面缺失不一定产生1000个-nbd-请求)
  - [3.7 和 GC 超过 50% 的关系](#37-和-gc-超过-50-的关系)
- [4. d.prov.ReadAt 解析](#4-dprovreadat-解析)

## 1. Dispatch 是什么

`Dispatch` 是 orchestrator 内部的一个“单条 NBD 连接请求处理器”。

它负责在 Linux NBD 内核驱动和实际 rootfs 后端之间转发请求：

```text
沙箱 / Firecracker
    ↓ 读写 rootfs
Linux /dev/nbdX
    ↓ Unix Socket
Dispatch
    ↓ ReadAt / WriteAt
Overlay + 模板 rootfs
```

它主要完成三件事：

1. `Handle()` 从 Unix Socket 读取并解析 NBD 请求头，识别 Read、Write、Trim 等命令。
2. `cmdRead()`、`cmdWrite()` 调用后端 `Provider` 读取或写入 rootfs。
3. `writeResponse()` 将结果通过 Socket 返回给 Linux NBD 驱动。

一个沙箱不是只有一个 `Dispatch`。默认每个 NBD 设备建立 4 条 Socket 连接，每条连接各有一个 `Dispatch`：

```text
一个沙箱
  └── 一个 /dev/nbdX
       ├── Dispatch 1
       ├── Dispatch 2
       ├── Dispatch 3
       └── Dispatch 4
```

因此：

- 16 个沙箱默认有 64 个 `Dispatch`。
- 32 个沙箱默认有 128 个 `Dispatch`。

本次问题中的 `Dispatch.cmdRead.func1`，就是某条 NBD 连接处理读请求时，为请求创建数据缓冲区并读取 rootfs 的内部函数。它不是 Firecracker 自身的组件，而是 orchestrator 实现的用户态 NBD 服务端。

## 2. 锁是什么级别的

`Dispatch.writeResponse` 中的 `writeLock` 是“单个 `Dispatch` 实例级别”的锁，也就是“单条 NBD Socket 连接级别”，不是全局锁，也不是沙箱级锁。

默认一个沙箱有 4 条 NBD 连接，每条连接对应：

```text
1 个 Dispatch
1 个 writeLock
1 个 Socket
```

因此 16 个沙箱默认有：

```text
16 沙箱 × 4 Dispatch/沙箱 = 64 个独立 writeLock
```

不同沙箱、不同 Dispatch 之间不会竞争同一把锁。

### 2.1 竞争协程数量

竞争锁的不是沙箱数量，而是：

```text
竞争数量 = 分配给该连接，并且已经运行到 writeResponse 的在途请求数
```

代码没有对这个数量做限制。内层执行 `ReadAt` 的协程不直接竞争 `writeLock`，真正竞争锁的是外层响应协程，以及写请求的响应协程。

假设每条连接同时有 `K` 个响应到达 `writeResponse`：

```text
总调用者：64 × K
持有锁执行写入：最多 64 个
等待锁：64 × (K - 1)
```

这把锁不仅保护 `responseHeader`，还覆盖整个 payload 写入。这是为了防止两个 NBD 响应交叉。但代价是：如果 `fp.Write(chunk)` 阻塞，后面的所有响应协程都会等待，并继续持有各自的 `data` 缓冲区。

准确结论是：
- 16 个沙箱默认不存在“所有协程竞争一把锁”。
- 存在 64 组独立的锁竞争。
- 每组竞争者数量没有代码层面的上限。
- 全系统最多可以有64个协程同时执行 Socket 响应写入，其余到达这里的协程等待并持有内存。

## 3. Handle 的调用与阻塞机制

### 3.1 Handle 什么时候调用

调用链是：

```text
NBDProvider.Start
  → DirectPathMount.Open
    → 为每条 NBD connection 创建 socketpair
      → NewDispatch
        → 启动 goroutine
          → dispatch.Handle(ctx)
```

旧版默认值是 4，因此：

| 沙箱数 | Handle goroutine 数 |
|---:|---:|
| 1 | 4 |
| 16 | 64 |
| 32 | 128 |

这些 goroutine 刚启动时，`Handle` 会先分配 4 MiB解析缓冲区，然后阻塞等待内核 NBD 请求。

### 3.2 Handle 本身在哪里阻塞

`Handle` 执行过程：

```text
Handle 启动
  → make 4 MiB dispatch buffer
  → d.fp.Read()
  → 阻塞等待 NBD socket 请求
```

直到 Linux NBD 驱动发送 READ/WRITE 请求。

### 3.3 data := make([]byte, length) 不在 Handle goroutine 中

准确的执行关系是：

```text
Handle goroutine
  → 从 socket 读到 NBD READ header
  → cmdRead()
      → 启动一个新的 worker goroutine
      → cmdRead 立即返回
  → Handle 继续解析下一个请求

worker goroutine
  → make(chan error, 1)
  → make([]byte, length)
  → 再启动一个 prov.ReadAt goroutine
  → 等待 ReadAt
  → 等待 writeLock
  → 写回 NBD 响应
```

一个固定的 Handle goroutine不断读取请求；每个 READ 请求由 `cmdRead` 启动新的 worker，worker 很快执行到 `make([]byte, length)`。

### 3.4 分配前有没有阻塞

在 `data := make([]byte, length)` 之前没有信号量、队列容量、并发上限或后端 IO 等待。请求一旦进入 `cmdRead`，通常会很快产生 data 分配。这正是容易造成 allocation burst 和 GC 压力的地方。

### 3.5 假设有 1000 个 NBD READ

如果内核已经向这些 socket 提交了 1000 个独立的 NBD READ，并且 Handle 读取请求的速度快于后端完成速度，代码没有并发上限：

- Handle 数仍然是固定的，例如 16 沙箱默认 64 个。
- 可能出现约 1000 个外层 `performRead` goroutine。
- 每个请求又启动一个 `prov.ReadAt` goroutine。
- 在 ReadAt 尚未结束时，最多可能接近 2000 个 READ 相关 goroutine。
- 每个请求都会单独执行一次 `data := make([]byte, length)`。

### 3.6 “1000 个页面缺失”不一定产生1000个 NBD 请求

需要区分两种 page fault：

1. **Firecracker 内存快照缺页**：走的是 UFFD，不会调用 NBD `Dispatch.Handle/cmdRead`。UFFD 的并发上限是 4096。
2. **Guest 文件系统读取触发的缺页**：例如浏览器启动时。guest kernel 会进行请求合并和 readahead。一个 NBD READ 的 `length` 可能覆盖多个 4 KiB 页面，因此 1000 个 guest 页面缺失不等于必然产生 1000 个 NBD READ。

### 3.7 和 GC 超过 50% 的关系

放大链路：

```text
大量 NBD READ
  → 每请求 make(data)
  → 每请求两个异步 goroutine
  → ReadAt 等待远端/缓存
  → 响应等待每连接 writeLock
  → data buffer 长时间存活
  → heap 和分配速率上升
  → GC CPU 占比上升
```

不是1000个页面就产生1000个Handle；Handle数量由NBD连接数决定。真正可能随请求数量膨胀的是`cmdRead`及其内部`ReadAt` goroutine。该实现没有READ并发和在途内存限制，这与观察到的大量分配和GC压力高度吻合。

## 4. d.prov.ReadAt 解析

```go
_, err := d.prov.ReadAt(ctx, data, int64(from))
```

这行代码的作用是：从 NBD 后端存储读取指定位置的数据，写入刚刚分配的 `data` 缓冲区。
这里的 `d.prov` 实际是 `Overlay`（`block.Overlay`）。

完整读取链路为：

```text
NBD READ
  → Dispatch.cmdRead
  → d.prov.ReadAt
  → Overlay.ReadAt
      → 先读本地 cache
      → cache 没有对应 block
      → 调用下层 rootfs.ReadAt
      → 可能从本地文件或远端存储拉取
```

这是 READ 路径最主要的 I/O 阻塞点。时序是：先完成内存分配，然后开始可能很慢的 I/O，等待读取完成，再等待写锁并回写内核。
这意味着在后端读取较慢时，每个在途 READ 都会一直持有自己的 `data`，内存不能及时释放，增加 GC 压力。
