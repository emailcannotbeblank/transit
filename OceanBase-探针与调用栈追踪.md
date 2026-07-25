> From: https://gemini.google.com/app/d8b883a459f5450a

# you asked

message time: 2026-06-23 18:17:02

已经修改 #define PAUSE() ({OB_ATOMIC_EVENT(atomic_pause); DTRACE_PROBE(oceanbase, pause_called); asm("yield\n");})  // for ARM。怎么打探针，怎么抓调用栈

---

# gemini response

你通过 `DTRACE_PROBE` 埋点的是一个 **USDT (User-Level Statically Defined Tracing)** 探针。在 Linux (ARM 架构) 环境下，通常 OceanBase 会作为 `observer` 进程运行。

针对 USDT 探针，目前最强大且现代的工具是基于 eBPF 的 **`bpftrace`**，其次是 Linux 原生的 **`perf`** 工具。

以下是具体的实操步骤：

### 前置确认：检查探针是否编译成功

在挂载探针之前，先确认新加的探针已经成功编译进 OceanBase 的二进制文件（通常是 `observer`）中。

你可以使用 `readelf` 或 BCC 工具包中的 `tplist` 来检查：
```bash
# 假设你的二进制文件路径为 /home/admin/oceanbase/bin/observer
readelf -n /home/admin/oceanbase/bin/observer | grep pause_called

# 或者使用 tplist (如果安装了 bcc-tools)
tplist -l /home/admin/oceanbase/bin/observer | grep pause_called
```
如果输出包含类似 `oceanbase:pause_called` 的字样，说明 USDT 探针已经生效，可以开始抓取了。

---

### 方法一：使用 `bpftrace` 抓取调用栈（强烈推荐）

`bpftrace` 是追踪 eBPF/USDT 最简单、最直观的工具。它可以一行命令直接打印出命中探针时的用户态调用栈。

**1. 安装 bpftrace（如果未安装）：**
```bash
# CentOS/RedHat/Anolis 等
sudo yum install bpftrace

# Ubuntu/Debian
sudo apt-get install bpftrace
```

**2. 编写并运行探测命令：**
使用 `usdt` 探针类型，并调用内部变量 `ustack`（获取用户态调用栈）和 `comm`（获取进程名）/ `tid`（获取线程 ID）。

```bash
sudo bpftrace -e '
usdt:/home/admin/oceanbase/bin/observer:oceanbase:pause_called 
{ 
    printf("Hit PAUSE() by thread: %d (%s)\n", tid, comm);
    printf("%s\n", ustack); 
}'
```

**如果你只想抓取某个特定进程 (PID) 的调用栈，以减少性能开销：**
```bash
sudo bpftrace -p <OBSERVER_PID> -e '
usdt:/home/admin/oceanbase/bin/observer:oceanbase:pause_called 
{ 
    printf("Hit PAUSE() stack:\n%s\n", ustack); 
}'
```

---

### 方法二：使用 BCC 的 `trace` 工具

如果你已经安装了 `bcc-tools`，可以直接使用它的 `trace` 脚本，它同样底层基于 eBPF。

```bash
# 假设 trace 工具在 /usr/share/bcc/tools/trace
sudo /usr/share/bcc/tools/trace -p <OBSERVER_PID> -U \
'u:/home/admin/oceanbase/bin/observer:oceanbase:pause_called "Hit pause"'
```
*-U 参数的作用是在触发时打印用户态调用栈 (User stack)。*

---

### 方法三：使用 `perf` 工具（适用于不支持 eBPF 的老系统）

如果你的环境无法运行 eBPF，或者内核版本较低，可以使用 Linux 自带的 `perf` 工具。

**1. 注册 USDT 探针：**
将 `observer` 中的 SDT 探针注册到系统中（可能需要 root 权限）。
```bash
sudo perf buildid-cache --add /home/admin/oceanbase/bin/observer
sudo perf probe -x /home/admin/oceanbase/bin/observer sdt_oceanbase:pause_called
```

**2. 录制调用栈数据：**
使用 `-g` 参数抓取调用栈，`-e` 指定刚刚注册的探针，`-p` 指定进程。
```bash
# 录制 10 秒钟的数据，或者按 Ctrl+C 停止
sudo perf record -e sdt_oceanbase:pause_called -g -p <OBSERVER_PID> -- sleep 10
```

**3. 查看报告：**
```bash
sudo perf report
```
在交互界面中展开 `sdt_oceanbase:pause_called`，即可看到对应的代码调用栈。

---

### ⚠️ 重要注意事项：关于 ARM 调用栈完整性

1. **Frame Pointer (栈帧指针)：** ARM64 架构下，为了能让 eBPF (如 `ustack`) 完美解析出调用栈，OceanBase 在编译时需要保留 Frame Pointer（即**不能**使用 `-fomit-frame-pointer` 编译选项，通常需要带上 `-fno-omit-frame-pointer`）。
2. **符号表 (Symbols)：** 如果打印出的栈只有十六进制地址，没有函数名（如 `ob_foo_function+0x12`），说明你的 `observer` 二进制被 strip 过，去除了符号表。你需要使用带有 debug 信息的未 strip 的二进制文件，或者将 debuginfo 文件放在系统对应路径下。

---

# you asked

message time: 2026-06-23 18:20:44

我要分析调用栈 方案选型

---

# gemini response

分析 C/C++ 程序（尤其是像 OceanBase 这样对时延和并发要求极高的分布式数据库）的调用栈，选型时最核心的考量维度通常是：**性能开销（Overhead）**、**生产环境安全性（Safety）**以及**灵活度（Flexibility）**。

以下是针对 Linux/ARM 环境下主流调用栈分析方案的深度对比与选型指南：

### 核心方案对比矩阵

| 技术方案 | 核心原理 | 性能开销 | 生产环境可用 | 优势 | 劣势 / 依赖 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **eBPF (bpftrace/bcc)** | 在内核沙盒中运行字节码，读取用户态/内核态栈 | **极低** | ✅ 极佳 | 安全、动态插拔、无需重启应用、支持 USDT | 依赖较高内核版本（推荐 4.4+，最好 4.18+） |
| **perf (Linux Native)** | 基于硬件 PMU 或内核事件的采样采样（Profiling） | **低 - 中** | ✅ 适合 | 内核自带、支持生成火焰图、适合宏观性能调优 | 解析 DWARF 展开栈时开销较大，报告生成较慢 |
| **GDB / pstack** | 利用 `ptrace` 附加到进程，读取寄存器和内存 | **极高** | ❌ 严禁 | 细节最丰富，可查看所有变量和内存状态 | 会导致进程 **Stop-The-World** (暂停)，业务中断 |
| **SystemTap** | 动态编译加载内核模块 (LKM) 进行 Hook | **低** | ⚠️ 需谨慎 | 历史悠久，支持老旧内核版本 | 插入内核模块有宕机风险，正逐渐被 eBPF 淘汰 |
| **应用内置 (libunwind / backtrace)** | 在 C++ 代码中主动调用 API 回溯当前线程栈 | **低** (按需触发) | ✅ 适合 | 无需外部工具，常用于 Crash 兜底或慢查询打点 | 需要修改源码，侵入性强，无法动态观测其他代码路径 |

---

### 详细选型建议与落地场景

#### 1. eBPF 生态 (bpftrace / libbpf) —— **首选推荐 (Modern Standard)**
如果你使用的是较新的 Linux 系统（如 CentOS 8, Ubuntu 18.04+, Anolis OS），**eBPF 是绝对的首选**。
* **适用场景：** 线上突发问题排查、特定探针（如你加的 `PAUSE()` USDT 探针）的调用链分析、锁争用分析。
* **最佳实践：** 线下使用 `bpftrace` 写单行脚本快速验证，线上可以通过开发基于 `libbpf` 的 C/C++ 程序（即 CO-RE 架构）打包成独立的可执行文件进行常态化监控，不依赖宿主机安装 LLVM。

#### 2. Linux perf + 火焰图 (Flamegraph) —— **性能瓶颈分析利器**
当你不知道问题出在哪里，只知道“CPU 跑满了”或者“请求变慢了”时，`perf` 是最好的宏观分析工具。
* **适用场景：** 寻找 CPU 热点（On-CPU）或等待事件（Off-CPU），全局架构审视。
* **最佳实践：** 定期使用 `perf record -F 99 -g` 采样，然后用 Brendan Gregg 的开源脚本生成 SVG 火焰图。它能让你一目了然地看到哪个函数占用了最多的时间。

#### 3. pstack / GDB —— **仅限线下“解剖”**
* **适用场景：** 线下开发环境排查死锁（Deadlock）、内存损坏（Memory Corruption）或 Core Dump 分析。
* **最佳实践：** 线上绝对禁止对正在运行的 observer 进程使用 `gdb` 或 `pstack`，因为 `ptrace` 系统调用会让进程瞬间挂起，直接导致分布式节点超时剔除或主备切换。

#### 4. 代码内嵌方案 (libunwind / boost::stacktrace) —— **系统鲁棒性兜底**
* **适用场景：** 发生 Segment Fault、OOM 时的临终遗言（Crash Log），或者检测到长时间未释放的内存锁时主动打印告警日志。
* **最佳实践：** 在 OceanBase 的信号处理函数（Signal Handler）中集成，确保崩溃时留下第一手现场。

---

### ⚠️ ARM 架构特别避坑指南：栈展开 (Stack Unwinding)

在 ARM64 架构下抓取调用栈，你必须面对一个编译器选项的抉择：**如何展开栈？**

1.  **Frame Pointer (帧指针, FP)：**
    * **原理：** 利用寄存器 `x29` 保存上一层栈帧的地址。
    * **优点：** 极快，`perf` 和 `eBPF` 抓取时几乎没有额外开销。
    * **缺点：** 占用了一个通用寄存器，可能导致 1%~2% 的性能损耗。
    * **建议：** 在编译 OceanBase 时**强烈建议**带上 `-fno-omit-frame-pointer` 选项。
2.  **DWARF (调试信息展开)：**
    * **原理：** 编译器生成包含映射关系的 `.debug_frame` 或 `.eh_frame` 段，工具通过查表回溯。
    * **优点：** 不损失任何运行时性能。
    * **缺点：** 工具在读取时开销巨大（甚至可能造成卡顿），且 `bpftrace` 等 eBPF 工具对用户态的 DWARF 栈展开支持有限/较差。

---

选择哪种方案，取决于你的具体目标和所处的环境。你目前主要是在**线下开发/测试环境**排查特定 Bug，还是需要部署一套到**线上生产环境**进行常态化的性能监控？

---

# you asked

message time: 2026-06-23 18:24:21

我要分析有哪些不同的调用栈。提供ebpf方案。我要看这个的调用栈 #define PAUSE() ({OB_ATOMIC_EVENT(atomic_pause); DTRACE_PROBE(oceanbase, pause_called); asm("yield\n");})  // for ARM

---

# gemini response

在使用 eBPF 技术分析特定的调用栈时，最核心的优势在于**内核态聚合（Kernel-space Aggregation）**。如果直接把每次触发探针的调用栈都传回用户态，高并发下会导致极大的性能开销。而 eBPF 允许我们在内核中通过哈希表（Map）将相同的调用栈归类并计数，最后只输出去重后的调用栈及其命中次数。

针对你修改的 `#define PAUSE()` 中的 `oceanbase:pause_called` 探针，以下是两种最常用的 eBPF 聚合分析方案。

---

### 方案一：使用 `bpftrace` 进行快速聚合（推荐）

`bpftrace` 适合编写简短的脚本，能够非常直观地把“调用栈+线程名”作为 Key 进行计数聚合。

**1. 编写聚合脚本 (`aggregate_pause.bt`)：**
```bpftrace
// aggregate_pause.bt
usdt:/home/admin/oceanbase/bin/observer:oceanbase:pause_called
{
    // 以 用户态调用栈(ustack) 和 线程名(comm) 作为联合Key进行计数
    @[ustack, comm] = count();
}
```

**2. 运行并收集数据：**
```bash
sudo bpftrace aggregate_pause.bt
```
让程序运行一段时间（例如业务压测期间），然后按下 **`Ctrl + C`** 结束。`bpftrace` 会自动在终端打印出所有**不同的调用栈**，并按出现次数从低到高排序。

输出格式通常类似下面这样：
```text
@[
    oceanbase::common::ObTimeGuard::check_and_print()+72
    oceanbase::common::ObWaitEvent::wait()+144
    oceanbase::storage::ObLatch::lock_loop()+256
    oceanbase::storage::ObLatch::lock()+88
    ob_worker_thread_loop+512
, ob_sql_worker]: 4122  <-- 触发了 4122 次

@[
    oceanbase::election::ObLeaderElection::wait_period()+96
    oceanbase::election::ObElectionThread::run()+180
, ob_elect_worker]: 18530 <-- 触发了 18530 次
```

---

### 方案二：使用 BCC 的 `stackcount` 工具（生产环境更稳健）

如果你需要更长的时间监控，或者不希望在服务器上安装 `bpftrace` 的编译前端，可以使用 BCC 工具链中自带的 `stackcount` 脚本。它是一个用 Python 包装的 eBPF 程序。

**执行命令：**
```bash
# -p 指定 observer 的进程号
# -U 表示只抓取用户态堆栈 (User stack)
# -v 打印更多调试符号信息
sudo /usr/share/bcc/tools/stackcount -p $(pgrep observer) -U u:/home/admin/oceanbase/bin/observer:oceanbase:pause_called
```

同样，在压测结束后按 `Ctrl + C`，它会吐出所有不同的调用栈和对应的 Count 值。你可以将输出重定向到文件中进行离线分析。

---

### 💡 ARM64 环境下的关键避坑指南

在 ARM 架构下分析调用栈，经常会遇到“栈断了”或者“全是十六进制地址”的问题，请务必确认以下三点：

1. **帧指针（Frame Pointer）：** OceanBase 编译时必须带上 `-fno-omit-frame-pointer` 选项。ARM64 依赖 `x29` 寄存器作为栈帧指针。如果去除了它，eBPF 内核探测器将无法进行快速的 `ustack` 步进回溯。
2. **符号表（Symbols）：**
   确保运行机器上有 `observer` 的未剥离（unstripped）符号表，或者安装了对应的 `debuginfo` 包。否则你看到的调用栈只有一堆十六进制地址（如 `0xaaaae0123456`），无法识别函数名。
3. **生成火焰图（Flame Graph）：**
   如果你拿到了大量的栈数据，可以配合 Brendan Gregg 的 `stackcount` 转换工具，直接将其转化为**火焰图**，这样哪个模块在密集调用 `PAUSE()` 就一目了然了。

---

为了让你更直观地理解 eBPF 是如何将海量的并发调用聚合为几种不同的典型调用栈的，你可以通过下面的互动工具来模拟和分析 OceanBase 中可能触发 `PAUSE()` 的几种经典场景。

---

# you asked

message time: 2026-06-23 18:27:56

怎么安装 [root@localhost z00951445]# bpftrace
bash: bpftrace: command not found
[root@localhost z00951445]#   [root@localhost z00951445]# cat /etc/os-release 
NAME="Alibaba Cloud Linux"
VERSION="4"
ID="alinux"
ID_LIKE="anolis"
VERSION_ID="4"
VARIANT="Pro Edition"
VARIANT_ID="pro"
ALINUX_MINOR_ID="0"
ALINUX_UPDATE_ID="1"
PLATFORM_ID="platform:alnx4"
PRETTY_NAME="Alibaba Cloud Linux 4.0.1 (Pro Edition)"
ANSI_COLOR="0;31"
HOME_URL="https://www.aliyun.com/"
BUG_REPORT_URL="https://www.aliyun.com/"
[root@localhost z00951445]#   用yum还是apt

---

# gemini response

你应该使用 **`yum`**（或者它的升级版 **`dnf`**）。

根据你提供的 `/etc/os-release` 信息，你的系统是 **Alibaba Cloud Linux 4 (Anolis 衍生版)**。它属于 RedHat/CentOS 生态（RPM 系），因此默认的包管理器是 `yum` 和 `dnf`，而不是 Ubuntu/Debian 生态下的 `apt`。

你可以直接在终端运行以下命令来安装：

```bash
# 使用 yum 或 dnf 安装（加上 -y 自动确认）
yum install -y bpftrace

# 或者
dnf install -y bpftrace
```
*(注：在 Alibaba Cloud Linux 4 中，`yum` 实际上只是 `dnf` 的一个软链接，两者完全等价。)*

安装完成后，你可以运行以下命令验证是否安装成功：

```bash
bpftrace -V
```

如果成功输出版本号（例如 `bpftrace v0.16.0` 等），你就可以继续用它来抓取 OceanBase 的调用栈了。

---

# you asked

message time: 2026-06-23 18:31:36

}[root@localhost z00951445]# bpftrace aggregate_pause.bt 
ERROR: Can't attach to 1235 probes because it exceeds the current limit of 512 probes.
You can increase the limit through the BPFTRACE_MAX_PROBES environment variable, but BE CAREFUL since a high number of probes attached can cause your system to crash.
[root@localhost z00951445]#

---

# gemini response

这是一个非常经典的 C/C++ 宏定义埋点导致的“探针爆炸”问题。

### 为什么会超过限制？
你在代码里通过 `#define PAUSE() ...` 定义了这个探针。由于它是宏（或者如果它是定义在头文件里的 `inline` 函数），编译器在编译 OceanBase 时，会把这行代码**展开（内联）到所有调用它的地方**。
这就导致最终的 `observer` 二进制文件中，实际上生成了 **1235 个**独立的物理探针点。`bpftrace` 为了保护内核不被瞬间干崩，默认限制一次最多只能挂载 512 个探针。

你有以下两种解决方案，根据你的实际情况选择：

---

### 方案一：强行调大 bpftrace 限制（最快，适合线下测试）

你的系统是基于较新内核的 Alinux 4，机器内存如果充裕的话，挂载 1000 多个探针通常是没问题的（只是 `bpftrace` 启动时会稍微慢几秒钟去 attach）。

你只需要在执行命令前加上环境变量即可：

```bash
sudo BPFTRACE_MAX_PROBES=2000 bpftrace aggregate_pause.bt
```

**⚠️ 警告：** 这种做法在线下排查完全没问题，但如果是**线上高负载的生产环境**，同时向 1235 个地址注入 eBPF 字节码，可能会在瞬间引起微小的 CPU 抖动。

---

### 方案二：修改 OceanBase 源码（最优雅，强烈推荐）

如果你只是想知道谁调用了 `PAUSE()`，最好的做法是**打破内联**，让系统里只存在 **1 个**物理探针，而不是 1235 个。这样 eBPF 加载瞬间完成，且性能极佳。

**1. 修改头文件（去掉探针，改为调用函数）：**
把你修改的地方改回宏，但让它调用一个实函数：
```cpp
// 在头文件里
void ob_trigger_pause_probe(); // 声明一个普通函数

#define PAUSE() ({ \
    OB_ATOMIC_EVENT(atomic_pause); \
    ob_trigger_pause_probe(); \
    asm("yield\n"); \
})
```

**2. 在某个 `.cpp` 文件中实现这个函数：**
找一个相关的 `.cpp` 文件（比如 `ob_utility.cpp` 或类似的基础组件文件），在里面放入探针：
```cpp
// 在 .cpp 文件里
void ob_trigger_pause_probe() {
    DTRACE_PROBE(oceanbase, pause_called);
}
```

**为什么这样做更好？**
因为 `.cpp` 文件只会编译一次，探针只会在这个函数的地址生成 **1 个**断点。当其他 1235 个地方触发 `PAUSE()` 时，都会跳入 `ob_trigger_pause_probe()` 从而触发这唯一的探针。此时你再用 `bpftrace` 抓 `ustack`，依然能完美抓到完整的调用栈，而且绝对不会触发 512 个探针的上限报错。

---

# you asked

message time: 2026-06-23 18:33:04

打上探针有提醒吗 [root@localhost z00951445]# BPFTRACE_MAX_PROBES=2000 bpftrace aggregate_pause.bt
Attaching 1235 probes...

---

# gemini response

默认情况下**没有**额外的“挂载成功”提醒。

当你看到终端输出 `Attaching 1235 probes...` 并且光标悬停在下一行闪烁，没有跳出任何错误提示时，这说明**探针已经成功挂载，并且正在内核中静默收集数据了。**

### 为什么它看起来“卡”住了？

这是由你在 `aggregate_pause.bt` 脚本中使用的动作决定的：
```bpftrace
@[ustack, comm] = count();
```
* 你使用的是聚合函数 `count()`，而不是打印函数 `printf()`。
* `bpftrace` 会在内核态（Kernel-space）维护一个哈希表不断累加次数，为了极致的性能，它**不会**每命中一次就向用户态的终端发消息。

### 如何查看结果？

你需要让它飞一会儿（比如在这期间跑一些 SQL 压测，或者让集群正常运行几十秒），然后**按下 `Ctrl + C`** 结束进程。

在收到中断信号退出的瞬间，`bpftrace` 会自动把内核里统计好的哈希表结果打印到屏幕上：

```bash
^C  <-- 这里是你按下了 Ctrl + C

@[
    oceanbase::common::ObWaitEvent::wait()+144
    oceanbase::storage::ObLatch::lock_loop()+256
    ...
, ob_sql_worker]: 4122
```
*(注：如果在你挂载探针的这段时间里，没有任何线程调用过 `PAUSE()`，按下 `Ctrl + C` 后就不会有任何输出。)*

---

### 💡 进阶技巧：如何让它给自己一个“提醒”？

如果你觉得静默运行心里没底，可以通过添加 `BEGIN` 探针和定时器探针，让脚本变“聪明”一点：

修改你的 `aggregate_pause.bt` 如下：

```bpftrace
// 1. 挂载开始时的提醒
BEGIN
{
    printf("✅ 成功挂载 1235 个探针！正在后台统计，按 Ctrl+C 查看结果...\n");
}

// 2. 你的核心业务逻辑
usdt:/home/admin/oceanbase/bin/observer:oceanbase:pause_called
{
    @[ustack, comm] = count();
}

// 3. (可选) 每隔 10 秒打印一次当前快照，并清空计数器
/*
interval:s:10
{
    printf("\n--- 过去 10 秒的统计结果 ---\n");
    print(@);
    clear(@);
}
*/
```
这样再运行脚本时，你不仅能立刻看到成功提示，还可以在不打断进程的情况下定期查看最新情况。

---

# you asked

message time: 2026-06-23 18:34:17

怎么退出不了 [root@localhost z00951445]# BPFTRACE_MAX_PROBES=2000 bpftrace aggregate_pause.bt
Attaching 1235 probes...
^C^C

---

# gemini response

这是一个在挂载大量 USDT/uprobe 探针时非常常见的“卡死”现象。

你按了 `Ctrl+C` 之后退不出来，**并不是工具坏了，而是它正在极其缓慢地执行“卸载探针”的清理工作。**

### 为什么会卡住？

当你按下 `Ctrl+C` (发送 SIGINT 信号) 时，`bpftrace` 会尝试优雅地退出，这包括：
1. 停止收集数据。
2. 打印哈希表（你想要的调用栈结果）。
3. **将那 1235 个探针从 `observer` 进程的内存和内核态中逐一剥离（Detach）。**

问题就出在第 3 步。在 Linux 内核中，批量卸载一千多个用户态探针（uprobe/USDT）的开销非常巨大。系统需要反复加锁、修改进程内存页权限、移除断点指令等。这个清理过程可能会耗时 **几十秒甚至几分钟**。在这期间，它会忽略你后续按下的 `Ctrl+C`。

---

### 现在的解决办法

你有两个选择：

#### 方案一：再耐心等一会儿（推荐）
放下键盘，**等 30 秒到 2 分钟**。通常情况下，等内核慢吞吞地把探针全部卸载完之后，它最终是会把结果打印出来并正常退出的。

#### 方案二：强制击杀进程（如果实在等不及了）
如果你等了几分钟它依然没有任何反应（可能由于高负载触发了内核底层的等待锁），你可以强制干掉它。
**新开一个终端（SSH 会话）** 连上这台机器，然后执行：

```bash
# 找到 bpftrace 的进程 ID 并强制发送 SIGKILL
sudo kill -9 $(pidof bpftrace)
```
*⚠️ 注意：使用 `kill -9` 强杀 `bpftrace` 可能会导致极少数探针没有被完全清理，偶尔会遗留一点极小的内存开销，但通常不会导致业务进程崩溃。*

---

### 治本之策：强烈建议修改代码

这再次印证了我们在上一步讨论的问题：**宏展开导致 1235 个物理探针是绝对的“反模式”（Anti-pattern）。**

不仅加载时会触发 512 的上限报错，退出时更是会卡到让人怀疑人生。

为了让你后续的排查不再受折磨，请务必修改 OceanBase 的代码，**把探针收敛到一个普通的 C++ 函数中**：

```cpp
// 1. 定义一个实函数
void ob_trigger_pause_probe() {
    DTRACE_PROBE(oceanbase, pause_called);
}

// 2. 宏里只调用这个函数
#define PAUSE() ({ \
    OB_ATOMIC_EVENT(atomic_pause); \
    ob_trigger_pause_probe(); \
    asm("yield\n"); \
})
```

重新编译后，整个系统里就只有 **1 个探针**。那时候你再运行 `bpftrace`，无论挂载还是按 `Ctrl+C` 退出，都会是**毫秒级**的瞬间响应，体验会非常丝滑。

---

# you asked

message time: 2026-06-23 18:34:51

用bcc方案会不会更好？

---

# gemini response

简短的回答是：**不会更好，甚至在这个特定的场景下，体验可能会更差。**

无论是使用 `bpftrace`，还是直接调用底层 API 写定制化的 BCC Python 脚本，都无法逃避你目前遇到的“探针挂载/卸载缓慢卡死”的问题。

这是因为问题的瓶颈不在于“观测前端工具”，而在于**Linux 内核底层的 uprobe/USDT 机制**。

以下是具体的原理解析和对比：

### 1. 底层机制是相同的
当你编写一个 BCC 脚本去追踪这 1235 个探针时，BCC 和 `bpftrace` 在底层的行为是完全一致的：
* 它们都需要解析 `observer` 的 ELF 文件。
* 它们都需要通过 `bpf()` 系统调用将 eBPF 字节码加载到内核。
* **最致命的一步：** 它们都需要通过 `perf_event_open` 通知内核，去修改 `observer` 进程在内存中的 **1235 个不同地址**，把原本的机器码替换成断点指令（在 ARM64 下通常是 `brk` 指令）。

当你按下 `Ctrl+C` 退出时，内核又必须把这 1235 个断点指令逐一恢复成原来的指令。这个需要频繁加锁和修改内存页的内核级操作，无论用什么高级工具发起，开销都是极其巨大的。

### 2. BCC 在处理大量探针时甚至更慢
如果你习惯于使用 BCC 框架编写基于 Python 的观测脚本，你会发现 BCC 在启动阶段会比 `bpftrace` 更慢。
* `bpftrace` 是纯 C/C++ 实现的，ELF 解析和探针加载相对高效。
* BCC 会在 Python 层进行大量的预处理、符号解析以及 USDT 参数构建。在面对成千上万个内联展开的探针时，BCC 的 Python 进程往往会消耗大量的 CPU 和时间在初始化上，导致你敲下回车后需要等待更久才会看到数据开始采集。

### 3. BCC 的唯一优势（仅限表面体验）
BCC 唯一的优势在于**可编程性**。因为你是用 Python 控制逻辑，你可以捕获 `SIGINT` (Ctrl+C) 信号，然后打印一句：
> *"正在缓慢卸载 1235 个探针，这可能需要 1 分钟，请勿强制退出..."*

这能安抚使用者的情绪，让你知道它没有死机，但它**无法加速**卸载的过程本身。

---

### 结论
在宏展开导致探针数量爆炸（Probe Explosion）的情况下，**切换观测工具治标不治本**。

唯一的正确解法依然是**重构你的埋点代码**，打破内联，将探针收敛到一个独立的实函数中：
```cpp
void ob_trigger_pause_probe() {
    DTRACE_PROBE(oceanbase, pause_called);
}
```
只要将 1235 个物理断点变成 **1 个物理断点**，无论是用 `bpftrace` 还是 BCC，加载和卸载都将是真正的毫秒级瞬间完成。

---

# you asked

message time: 2026-06-23 19:00:57

这是何意     0xaaaac397e28c
    oceanbase::observer::ObSrvDeliver::deliver_mysql_request(oceanbase::rpc::ObRequest&)+1136
    oceanbase::obmysql::ObSqlNio::run(long)+1864
    0x5
, sql_nio5]: 105112
@[
    oceanbase::omt::ObTenant::recv_request(oceanbase::rpc::ObRequest&)+492
    oceanbase::observer::ObSrvDeliver::deliver_mysql_request(oceanbase::rpc::ObRequest&)+1136
    oceanbase::obmysql::ObSqlNio::run(long)+1864
    0x5
, sql_nio5]: 105921
@[
    0xaaaac397e2b4
    oceanbase::observer::ObSrvDeliver::deliver_mysql_request(oceanbase::rpc::ObRequest&)+1136
    oceanbase::obmysql::ObSqlNio::run(long)+1864
    0x4
, sql_nio4]: 105999
@[
    oceanbase::omt::ObTenant::recv_request(oceanbase::rpc::ObRequest&)+452
    oceanbase::observer::ObSrvDeliver::deliver_mysql_request(oceanbase::rpc::ObRequest&)+1136
    oceanbase::obmysql::ObSqlNio::run(long)+1864
    0x5
, sql_nio5]: 106848
@[
    0xaaaac397e28c
    oceanbase::observer::ObSrvDeliver::deliver_mysql_request(oceanbase::rpc::ObRequest&)+1136
    oceanbase::obmysql::ObSqlNio::run(long)+1864
    0x4
, sql_nio4]: 107033
@[
    oceanbase::omt::ObTenant::recv_request(oceanbase::rpc::ObRequest&)+492
    oceanbase::observer::ObSrvDeliver::deliver_mysql_request(oceanbase::rpc::ObRequest&)+1136
    oceanbase::obmysql::ObSqlNio::run(long)+1864
    0x4
, sql_nio4]: 108009
@[
    oceanbase::omt::ObTenant::recv_request(oceanbase::rpc::ObRequest&)+452
    oceanbase::observer::ObSrvDeliver::deliver_mysql_request(oceanbase::rpc::ObRequest&)+1136
    oceanbase::obmysql::ObSqlNio::run(long)+1864
    0x4
, sql_nio4]: 109087
[root@localhost z00951445]# 
[root@localhost z00951445]#

---

# gemini response

恭喜！这说明你已经成功熬过了漫长的“卸载等待期”，`bpftrace` 正常退出了，并**完美地打印出了它在内核态聚合好的调用栈数据。**

这段输出清晰地展示了在这段时间内，是谁、在什么代码路径下、以多高的频次触发了你埋设的 `PAUSE()` 探针。

我们来逐层拆解这段数据的含义：

### 1. 基础格式解析
以这段输出为例：
```text
@[
    oceanbase::omt::ObTenant::recv_request(oceanbase::rpc::ObRequest&)+492
    oceanbase::observer::ObSrvDeliver::deliver_mysql_request(oceanbase::rpc::ObRequest&)+1136
    oceanbase::obmysql::ObSqlNio::run(long)+1864
    0x5
, sql_nio5]: 105921
```
* **`@[ ... , sql_nio5]`**：中括号内是聚合的 Key。这里由“调用栈”和“线程名 (`sql_nio5`)”组成。
* **`105921`**：这是 Value（触发次数）。说明在压测/运行期间，**这个特定的调用栈被命中了 105,921 次**。
* **调用栈顺序**：从下往上读，最下面是调用链的起点（栈底），最上面是触发探针的直接位置（栈顶）。

---

### 2. OceanBase 业务逻辑映射

从这些栈中，我们可以非常清晰地看到 OceanBase 处理请求的核心网络入口路径：

1. **`sql_nio4` / `sql_nio5` (网络 I/O 线程)**：这是底层负责监听和接收 MySQL 协议网络包的线程。
2. **`ObSqlNio::run(long)`**：NIO 线程的主循环。它在这里不断地 epoll/轮询网络事件。
3. **`ObSrvDeliver::deliver_mysql_request`**：网络包解析完毕后，准备将其分发给具体的租户（Tenant）层。
4. **`ObTenant::recv_request`**：最终将请求推入对应租户的工作队列中。

**为什么这里会极其疯狂地触发 `PAUSE()`（达到 10 万次级别）？**
因为 NIO 线程是非常底层的组件。当它试图把接收到的网络请求塞进租户的队列（`recv_request`）时，如果遇到高并发锁争用，或者采用自旋锁（Spinlock）机制重试时，就会调用你修改的 `PAUSE()` 宏里面的 `yield` 指令来主动让出极短暂的 CPU 周期，避免死循环把单个 ARM 核心打满。

---

### 3. 为什么会出现十六进制地址（`0xaaaac397e28c` 和 `0x5`）？

你肯定注意到了栈里面混进了一些奇怪的数字。这是在 ARM64 架构下使用 eBPF 抓栈时非常经典的现象：

* **栈底的 `0x4` / `0x5`**：
  这出现在 `ObSqlNio::run` 之下。这是因为线程在刚被创建（`pthread_create`）时，最底层的栈帧指针（Frame Pointer, 寄存器 `x29`）可能没有被初始化为 0，而是保留了某个线程 ID 相关的脏数据。`bpftrace` 沿着栈往下回溯时，把这个脏数据当成了函数地址打印出来了。可以完全忽略它。

* **栈顶的 `0xaaaac397e28c`**：
  例如这段：
  ```text
  @[
      0xaaaac397e28c  <-- 这里无法解析为函数名
      oceanbase::observer::ObSrvDeliver::deliver_mysql_request(...)
  ```
  这通常是因为：
  1. 你的 `PAUSE()` 宏所在的那个微小函数被编译器**深度内联 (Inline)** 到了 `deliver_mysql_request` 的某段逻辑中，导致没有独立的符号名。
  2. 或者是代码执行了尾调用优化 (Tail-call Optimization)，破坏了这一层的栈帧。

### 总结

这份报告非常有价值。它证明了你的 eBPF 探针方案（虽然由于宏展开导致探针数量很多）是完全可行的。

**你现在的结论是：**
在当前负载下，OceanBase 的 CPU 周期大量消耗在网络收包线程 (`sql_nio`) 向租户队列派发请求 (`recv_request`) 的自旋或等待逻辑中。这就是目前系统的一个微观瓶颈点/热点。

---

# you asked

message time: 2026-06-23 19:03:38

我要分析不同的调用栈啊 你这些很多调用栈看上去是重复的 
    oceanbase::common::ObAtomicReference::dec_ref_cnt_and_inc_seq_num(unsigned int&)+144
    oceanbase::storage::ObMicroBlockDataHandle::operator=(oceanbase::storage::ObMicroBlockDataHandle const&)+296
    oceanbase::storage::ObMicroBlockHandleMgr::get_micro_block_handle(unsigned long, oceanbase::blocksstable::ObMicroIndexInfo const&, bool, oceanbase::storage::ObMicroBlockDataHandle&)+1612
, T1002_L0_G0]: 15626
@[
    oceanbase::common::ObAtomicReference::dec_ref_cnt_and_inc_seq_num(unsigned int&)+144
    oceanbase::storage::ObMicroBlockDataHandle::operator=(oceanbase::storage::ObMicroBlockDataHandle const&)+296
    oceanbase::storage::ObMicroBlockHandleMgr::get_micro_block_handle(unsigned long, oceanbase::blocksstable::ObMicroIndexInfo const&, bool, oceanbase::storage::ObMicroBlockDataHandle&)+1612
, T1002_L0_G0]: 15687
@[
    oceanbase::common::ObAtomicReference::dec_ref_cnt_and_inc_seq_num(unsigned int&)+144
    oceanbase::storage::ObMicroBlockDataHandle::operator=(oceanbase::storage::ObMicroBlockDataHandle const&)+296
    oceanbase::storage::ObMicroBlockHandleMgr::get_micro_block_handle(unsigned long, oceanbase::blocksstable::ObMicroIndexInfo const&, bool, oceanbase::storage::ObMicroBlockDataHandle&)+1612
, T1002_L0_G0]: 15726
@[
    oceanbase::common::ObAtomicReference::dec_ref_cnt_and_inc_seq_num(unsigned int&)+144
    oceanbase::storage::ObMicroBlockDataHandle::operator=(oceanbase::storage::ObMicroBlockDataHandle const&)+296
    oceanbase::storage::ObMicroBlockHandleMgr::get_micro_block_handle(unsigned long, oceanbase::blocksstable::ObMicroIndexInfo const&, bool, oceanbase::storage::ObMicroBlockDataHandle&)+1612
, T1002_L0_G0]: 15745
@[
    oceanbase::common::ObAtomicReference::dec_ref_cnt_and_inc_seq_num(unsigned int&)+144
    oceanbase::storage::ObMicroBlockDataHandle::operator=(oceanbase::storage::ObMicroBlockDataHandle const&)+296
    oceanbase::storage::ObMicroBlockHandleMgr::get_micro_block_handle(unsigned long, oceanbase::blocksstable::ObMicroIndexInfo const&, bool, oceanbase::storage::ObMicroBlockDataHandle&)+1612
, T1002_L0_G0]: 16026
@[
    0xaaaac3902f48
    oceanbase::transaction::ObTxLogCb::on_success()+192
    0xaaaac3a03614
    0xaaaac3a022d8
    oceanbase::lib::Threads::run(long)+296
    oceanbase::lib::Thread::__th_start(void*)+1556
    start_thread+688
    thread_start+12
, T1002_ApplySrv0]: 16127

---

# gemini response

你的直觉非常敏锐！这些调用栈在人类眼里确实是**完全重复**的。

这就是我们在复杂 C++ 项目中使用 eBPF 时经常遇到的“坑”。出现这种现象的根本原因，依然要归咎于那个让你头疼的 **`PAUSE()` 宏定义展开**。

下面为你拆解为什么 eBPF 会把它们当成不同的栈，以及如何正确地进行合并分析。

### 为什么 `bpftrace` 认为它们是不同的？

`bpftrace` 在内核态进行聚合并不仅仅是通过比对“函数名字符串”。为了保证极高的性能，eBPF 在内核底层使用的哈希表的 Key 是**指令的真实内存地址数组 (Raw IP Addresses)**。

由于 `PAUSE()` 是一个宏，它在编译时被散弹枪一样打到了 OceanBase 源码的 1235 个地方。
即便这两段代码最终都位于 `dec_ref_cnt_and_inc_seq_num` 这个函数内部（可能是循环里的不同分支，或者是多次被内联），它们触发探针时的**物理内存地址是不同的**。

* **栈 A 的真实形态：** `[0xaaaa1001, 0xaaaa2005, 0xaaaa3008]`
* **栈 B 的真实形态：** `[0xaaaa1099, 0xaaaa2005, 0xaaaa3008]`

在内核态，`bpftrace` 发现这两个地址数组不一样，自然把它们当成了两个不同的 Key 进行计数。
等到最后你按下 `Ctrl+C` 时，`bpftrace` 回到用户态，调用地址解析工具将这些地址翻译成函数名，结果发现 `0xaaaa1001` 和 `0xaaaa1099` 对应的竟然都是 `...dec_ref_cnt_and_inc_seq_num+144`，这才导致你在屏幕上看到了大片“长得一模一样”的调用栈。

---

### 怎么正确分析这些堆栈？（落地方案）

面对这种被拆散的调用栈数据，你有两条路可以走：

#### 方案一：一劳永逸的代码改造（强烈推荐）
正如上一回提到的，只要你修改一下 C++ 代码，把宏调用改成普通的函数调用：
```cpp
void ob_trigger_pause_probe() {
    DTRACE_PROBE(oceanbase, pause_called);
}
#define PAUSE() ({ OB_ATOMIC_EVENT(atomic_pause); ob_trigger_pause_probe(); asm("yield\n"); })
```
修改后，不管系统里有多少地方调用 `PAUSE()`，最终压入 eBPF 栈顶的**绝对内存地址永远只有 1 个**。
这样不仅彻底解决了探针超限、卸载卡死的问题，**相同路径的调用栈在内核中就会完美地合并成一条**，输出结果将变得极其清爽。

#### 方案二：离线折叠并生成火焰图 (FlameGraph)
如果你现在不想改代码、重新编译，只想尽快分析手头这份数据，最专业的做法是**不要用肉眼看终端输出**，而是用脚本把它们折叠合并，生成**火焰图**。

火焰图生成工具在处理数据时，是**基于字符串名**合并的，它会自动把这些重复的栈揉在一起。

**操作步骤：**

1.  **把输出重定向到文件：**
    ```bash
    sudo BPFTRACE_MAX_PROBES=2000 bpftrace aggregate_pause.bt > trace_output.txt
    ```
2.  **清理多余信息（保留栈和数字）：**
    下载 Brendan Gregg 大神提供的火焰图工具：
    ```bash
    git clone https://github.com/brendangregg/FlameGraph.git
    ```
3.  **转换并生成 SVG 图片：**
    `bpftrace` 输出的格式略显特殊，但你可以用一行 awk 命令将其转换为 `栈 频次` 格式，然后喂给火焰图脚本：
    ```bash
    # 使用 awk 将多行堆栈合并成单行 (用分号隔开)，然后生成火焰图
    awk '/^@/{flag=1; stack=""; next} /^,/{print stack $2; flag=0; next} flag{stack=$1";"stack}' trace_output.txt > folded.out
    
    ./FlameGraph/flamegraph.pl folded.out > pause_stacks.svg
    ```
4.  **在浏览器中打开 `pause_stacks.svg`：**
    这时候你会看到，所有来自 `T1002_L0_G0` 线程的 `dec_ref_cnt_and_inc_seq_num` 分支，已经被完美地聚合成了图表上的一块大砖头。鼠标放上去，就能清晰地看到它在总等待时间中究竟占了百分之多少。

---

# you asked

message time: 2026-06-23 19:11:41

这是何意 [root@localhost z00951445]# BPFTRACE_MAX_PROBES=2000 bpftrace aggregate_pause.bt > trace_output.txt
^C
^CERROR: failed to look up stack id -17 (pid 1609081): -2
ERROR: failed to look up stack id -17 (pid 1609070): -2
ERROR: failed to look up stack id -17 (pid 1609071): -2
ERROR: failed to look up stack id -17 (pid 1609094): -2
ERROR: failed to look up stack id -17 (pid 1609082): -2
ERROR: failed to look up stack id -17 (pid 1666232): -2
ERROR: failed to look up stack id -17 (pid 1609085): -2
ERROR: failed to look up stack id -17 (pid 1646090): -2
ERROR: failed to look up stack id -17 (pid 1609091): -2
ERROR: failed to look up stack id -17 (pid 1609074): -2
ERROR: failed to look up stack id -17 (pid 1609063): -2
ERROR: failed to look up stack id -17 (pid 1609075): -2
ERROR: failed to look up stack id -17 (pid 1609080): -2
ERROR: failed to look up stack id -17 (pid 1609056): -2
ERROR: failed to look up stack id -17 (pid 1609064): -2
ERROR: failed to look up stack id -17 (pid 1609068): -2
ERROR: failed to look up stack id -17 (pid 1609089): -2

---

# gemini response

这是一个非常典型的 **eBPF 堆栈映射表溢出（或者哈希冲突）**报错。

简单来说：**因为并发太高、探针太散，内核用来暂存调用栈的内存空间被撑爆了（或者发生了严重的挤兑冲突）。**

我们来详细拆解一下这行报错背后的逻辑：

### 报错信息拆解：`failed to look up stack id -17 (pid ...): -2`

1.  **`stack id -17` (为什么是负数？)**
    正常情况下，当 eBPF 抓到一个调用栈时，会把它存入内核的一块哈希表（Map）中，并返回一个正数的 ID（比如 ID: 105）。
    但是，当你看到返回了 `-17` 时，这是一个 Linux 内核错误码（`-EEXIST`）。它表示**“哈希冲突”**或**“表已满”**。内核在尝试存储一个新的调用栈时，发现坑位已经被占了，存储失败，于是丢弃了这个栈并返回了错误码 `-17`。
2.  **`failed to look up ... : -2`**
    当你按下 `Ctrl+C` 时，用户态的 `bpftrace` 拿着你统计好的数据准备打印。它看到了一个 `stack id = -17` 的记录，于是去内核里问：“请把 17 号栈的具体函数名翻译给我”。
    内核回答：`-2`（即 `-ENOENT`，找不到该条目）。因为 `-17` 根本不是一个有效的存储 ID，所以 `bpftrace` 无法将其还原成文本堆栈，只能无奈地把这行错误抛给你。

---

### 为什么会发生这种情况？（根本原因）

这完全印证了我们在上一步分析出的结论：**宏展开带来的灾难。**

* **物理栈数量爆炸：** 因为 `PAUSE()` 是宏，生成了 1235 个不同的探针点。在 eBPF 看来，只要触发探针的物理内存地址（IP 地址）差了一个字节，它就会认为这是一个**“全新”的调用栈**。
* **高并发雪上加霜：** OceanBase 是极高并发的数据库。短短几秒钟内，这 1235 个探针可能被几百个线程疯狂命中几十万次。
* **内核空间有限：** eBPF 在内核中分配给 `stack` 的哈希表大小是固定的（通常只能存一万多个不同的栈）。这 1235 个微小差异的调用栈，在极短时间内把哈希桶全部占满，导致后续的抓取全部发生 `-17` (EEXIST) 冲突。

---

### 现在的解决方案

你现在面临的数据量已经超出了 `bpftrace` 默认配置能处理的极限。要解决这个问题，你有以下几个选择：

#### 方案一：彻底解决（强烈建议）
**不要再犹豫了，修改 C++ 代码！**
把 `#define PAUSE()` 里面的探针改成调用一个统一的 `.cpp` 实函数。
只要物理探针变成 1 个，调用栈的种类就会从成千上万种瞬间锐减到十几种（只有逻辑上真正不同的路径）。这样绝对不会发生哈希表溢出，而且打印出的数据完全不需要手工折叠，干干净净。

#### 方案二：如果你暂时无法修改代码、重新编译

如果你必须在当前这个 1235 个探针的环境下抓出数据，你必须**“减少数据采集量”**，给内核减负：

1.  **缩短采样时间：**
    不要让它跑太久，运行 `bpftrace` 后，**1 到 2 秒钟**内立刻按下 `Ctrl+C`，抢在哈希表被撑爆之前把数据倒出来。
2.  **增加过滤条件（只抓特定的线程）：**
    修改你的 `aggregate_pause.bt` 脚本，不要抓所有线程，只抓你最关心的某个特定线程（例如只抓 `ob_sql_worker`）：
    ```bpftrace
    usdt:/home/admin/oceanbase/bin/observer:oceanbase:pause_called
    /comm == "ob_sql_worker"/  // 增加过滤条件，极大地减少数据量
    {
        @[ustack, comm] = count();
    }
    ```
3.  **如果报错不多，可以直接忽略：**
    如果你在输出的最后看到了几十行 `ERROR: failed to look up...`，但在它**上面**依然成功打印出了很多正常的调用栈和数字。那么你可以忽略这些错误。这只是意味着“有一小部分并发极高的堆栈在最后几秒钟丢失了”，但你已经抓到的那些大部分数据，依然足够用来做性能瓶颈分析了。

---

# you asked

message time: 2026-06-23 19:16:05

修改代码后 这个需要重新执行吗 还是只需要指定make   bash build.sh rpm --init && cd build_rpm && make -j16

---

# gemini response

**只需要执行 `make -j16`。千万不要重新执行带 `--init` 的脚本！**

对于像 OceanBase 这样千万行代码级别的巨型 C++ 项目，理解编译机制能帮你省下几个小时的时间。

以下是具体的原理解析和实操建议：

### 为什么只需要 `make`？（增量编译机制）
1. **`bash build.sh rpm --init` 的作用：** 这个命令主要是用来做**环境初始化**的。它会去拉取依赖库、生成 CMake 缓存（CMakeCache.txt）、全量生成 Makefile 等等。如果你跑了这个命令，它往往会把之前的编译缓存清空，导致接下来的 `make` 变成**全量重新编译**（可能需要 1 到 2 个小时）。
2. **`make -j16` 的作用：** `make` 具备**增量编译**的能力。当你修改了某个 `.cpp` 或 `.h` 文件后，直接在原来的 `build_rpm` 目录下运行 `make`，编译器会自动比对文件的时间戳，**只重新编译你修改过的文件**以及依赖了那个头文件的其他代码。这通常只需要几秒钟到几分钟。

---

### 正确的实操步骤

假设你已经修改完了代码（把 `PAUSE()` 宏改成了调用 `.cpp` 里的实函数），请按以下步骤操作：

**1. 进入原有的编译目录**
确保你处于之前已经初始化好的编译目录中：
```bash
cd build_rpm
```

**2. 触发增量编译**
直接执行 `make` 即可。如果你只想替换 `observer` 进程的代码来验证探针（不想编译其他测试用例或周边工具，这样速度最快），可以指定目标：
```bash
# 只编译 observer 二进制文件（极快）
make observer -j16

# 或者按照你原来的习惯编译全部目标
make -j16
```

**3. 替换二进制并重启**
编译完成后，新生成的二进制文件通常在 `build_rpm/src/observer/observer`（具体路径视 OceanBase 版本而定）。将其覆盖到你正在运行的测试环境目录（如 `/home/admin/oceanbase/bin/observer`），然后重启 observer 进程即可生效。

### 💡 什么时候才需要重新跑 `--init`？
只有在以下几种情况下，你才需要重新执行 `build.sh --init` 甚至清理 `build_rpm` 目录：
* 你修改了 `CMakeLists.txt`，新增或删除了文件。
* 你修改了编译选项（比如我们之前提到的加上 `-fno-omit-frame-pointer` 来保留 ARM 的栈帧）。
* 你更新了第三方依赖库。
* 你的编译目录彻底损坏或者出现了莫名其妙的链接错误（此时作为终极兜底手段）。