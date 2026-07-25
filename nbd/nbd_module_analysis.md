# NBD (Network Block Device) 内核模块详解

## 一、模块概述

### 1.1 什么是 NBD

NBD (Network Block Device) 是一个 Linux 内核模块，它将远程服务器上的块设备通过网络(TCP/Unix Socket)映射为本地块设备。客户端内核驱动负责将本地的块 I/O 请求封装为 NBD 协议报文，发送给运行在用户空间的服务端（如 `nbd-server`），并接收服务端的响应。

```
用户空间应用程序 (dd, mount, fdisk, ...)
        |
        v
    块层 (block layer) / blk-mq
        |
        v
   NBD 内核驱动 (nbd.ko)     <--- TCP/Unix Socket --->   NBD 服务端 (nbd-server)
        |                                                    |
    /dev/nbd0                                            /path/to/export.img
```

### 1.2 源码位置

| 文件 | 说明 |
|------|------|
| `drivers/block/nbd.c` | 主驱动源码 (~2764 行) |
| `include/uapi/linux/nbd.h` | NBD 协议头定义 (ioctl、请求/响应结构) |
| `include/uapi/linux/nbd-netlink.h` | Netlink 配置接口定义 |
| `include/trace/events/nbd.h` | Tracepoint 追踪事件 |

### 1.3 模块参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `nbds_max` | int | 16 | 初始化时创建的 NBD 设备数量 |
| `max_part` | int | 16 | 每个设备支持的最大分区数 |

---

## 二、核心数据结构

### 2.1 struct nbd_device — NBD 设备实例

```c
struct nbd_device {
    struct blk_mq_tag_set tag_set;      // blk-mq 标签集，管理 inflight 请求
    int index;                          // 设备索引号 (决定设备名 nbd%d)
    refcount_t config_refs;             // 配置引用计数
    refcount_t refs;                    // 设备引用计数
    struct nbd_config *config;          // 当前配置（NULL 表示未配置）
    struct mutex config_lock;           // 配置互斥锁
    struct gendisk *disk;               // 内核通用磁盘对象
    struct workqueue_struct *recv_workq;// 接收工作队列（每个设备一个）
    struct work_struct remove_work;     // 异步设备删除工作项
    struct list_head list;              // 链表节点（模块卸载用）
    struct task_struct *task_setup;     // 正在设置设备的进程 (ioctl 模式)
    unsigned long flags;                // 设备标志
    pid_t pid;                          // 附加进程 PID (0 表示未附加)
    char *backend;                      // 后端标识（netlink 重配置校验用）
};
```

### 2.2 struct nbd_config — 设备配置

```c
struct nbd_config {
    u32 flags;                          // 服务端能力标志 (NBD_FLAG_*)
    unsigned long runtime_flags;        // 运行时状态位图 (NBD_RT_*)
    u64 dead_conn_timeout;              // 全连接死亡后的等待超时

    struct nbd_sock **socks;            // 连接指针数组
    int num_connections;                // 总连接数
    atomic_t live_connections;          // 活连接计数
    wait_queue_head_t conn_wait;        // 等待连接恢复的等待队列

    atomic_t recv_threads;              // 活跃接收线程数
    wait_queue_head_t recv_wq;          // 等待所有接收线程结束
    unsigned int blksize_bits;          // 块大小 (2 的幂次)
    loff_t bytesize;                    // 设备总字节数
};
```

### 2.3 struct nbd_sock — 单个网络连接

```c
struct nbd_sock {
    struct socket *sock;                // 内核 socket 对象
    struct mutex tx_lock;               // 发送互斥锁
    struct request *pending;            // 待发送请求 (分片发送时)
    int sent;                           // 已发送字节数
    bool dead;                          // 连接是否已死亡
    int fallback_index;                 // 故障转移目标连接索引
    int cookie;                         // 连接 cookie (每次重连递增)
    struct work_struct work;            // 延迟发送工作项
};
```

### 2.4 struct nbd_cmd — 每个请求的 NBD 命令上下文

```c
struct nbd_cmd {
    struct nbd_device *nbd;     // 反向引用
    struct mutex lock;          // 命令锁
    int index;                  // 发送使用的连接索引
    int cookie;                 // 发送时的 socket cookie
    int retries;                // 超时重试次数
    blk_status_t status;        // 完成状态
    unsigned long flags;        // NBD_CMD_* 状态标志
    u32 cmd_cookie;             // 命令级 cookie (嵌入 handle 检测重复)
};
```

每个 blk-mq 请求携带一个 `nbd_cmd` 作为 PDU（Protocol Data Unit），通过 `blk_mq_rq_to_pdu()` 获取。

---

## 三、支持的功能与操作

### 3.1 块 I/O 操作映射

| blk-mq 请求类型 | NBD 命令 | 说明 |
|----------------|----------|------|
| `REQ_OP_READ` | `NBD_CMD_READ` (0) | 读数据 |
| `REQ_OP_WRITE` | `NBD_CMD_WRITE` (1) | 写数据 |
| `REQ_OP_DISCARD` | `NBD_CMD_TRIM` (4) | 释放/裁剪块 |
| `REQ_OP_FLUSH` | `NBD_CMD_FLUSH` (3) | 刷新写缓存 |
| `REQ_OP_WRITE_ZEROES` | `NBD_CMD_WRITE_ZEROES` (6) | 写零块 |

### 3.2 服务端能力标志 (NBD_FLAG_*)

| 标志 | 位 | 说明 |
|------|----|------|
| `NBD_FLAG_HAS_FLAGS` | bit 0 | 服务端支持标志协商 |
| `NBD_FLAG_READ_ONLY` | bit 1 | 设备只读 |
| `NBD_FLAG_SEND_FLUSH` | bit 2 | 支持 FLUSH 命令 |
| `NBD_FLAG_SEND_FUA` | bit 3 | 支持 FUA (强制单元访问) |
| `NBD_FLAG_ROTATIONAL` | bit 4 | 旋转介质 (影响 IO 调度器) |
| `NBD_FLAG_SEND_TRIM` | bit 5 | 支持 TRIM/DISCARD 命令 |
| `NBD_FLAG_SEND_WRITE_ZEROES` | bit 6 | 支持 WRITE_ZEROES 命令 |
| `NBD_FLAG_CAN_MULTI_CONN` | bit 8 | 支持多连接 |

### 3.3 客户端行为标志 (NBD_CFLAG_*)

| 标志 | 位 | 说明 |
|------|----|------|
| `NBD_CFLAG_DESTROY_ON_DISCONNECT` | bit 0 | 断开连接时销毁 /dev/nbdX 设备 |
| `NBD_CFLAG_DISCONNECT_ON_CLOSE` | bit 1 | 最后一个打开者关闭时断开连接 |

### 3.4 请求命令标志 (NBD_CMD_FLAG_*)

| 标志 | 位 | 说明 |
|------|----|------|
| `NBD_CMD_FLAG_FUA` | bit 16 | 强制单元访问, 数据必须写入持久存储 |
| `NBD_CMD_FLAG_NO_HOLE` | bit 17 | WRITE_ZEROES 时不打洞, 实际写入零 |

---

## 四、ioctl 接口 (传统模式)

传统 `nbd-client` 使用 ioctl 与内核驱动交互。ioctl 命令字使用魔数 `0xab`。

### 4.1 操作序列

典型的 nbd-client 操作流程：

```
1. open("/dev/nbd0")                    → nbd_open()
   创建 nbd_config, 初始化数据结构

2. ioctl(NBD_SET_SOCK, socket_fd)       → nbd_add_socket()
   将 TCP/Unix socket 绑定到设备

3. ioctl(NBD_SET_BLKSIZE, 512)          → nbd_set_size()
   设置块大小

4. ioctl(NBD_SET_SIZE, 10737418240)     → nbd_set_size()
   设置设备大小 (10GB)

5. ioctl(NBD_SET_FLAGS, ...)            → 设置服务端标志
   可选: 设置 flags

6. ioctl(NBD_DO_IT)                     → nbd_start_device_ioctl()
   启动设备, 当前线程阻塞等待设备断开

7. close()                              → nbd_release()
   可以释放或断开
```

### 4.2 完整 ioctl 列表

| ioctl | 功能 | 实现函数 | 说明 |
|-------|------|---------|------|
| `NBD_SET_SOCK` | 绑定 socket | `nbd_add_socket(nbd, arg, false)` | 添加 TCP/Unix 连接 |
| `NBD_SET_BLKSIZE` | 设置块大小 | `nbd_set_size()` | 保持当前大小, 仅改块大小 |
| `NBD_SET_SIZE` | 设置字节大小 | `nbd_set_size()` | 设置设备容量 |
| `NBD_SET_SIZE_BLOCKS` | 按块数设大小 | `nbd_set_size()` | 自动换算为字节 |
| `NBD_DO_IT` | 启动设备 | `nbd_start_device_ioctl()` | 阻塞等待断开 |
| `NBD_CLEAR_SOCK` | 清除连接 | `nbd_clear_sock_ioctl()` | 关闭 socket + 清队列 |
| `NBD_CLEAR_QUE` | 清空队列 | (兼容, 无实际操作) | 已废弃 |
| `NBD_PRINT_DEBUG` | 打印调试 | (兼容, 无实际操作) | 已废弃 |
| `NBD_DISCONNECT` | 断开连接 | `nbd_disconnect()` | 优雅断开 |
| `NBD_SET_TIMEOUT` | 设置超时 | `nbd_set_cmd_timeout()` | 秒为单位 |
| `NBD_SET_FLAGS` | 设置标志 | 直接赋值 `config->flags` | 服务端能力标志 |

---

## 五、Netlink 接口 (现代模式)

### 5.1 Generic Netlink 架构

```
Family: "nbd" (version 0x1)
Multicast Group: "nbd_mc_group"
```

### 5.2 Netlink 命令

| 命令 | 功能 | 实现函数 |
|------|------|---------|
| `NBD_CMD_CONNECT` | 配置并启动设备 | `nbd_genl_connect()` |
| `NBD_CMD_DISCONNECT` | 断开设备 | `nbd_genl_disconnect()` |
| `NBD_CMD_RECONFIGURE` | 重配置设备 | `nbd_genl_reconfigure()` |
| `NBD_CMD_STATUS` | 查询设备状态 | `nbd_genl_status()` |
| `NBD_CMD_LINK_DEAD` | 连接死亡通知 (多播) | `nbd_mcast_index()` |

### 5.3 Netlink 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `NBD_ATTR_INDEX` | u32 | 设备索引 (-1 表示自动分配) |
| `NBD_ATTR_SIZE_BYTES` | u64 | 设备字节大小 |
| `NBD_ATTR_BLOCK_SIZE_BYTES` | u64 | 块大小 |
| `NBD_ATTR_TIMEOUT` | u64 | 请求超时 (秒) |
| `NBD_ATTR_SERVER_FLAGS` | u64 | 服务端能力标志 |
| `NBD_ATTR_CLIENT_FLAGS` | u64 | 客户端行为标志 |
| `NBD_ATTR_SOCKETS` | nested | 嵌套的 socket FD 列表 |
| `NBD_ATTR_DEAD_CONN_TIMEOUT` | u64 | 全死亡后等待超时 |
| `NBD_ATTR_DEVICE_LIST` | nested | 设备列表 (STATUS 响应) |
| `NBD_ATTR_BACKEND_IDENTIFIER` | string | 后端标识 (重配置校验) |

### 5.4 connect 操作流程

```
1. nbd_genl_connect()
   - 解析 NBD_ATTR_INDEX (自动分配或指定)
   - 在 IDR 中查找或创建设备 (nbd_dev_add)
   - 分配并将初始化 config (nbd_alloc_and_init_config)
   
2. 设置设备参数
   - NBD_ATTR_SIZE_BYTES / NBD_ATTR_BLOCK_SIZE_BYTES → nbd_genl_size_set()
   - NBD_ATTR_TIMEOUT → nbd_set_cmd_timeout()
   - NBD_ATTR_DEAD_CONN_TIMEOUT → config->dead_conn_timeout
   - NBD_ATTR_SERVER_FLAGS → config->flags
   - NBD_ATTR_CLIENT_FLAGS → 解析 DESTROY_ON_DISCONNECT, DISCONNECT_ON_CLOSE
   
3. 添加 socket 连接
   - 遍历 NBD_ATTR_SOCKETS 嵌套属性
   - 对每个 socket fd 调用 nbd_add_socket(nbd, fd, true)
   
4. 创建 backend sysfs 属性 (如果有后端标识)
   
5. nbd_start_device()
   - 设置 blk-mq 硬件队列数 = 连接数
   - 启动所有接收线程 (recv_work)
   - 应用大小和块大小限制
   
6. 向调用了发送连接回复 (nbd_connect_reply)
   回复中包含分配的设备索引
```

### 5.5 disconnect 操作流程

```
1. nbd_genl_disconnect()
   - 通过 index 在 IDR 中查找设备
   - 增加 config_refs
   - 调用 nbd_disconnect_and_put()
     - nbd_disconnect(): 向服务端发送 NBD_CMD_DISC
     - sock_shutdown(): 关闭所有 socket
     - 唤醒 conn_wait (等待重连的请求)
     - flush_workqueue + nbd_clear_que: 完成所有 inflight 请求
     - 清除 NBD_RT_BOUND 标志
     - 释放 HAS_CONFIG_REF
```

### 5.6 reconfigure 操作流程

```
1. nbd_genl_reconfigure()
   - 通过 index 查找设备
   - 验证后端标识一致 (如果有)
   
2. 更新配置参数
   - nbd_genl_size_set() - 更新大小
   - nbd_set_cmd_timeout() - 更新超时
   - DEAD_CONN_TIMEOUT - 更新重连等待时间
   - CLIENT_FLAGS - 更新 DESTROY/DISCONNECT_ON_CLOSE
   
3. 如果有新的 socket fd:
   - 遍历 SOCKETS 列表
   - 对每个 fd 调用 nbd_reconnect_socket()
     - 找到第一个 dead 连接
     - 替换为新 socket
     - 递增 nsock->cookie (区分新旧连接)
     - 启动新的接收线程
     - 唤醒 conn_wait
```

### 5.7 status 查询

```
nbd_genl_status()
   - 如果 NBD_ATTR_INDEX == -1: 遍历所有设备, 返回全部状态
   - 如果指定 index: 返回单个设备状态
   - 每个设备返回: NBD_DEVICE_INDEX + NBD_DEVICE_CONNECTED (1/0)
```

---

## 六、IO 请求处理流程

### 6.1 请求发送路径

```
blk_mq_submit_bio()
    |
    v
nbd_queue_rq(hctx, bd)             <-- blk_mq_ops.queue_rq
    |
    +-- mutex_lock(&cmd->lock)       // 保护 cmd 状态
    +-- clear_bit(REQUEUED)
    |
    v
nbd_handle_cmd(cmd, hctx->queue_num)
    |
    +-- nbd_get_config_unlocked()    // 获取 config 引用
    +-- mutex_lock(&nsock->tx_lock)  // 获取连接发送锁
    |
    +-- 检查 nsock->dead ?
    |   |
    |   +-- 存活: 继续
    |   +-- 死亡: find_fallback()
    |       |
    |       +-- 有 fallback: 切换连接, goto again
    |       +-- 无 fallback: wait_for_reconnect()
    |           +-- 超时: 返回 IOERR
    |           +-- 恢复: goto again
    |
    +-- blk_mq_start_request(req)    // 记录开始时间
    +-- 检查 nsock->pending ?
    |   +-- 有 pending (其他请求): requeue 当前请求
    |
    v
nbd_send_cmd(nbd, cmd, index)
    |
    +-- 构造 nbd_request 包头:
    |   - magic = NBD_REQUEST_MAGIC
    |   - type   = req_to_nbd_cmd_type() | nbd_cmd_flags (FUA/NO_HOLE)
    |   - cookie = nbd_cmd_handle()  (cmd_cookie << 32 | tag)
    |   - from   = 偏移 (字节)
    |   - len    = 长度 (字节)
    |
    +-- sock_xmit(发送请求头, MSG_MORE for WRITE)
    |   |
    |   +-- 被信号中断:
    |       +-- sent > 0: nbd_sched_pending_work (异步完成)
    |       +-- sent = 0: 返回 RESOURCE (等待重试)
    |   +-- 发送失败:
    |       +-- nbd_mark_nsock_dead + requeue
    |
    +-- 如果是写请求: 遍历 bio 链, 逐段发送数据
    |   +-- 对每个 bio_vec 调用 sock_xmit
    |   +-- 最后一个段不设 MSG_MORE (触发 TCP 立即发送)
    |
    +-- nsock->pending = NULL
    +-- __set_bit(NBD_CMD_INFLIGHT)   // 标记命令已发送
```

### 6.2 响应接收路径

```
recv_work(work)                      <-- 每个连接一个工作项
    |
    v 循环:
nbd_read_reply(nbd, sock, &reply)    // 阻塞读取 16 字节包头
    |
    +-- __sock_xmit(recv, MSG_WAITALL)
    +-- 验证 magic == NBD_REPLY_MAGIC
    |
    v
nbd_handle_reply(nbd, index, &reply)
    |
    +-- 从 handle 解析 tag (低 32 位)
    +-- tag -> hwq -> set->tags -> request (反向查找)
    |
    +-- 多重验证:
    |   1. INFLIGHT 标志已设置
    |   2. cmd->index 匹配
    |   3. cmd_cookie 匹配 (防止重复/过时响应)
    |   4. cmd->status == BLK_STS_OK
    |   5. 未 REQUEUED (防止与超时路径竞争)
    |
    +-- 检查 reply->error:
    |   +-- error != 0: cmd->status = BLK_STS_IOERR
    |
    +-- 读请求: 从 socket 读取数据填充 bio
    |   +-- rq_for_each_segment: 遍历所有 bio_vec
    |   +-- sock_xmit(recv, MSG_WAITALL) 读取数据
    |
    v
返回 cmd (成功) 或 ERR_PTR (失败)

(在 recv_work 中)
    |
    +-- __test_and_clear_bit(NBD_CMD_INFLIGHT)
    +-- blk_mq_complete_request(rq) → nbd_complete_rq()
```

### 6.3 handle 编码方案

```
64 位 handle = (cmd_cookie << 32) | tag
              ^^^^^^^^^^^^   ^^^^^^^
              高 32 位      低 32 位

tag: blk_mq_unique_tag(req)
     — 唯一标识一个 request, 包含 hwq_id 和 request_id

cmd_cookie: 每次发送新请求时递增
     — 用于检测过时响应 (重连后旧连接的响应携带旧 cmd_cookie,
       与新请求的 cmd_cookie 不匹配, 被拒绝)
```

---

## 七、超时处理

### 7.1 超时配置

```
nbd_set_cmd_timeout(nbd, timeout_seconds)
    |
    +-- tag_set.timeout = timeout * HZ
    +-- blk_queue_rq_timeout(disk->queue, timeout * HZ)
```

- `timeout > 0`: 请求超时触发 `nbd_xmit_timeout()`
- `timeout = 0`: 使用默认 30 秒超时，但不标记连接死亡（仅告警）

### 7.2 nbd_xmit_timeout() 处理策略

```
blk-mq 检测到请求超时
    |
    v
nbd_xmit_timeout(req)
    |
    +-- mutex_trylock(&cmd->lock) 失败? → RESET_TIMER (竞争, 稍后重试)
    |
    +-- PARTIAL_SEND? → RESET_TIMER (由 pending_work 处理)
    |
    +-- !INFLIGHT? → DONE (已处理)
    |
    +-- 多连接 或 单连接+超时设置:
    |   +-- 验证 socket cookie 一致 (防止误伤重连后的新 socket)
    |   +-- nbd_mark_nsock_dead(nsock, notify=1)
    |   +-- nbd_requeue_cmd() → DONE
    |   (请求放到其他活连接或等待重连)
    |
    +-- timeout=0 (用户禁用超时断开):
    |   +-- retries++ (打印告警)
    |   +-- 检查 socket cookie 是否变更
    |   |   +-- 变更: requeue → DONE
    |   |   +-- 未变更: RESET_TIMER (继续等待)
    |
    +-- 默认: 无可恢复手段
        +-- cmd->status = BLK_STS_IOERR
        +-- sock_shutdown(nbd)
        +-- blk_mq_complete_request() → DONE
```

---

## 八、多连接 (Multi-Connection) 支持

### 8.1 架构

```
nbd_device
  └── config
        └── socks[] = [nbd_sock_0, nbd_sock_1, ..., nbd_sock_N]
             num_connections = N+1
             live_connections = 原子计数
             
blk-mq tag_set
  └── nr_hw_queues = num_connections
  
每个 hctx (硬件队列) 绑定到一条连接:
  - hctx->queue_num 对应 socks[] 索引
  - blk-mq 基于 CPU 亲和性将请求分发到不同 hctx
```

### 8.2 故障转移 (Fallback)

```
请求分配到 socks[k]
    |
    +-- socks[k]->dead = false: 正常发送
    |
    +-- socks[k]->dead = true:
        |
        +-- find_fallback(nbd, k)
        |   |
        |   +-- 检查设备是否 DISCONNECTED
        |   +-- 单连接: 返回 -1 (无后备)
        |   +-- 检查缓存的 fallback_index
        |   |   +-- 有效: 直接返回
        |   +-- 遍历所有连接, 找第一个活连接
        |   +-- 更新 fallback_index 缓存
        |
        +-- 有 fallback: 返回 fallback 索引
        +-- 无 fallback: wait_for_reconnect()
            +-- dead_conn_timeout > 0: 阻塞等待
            +-- dead_conn_timeout = 0: 立即返回失败
```

### 8.3 连接死亡通知

```
nbd_mark_nsock_dead(nbd, nsock, notify=1)
    |
    +-- kmalloc(link_dead_args, GFP_NOIO)
    +-- INIT_WORK → nbd_dead_link_work
    +-- queue_work(system_wq, &args->work)
    |
    v (异步)
nbd_dead_link_work()
    |
    v
nbd_mcast_index(args->index)
    +-- 构造 netlink 消息 (NBD_CMD_LINK_DEAD, NBD_ATTR_INDEX)
    +-- genlmsg_multicast() → "nbd_mc_group"
```

用户空间监听 `nbd_mc_group` 多播组即可收到连接死亡通知。

---

## 九、重连接机制

### 9.1 nbd_reconnect_socket()

```
nbd_reconnect_socket(nbd, new_fd)
    |
    +-- nbd_get_socket(nbd, new_fd)  // 验证 socket 类型
    |
    +-- 遍历所有连接, 找到第一个 dead:
        |
        +-- 获取 tx_lock (双重检查 dead)
        +-- sk_set_memalloc() + sk_sndtimeo
        +-- 增加 recv_threads + config_refs
        +-- 保存旧 socket → 安装新 socket
        +-- nsock->dead = false
        +-- nsock->cookie++ (区分新旧连接!)
        +-- sockfd_put(old_sock)
        +-- clear_bit(DISCONNECTED)
        +-- queue_work(recv_workq, &args->work)
        +-- atomic_inc(live_connections)
        +-- wake_up(conn_wait)
    
    +-- 返回 -ENOSPC (没有 dead 连接)
```

### 9.2 Cookie 机制的作用

`nsock->cookie` 每次重连时递增。同样 `cmd->cookie` 在发送时记录当前的 `nsock->cookie`。

用途：在超时处理中验证 socket 未被替换：
```c
if (cmd->cookie == nsock->cookie)  // 当前 socket 与我发送时相同
    nbd_mark_nsock_dead(nbd, nsock, 1);  // 可以标记为 dead
else
    // socket 已经被重连替换, 不要标记新 socket 为 dead
```

这防止了超时处理错误地将刚重连的新连接标记为死亡。

---

## 十、部分发送机制 (PENDING WORK)

### 10.1 背景

当 `nbd_send_cmd()` 在发送请求头或写数据时被信号中断：
- 请求头可能已经部分发送到服务端
- 不能触发重发 (重发会用新的 tag, 但服务端已看到旧的 tag)
- 必须在当前请求上下文中继续发送

### 10.2 实现

```
nbd_send_cmd() 被信号中断 (sent > 0)
    |
    v
nbd_sched_pending_work(nbd, nsock, cmd, sent)
    |
    +-- nsock->pending = req
    +-- nsock->sent = sent
    +-- set_bit(NBD_CMD_PARTIAL_SEND, &cmd->flags)
    +-- refcount_inc(&nbd->config_refs)
    +-- schedule_work(&nsock->work)
    |
    v (异步执行)
nbd_pending_cmd_work(work)
    |
    +-- 循环:
    |   +-- nbd_send_cmd(nbd, cmd, cmd->index)
    |   +-- 如果 !nsock->pending: 完成, 退出
    |   +-- 如果接近 deadline: BLK_STS_IOERR, 退出
    |   +-- msleep(wait_ms)   // 指数退避: 2, 4, 8, 16ms...
    |   +-- wait_ms *= 2
    |
    +-- clear_bit(NBD_CMD_PARTIAL_SEND)
    +-- nbd_config_put(nbd)
```

指数退避策略: 给 TCP 栈时间清空发送缓冲区, 逐步增加等待间隔, 避免 CPU 自旋。

---

## 十一、引用计数与生命周期

### 11.1 两个引用计数

| 引用计数 | 对象 | 用途 |
|---------|------|------|
| `nbd->refs` | nbd_device | 设备生命周期。降为 0 时销毁 /dev/nbdX |
| `nbd->config_refs` | nbd_config | 配置生命周期。降为 0 时释放连接和内存 |

### 11.2 config_refs 的来源

| 来源 | 增加时机 | 释放时机 |
|------|---------|---------|
| 初始创建 | `nbd_alloc_and_init_config()` | `nbd_config_put()` |
| 接收线程 | `nbd_start_device()` 中每个连接 +1 | `recv_work` 退出时 |
| 延迟发送 | `nbd_sched_pending_work()` | `nbd_pending_cmd_work` 退出 |
| inflight 请求 | `nbd_get_config_unlocked()` | `nbd_handle_cmd` 返回后 |
| ioctl SET_SOCK | 非 netlink 模式 | `nbd_clear_sock_ioctl()` |

### 11.3 设备销毁流程

```
最后一个 refs 释放
    |
    v
nbd_put(nbd)
    |
    +-- DESTROY_ON_DISCONNECT?
    |   +-- Yes: queue_work(nbd_del_wq, &nbd->remove_work)
    |   |           |
    |   |           v (异步)
    |   |       nbd_dev_remove_work()
    |   |
    |   +-- No: nbd_dev_remove(nbd)  (同步)
    |
    v
nbd_dev_remove()
    +-- del_gendisk(disk)          // 删除 /dev/nbdX
    +-- blk_mq_free_tag_set()      // 释放标签集
    +-- idr_remove(index)          // 从全局 IDR 注销
    +-- destroy_workqueue()        // 销毁接收工作队列
    +-- put_disk(disk)             // 释放 gendisk 内存
```

---

## 十二、网络传输

### 12.1 __sock_xmit() — 底层传输

```c
static int __sock_xmit(struct nbd_device *nbd, struct socket *sock,
                       int send, struct iov_iter *iter,
                       int msg_flags, int *sent)
```

关键设计:

1. **权限提升**: `override_creds(nbd_cred)` — 使用 root 凭证进行网络操作
2. **内存回收防护**: `memalloc_noreclaim_save()` — 防止网络栈内存分配触发回写
3. **内存分配策略**: `sk_allocation = GFP_NOIO | __GFP_MEMALLOC` — 不触发 IO
4. **信号抑制**: `MSG_NOSIGNAL` — 防止 SIGPIPE
5. **循环传输**: `while (msg_data_left(&msg))` — 直到所有数据传完

### 12.2 TCP_NODELAY 的含义

写请求数据发送时使用 MSG_MORE 标志优化 TCP 分段：
- 非最后一个 bio_vec: 设置 MSG_MORE（后面还有数据，暂不发送）
- 最后一个 bio_vec: 不设 MSG_MORE（触发立即发送）

---

## 十三、DebugFS 接口

在 `/sys/kernel/debug/nbd/nbdX/` 下提供：

| 文件 | 内容 | 说明 |
|------|------|------|
| `tasks` | recv PID | 当前接收线程的 PID |
| `size_bytes` | 设备大小 (字节) |
| `timeout` | 超时时间 |
| `blocksize_bits` | 块大小 (2 的幂) |
| `flags` | 标志位详解 | 列出所有已设置的 NBD_FLAG_* |

---

## 十四、Tracepoint 追踪事件

| 事件 | 触发时机 |
|------|---------|
| `nbd_send_request` | 发送 NBD 请求包头前 |
| `nbd_header_sent` | 请求头发送完成后 |
| `nbd_payload_sent` | 请求数据 (写) 发送完成后 |
| `nbd_header_received` | 收到服务端响应包头 |
| `nbd_payload_received` | 收到服务端响应数据 (读) 后 |

可通过 `perf` 或 `trace-cmd` 追踪：
```bash
perf record -e nbd:nbd_send_request -a
perf record -e nbd:nbd_header_sent -e nbd:nbd_header_received -a
```

---

## 十五、NBD 协议包格式

### 15.1 请求包 (nbd_request)

```
Offset  Size  Field     Description
------  ----  -----     -----------
0       4     magic     NBD_REQUEST_MAGIC (0x25609513, 网络字节序)
4       4     type      NBD_CMD_* | NBD_CMD_FLAG_* (网络字节序)
8       8     cookie    请求标识符 (网络字节序), 用于匹配响应
16      8     from      起始偏移 (字节, 网络字节序)
24      4     len       数据长度 (字节, 网络字节序)
------
Total: 28 字节
```

### 15.2 响应包 (nbd_reply)

```
Offset  Size  Field     Description
------  ----  -----     -----------
0       4     magic     NBD_REPLY_MAGIC (0x67446698, 网络字节序)
4       4     error     0 = 成功, 非零 = 错误 (网络字节序)
8       8     cookie    请求标识符 (原样返回请求中的 cookie)
------
Total: 16 字节
```

### 15.3 传输流程

```
写请求流程:
  Client  →  [request header (28 bytes)]  →  Server
  Client  →  [write data (len bytes)]     →  Server
  Client  ←  [reply (16 bytes)]           ←  Server

读请求流程:
  Client  →  [request header (28 bytes)]  →  Server
  Client  ←  [reply (16 bytes)]           ←  Server
  Client  ←  [read data (len bytes)]      ←  Server
```

---

## 十六、初始化与清理

### 16.1 模块初始化 (nbd_init)

```
nbd_init()
    |
    +-- BUILD_BUG_ON(sizeof(nbd_request) != 28)  // 协议包大小验证
    +-- 计算 part_shift (基于 max_part)
    +-- 验证参数合法性
    +-- register_blkdev(NBD_MAJOR, "nbd")  // 注册主设备号
    +-- alloc_workqueue("nbd-del")         // 创建删除工作队列
    +-- prepare_kernel_cred(&init_task)     // 准备 root 凭证
    +-- genl_register_family(&nbd_genl_family) // 注册 netlink 族
    +-- nbd_dbg_init()                     // 初始化 debugfs
    +-- 创建 nbds_max 个设备               // nbd_dev_add(i, 1)
        └── 每个设备: blk_mq_alloc_tag_set → blk_mq_alloc_disk → add_disk
```

### 16.2 模块卸载 (nbd_cleanup)

```
nbd_cleanup()
    |
    +-- genl_unregister_family()           // 注销 netlink (优先, 防止新命令)
    +-- nbd_dbg_close()                    // 关闭 debugfs
    +-- 遍历 IDR: 收集所有有引用计数的设备到链表
    +-- 逐个 nbd_put() 释放设备引用
    +-- destroy_workqueue(nbd_del_wq)      // 销毁删除队列
    +-- put_cred(nbd_cred)                 // 释放凭证
    +-- idr_destroy()                      // 销毁 IDR
    +-- unregister_blkdev(NBD_MAJOR)       // 注销主设备号
```

---

## 十七、安全设计

### 17.1 权限控制

- **CAP_SYS_ADMIN**: 所有 ioctl 操作需要 root 权限
- **netlink_capable(CAP_SYS_ADMIN)**: Netlink 操作同样需要 root

### 17.2 凭证提升

网络 I/O 使用 `prepare_kernel_cred(&init_task)` 提供的 root 凭证，确保即使调用者权限不足，网络操作也不会被拒绝。

### 17.3 内存死锁防护

- `memalloc_noreclaim_save/restore()`: 防止网络栈内存分配时触发内存回收
- `GFP_NOIO | __GFP_MEMALLOC`: 禁止网络栈内存分配触发 I/O
- `WQ_MEM_RECLAIM`: 接收工作队列有内存回收标志

这些措施的背景：NBD 是块设备驱动，如果它自身的内存分配触发了块 I/O (回写)，而该回写又经过 NBD，就会形成循环依赖导致死锁。

### 17.4 ioctl/netlink 互斥

- ioctl 绑定的设备: 可以任意修改
- Netlink 绑定的设备: ioctl 只允许 DISCONNECT 和 CLEAR_SOCK
- `NBD_RT_BOUND` 标志区分两种模式

---

## 十八、关键设计模式总结

| 设计模式 | 实现 | 目的 |
|---------|------|------|
| 引用计数 | refcount_t refs / config_refs | 确保对象在使用中不被释放 |
| 异步删除 | nbd_del_wq + remove_work | 避免在持有锁时调用 del_gendisk |
| Cookie 机制 | nsock->cookie / cmd->cookie | 区分重连前后的 socket 连接 |
| Handle 编码 | (cmd_cookie << 32) \| tag | 在一个 64 位值中携带 tag 和 cookie |
| 部分发送 | NBD_CMD_PARTIAL_SEND + pending_work | 信号中断后安全完成发送 |
| 故障转移 | fallback_index + find_fallback | 多连接下的自动故障转移 |
| 指数退避 | wait_ms *= 2 | 部分发送重试时减少 CPU 占用 |
| 原子标志 | test_and_set_bit / test_and_clear_bit | 无锁并发状态管理 |
| 内存屏障 | smp_mb__before_atomic / smp_mb__after_atomic | 确保 config 指针与引用计数的可见性 |
| 连接通知 | nbd_mcast_group 多播 | 通知用户空间连接状态变更 |

---

## 十九、总结

NBD 内核驱动 (nbd.ko) 是一个完整且成熟的网络块设备客户端实现，基于 blk-mq 框架，支持以下核心特性：

1. **两种配置接口**: 传统 ioctl (兼容 nbd-client) + 现代 netlink (nbd-server 等)
2. **多连接负载均衡**: 一个设备可配置多条连接，blk-mq 自动分发请求
3. **自动故障转移**: 连接断开时请求自动路由到其他活连接
4. **连接超时重试**: 可配置超时和重连接等待策略
5. **信号安全**: 部分发送机制确保信号中断不会导致协议错误
6. **内存安全**: 多层防护防止 NBD 驱动自身成为 I/O 死锁的根源
7. **TCP/Unix Socket**: 支持两种传输层协议
8. **完整块 I/O 栈**: 支持 READ/WRITE/FLUSH/FUA/TRIM/WRITE_ZEROES
9. **DebugFS 诊断**: 运行时查看设备状态和标志
10. **Tracepoint 追踪**: 支持 perf/trace-cmd 性能分析