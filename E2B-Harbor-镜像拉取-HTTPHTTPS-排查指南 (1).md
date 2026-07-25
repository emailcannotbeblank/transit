# E2B + Harbor 镜像拉取 HTTP/HTTPS 错误排查指南

> 本文档整理自一次自建 E2B 环境中通过 Harbor 私有仓库构建模板镜像时反复报错的真实排查过程。
> 全程围绕一个核心矛盾——**镜像拉取请求方与 Harbor 服务方在协议（HTTP / HTTPS）和证书信任上始终没有对齐**——梳理出完整的前因后果与解决方案。

---

## 1. 背景与运行环境

- 自建 E2B 平台（编排器地址 `141.61.5.131:3000`）。
- 私有镜像仓库 Harbor，部署在 `141.61.5.131` 上，宿主机以 Docker Compose 方式启动。
- E2B 的构建编排器 `template-manager` 由 Nomad 调度，使用 `raw_exec` 驱动，**直接以宿主机二进制进程方式运行**（这一点很关键，后面会反复用到）。
- 构建请求通过 Python 脚本 `fsandbox.py` 调用 `e2b` SDK 发起。

涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `harbor/harbor.yml` | Harbor 主配置，决定 HTTP/HTTPS 端口与证书路径 |
| `harbor/install.sh` | Harbor 安装脚本，会调用 `./prepare` 并 `docker-compose up -d` |
| `template-manager.hcl` | Nomad Job 定义，里面用环境变量把 `template-manager` 绑死到 HTTPS+证书上 |
| `fsandbox.py` | 客户端测试脚本，调用 `Template().from_image(...).build(...)` |
| `/etc/docker/daemon.json` | 宿主机 Docker 的 insecure-registries 配置 |
| `/etc/docker/certs.d/harbor:443/ca.crt` | 宿主机 Docker 信任的 Harbor CA 证书 |

---

## 2. 报错链路全景

整个排查过程中一共出现过 **4 类**递进式的报错，每一类都是前一类被部分修复后露出的下一层问题：

| 顺序 | 报错关键字 | 本质原因 |
| --- | --- | --- |
| ① | `http: server gave HTTP response to HTTPS client` | 客户端用 HTTPS 请求了只开 HTTP 的 Harbor 端口 |
| ② | `Unsuitable value: a number is required` (Nomad) | 直接 `nomad job run` 原始模板，`${...}` 变量未替换 |
| ③ | `x509: certificate relies on legacy Common Name field, use SANs instead` | 自签证书只填了 CN，新版 Go 校验要求 SAN 扩展 |
| ④ | `x509: certificate signed by unknown authority` | 自签证书未加入宿主机系统级 CA 信任库 |

---

## 3. 根因分析：为什么会“跨服聊天”

### 3.1 第一层矛盾：协议不一致

`harbor.yml` 中只配置了 HTTP，HTTPS 整块被注释：

```yaml
hostname: 141.61.5.131
http:
  port: 2900        # ← 只开了 HTTP
#https:
#  port: 443
#  certificate: ...
#  private_key: ...
```

但 Nomad 的 `template-manager.hcl` 在 `env` 块里写死了：

```hcl
SSL_CERT_FILE = "/etc/docker/certs.d/harbor:443/ca.crt"
GCP_DOCKER_REPOSITORY_NAME = "${HARBOR_HOST}"
```

也就是说，**E2B 编排器从设计上就假定 Harbor 一定跑在 443 端口的 HTTPS 上**，并强制要求读取一个 CA 证书来校验。无论客户端镜像地址写的是 `harbor:443`、`harbor:2900` 还是 `141.61.5.131:2900`，底层拉取逻辑都会倾向于走 HTTPS，于是出现“客户端发 HTTPS、服务端回 HTTP”的协议冲突。

### 3.2 第二层矛盾：DNS 与网络作用域

Python 脚本里有一段 `patched_getaddrinfo` 用来把 `e2b` / `harbor` 等域名强行解析到 `141.61.5.131`：

```python
def patched_getaddrinfo(host, port, ...):
    if host and ("e2b" in host or "localhost" in host or "-" in host):
        return _original_getaddrinfo("141.61.5.131", port, ...)
```

这只影响 **Python 脚本自身进程** 的 DNS，**真正执行 `pull` 的 `template-manager` 进程并不受影响**。所以脚本里的 DNS 拦截对修复拉取错误无效，只是一个排查时的“烟雾弹”。

> 教训：遇到镜像拉取类错误时，要分清“谁去拉”。
> - SDK 脚本：只是把构建请求提交给 E2B 后端。
> - `template-manager`（宿主机二进制 / `raw_exec`）：用宿主机的网络、DNS、CA 信任库去真正拉取镜像。
> 任何只在脚本侧做的网络/证书动作，对真正拉取方都没有作用。

### 3.3 第三层矛盾：`raw_exec` 直接吃宿主机环境

`template-manager` 不是跑在容器里，而是 `raw_exec` 裸进程，因此：

- 它读的证书不是容器内的，而是**宿主机的 `/etc/docker/certs.d/harbor:443/ca.crt` 和系统 CA 信任库**。
- 它使用的容器运行时（Docker / containerd / BuildKit）也是**宿主机本身的那一套**。

这就决定了所有配置都必须落在宿主机层面，而不是去改容器内部。

---

## 4. 解决方案选型

面对“HTTP / HTTPS 二选一”，有两条路：

| 路线 | 做法 | 评估 |
| --- | --- | --- |
| **HTTP 路线** | 注释掉 HCL 里的 `SSL_CERT_FILE`，宿主机 Docker `daemon.json` 加 `insecure-registries` | 最省事，但需要改 E2B 默认设计，且底层可能还跑 containerd/BuildKit，需要逐个放行 |
| **HTTPS 路线（推荐）** | 给 Harbor 配证书开 443，并把证书塞进 `template-manager` 早已预留的路径 | 顺着 E2B 设计走，链路天然对齐 |

最终选定的方案是 **HTTPS 路线**。原因是：E2B 的 `template-manager.hcl` 本身就是为 HTTPS+证书准备的，去对抗它要改动多处（Nomad、Docker、containerd/BuildKit），反而更脆弱。

---

## 5. HTTPS 路线完整实施步骤

### 5.1 生成带 SAN 扩展的自签证书

旧版一句命令 `openssl req -newkey ... -subj "/CN=harbor"` 只签 CN，会被新版 Go 拒绝：

```
x509: certificate relies on legacy Common Name field, use SANs instead
```

正确做法是显式带上 `subjectAltName`，同时覆盖 DNS 和 IP：

```bash
# 1) 准备 SAN 配置文件
cat > san.cnf <<'EOF'
[req]
default_bits       = 4096
prompt             = no
default_md         = sha256
distinguished_name = dn
x509_extensions    = v3_ext

[ dn ]
C  = CN
O  = E2B
CN = harbor

[ v3_ext ]
subjectAltName = @alt_names

[ alt_names ]
DNS.1 = harbor
IP.1  = 141.61.5.131
EOF

# 2) 生成 10 年期证书和私钥
sudo mkdir -p /data/cert
sudo openssl req -x509 -nodes -days 3650 -newkey rsa:4096 \
  -keyout /data/cert/server.key \
  -out    /data/cert/server.crt \
  -config san.cnf

# 3) 赋予读取权限
sudo chmod 644 /data/cert/server.crt /data/cert/server.key
```

> 要点：SAN 必须同时包含访问会用到的所有标识（这里是 `harbor` 域名与 `141.61.5.131` IP）。否则客户端用某个域名/IP 访问时会因 SAN 不匹配继续报错。

### 5.2 修改 `harbor.yml` 开启 HTTPS

```yaml
https:
  port: 443
  certificate: /data/cert/server.crt
  private_key: /data/cert/server.key
```

并保留 `http: port: 2900`（启用 HTTPS 后它会自动跳转到 443）。

### 5.3 重启 Harbor

在 `harbor.yml` 所在目录执行：

```bash
docker-compose down -v
./prepare
docker-compose up -d
```

#### 可能的坑：443 端口被占用

如果 `nginx` 容器启动时报：

```
bind: address already in use
```

排查与处理：

```bash
sudo lsof -i :443
# 或
sudo netstat -tulnp | grep :443
```

- 若是无关服务（如系统自带 nginx / apache），直接停掉：

  ```bash
  sudo systemctl stop nginx && sudo systemctl disable nginx
  # 或 sudo kill -9 <PID>
  docker-compose up -d
  ```

- 若占用进程不能停，则把 Harbor 的 HTTPS 端口改成 `8443`（**注意**：同时要把 Nomad 中 `harbor:443`、Python 脚本中的 `harbor:443` 以及 `/etc/docker/certs.d/harbor:443/` 目录一并改名为 `harbor:8443`，否则证书目录和镜像地址会对不上）。

### 5.4 把证书放到 Docker / system trust store 两处

**① Docker 私有仓库证书目录**（`template-manager` 的 `SSL_CERT_FILE` 默认就指向这里）：

```bash
sudo mkdir -p /etc/docker/certs.d/harbor:443
sudo cp /data/cert/server.crt /etc/docker/certs.d/harbor:443/ca.crt
sudo chmod 644 /etc/docker/certs.d/harbor:443/ca.crt
```

**② 宿主机系统 CA 信任库**（关键，否则会报 `signed by unknown authority`）。

`/etc/docker/certs.d/` 只对 Docker 引擎生效，而 `template-manager` 是 Go 写的裸进程，校验用的是 **系统全局 trust store**，必须把证书也追加进去：

CentOS / RHEL / Rocky / AlmaLinux：

```bash
sudo cp /data/cert/server.crt /etc/pki/ca-trust/source/anchors/harbor.crt
sudo update-ca-trust
```

Ubuntu / Debian：

```bash
sudo cp /data/cert/server.crt /usr/local/share/ca-certificates/harbor.crt
sudo update-ca-certificates
```

### 5.5 保留 / 恢复 Nomad `template-manager` 的证书配置

如果之前为了尝试 HTTP 路线在 Nomad 里删过 `SSL_CERT_FILE` 这一行，现在要把它加回去（通过 Nomad 前端 UI 的 **Edit/Definition** 功能编辑正在运行的成品 Job，千万别直接 `nomad job run` 原始模板，原因见 5.6）：

```hcl
env {
  ...
  SSL_CERT_FILE = "/etc/docker/certs.d/harbor:443/ca.crt"
  ...
}
```

然后 Plan → Run 重启任务。

### 5.6 不要直接 `nomad job run template-manager.hcl`

仓库里的 `.hcl` 是带 `${TEMPLATE_MANAGER_PORT}` 等占位符的**模板**，未经 E2B 安装脚本渲染就直接 `nomad job run` 会被 Nomad 拒绝：

```
template-manager.hcl:26,18-44: Unsuitable value type;
Unsuitable value: a number is required
```

正确做法是在 **Nomad 前端 UI** 中编辑已经在运行的 Job（其中的变量已被替换为真实值），保存并 Plan / Run。如果只是改了 `daemon.json` 或证书文件而没改 HCL，直接在 UI 里对 Allocation 点 **Restart** 即可。

### 5.7 客户端脚本改回 443

`fsandbox.py` 中镜像地址务必与系统信任的域名/IP 保持一致（这里证书的 SAN 包含 `harbor`，所以优先用域名访问以完成校验）：

```python
template = (
    Template()
    .from_image(
        "harbor:443/e2b-orchestration/django-bench-image:v2",
        username="admin",
        password="Harbor12345"
    )
)
```

### 5.8 重启 `template-manager`

系统 CA 库更新后，正在运行的 `template-manager` 进程需要重启才能重新加载证书链：

```bash
# 推荐：Nomad UI 里 Restart
# 或（已渲染的 hcl）：
nomad job stop template-manager
nomad job run <渲染后的 hcl>
```

---

## 6. 备用方案：HTTP 路线（当无法启用 HTTPS 时）

若因 443 被占用且无法释放、或找不到证书私钥等不可抗力，被迫走 2900 HTTP 端口，则需要同时改三处：

1. **Nomad `template-manager.hcl`**：注释或删除 `SSL_CERT_FILE = "/etc/docker/certs.d/harbor:443/ca.crt"` 一行，重启 Job。
2. **宿主机 Docker** `/etc/docker/daemon.json`：

   ```json
   {
     "insecure-registries": ["141.61.5.131:2900", "harbor:2900"]
   }
   ```

   ```bash
   sudo systemctl daemon-reload && sudo systemctl restart docker
   ```

3. **底层运行时（如使用 containerd 或 BuildKit）也必须各自放行**：

   - containerd（`/etc/containerd/config.toml`）：

     ```toml
     [plugins."io.containerd.grpc.v1.cri".registry.mirrors."141.61.5.131:2900"]
       endpoint = ["http://141.61.5.131:2900"]

     [plugins."io.containerd.grpc.v1.cri".registry.configs."141.61.5.131:2900".tls]
       insecure_skip_verify = true
     ```

     ```bash
     sudo systemctl restart containerd
     ```

   - BuildKit（`/etc/buildkit/buildkitd.toml`）：

     ```toml
     [registry."141.61.5.131:2900"]
       http = true
       insecure = true
     ```

4. **Python 脚本** 镜像地址改用 `141.61.5.131:2900/...`。

> 注意：HTTP 路线的脆弱点在于“真正拉镜像的运行时”可能不止一个。只要有一个（Docker / containerd / BuildKit）没放行，就会继续报 `server gave HTTP response to HTTPS client`。这也是不推荐此路线的重要原因。

---

## 7. 必备配套：让 `harbor` 域名在拉取侧可解析

报错日志里出现 `Get "https://harbor:443/v2/..."`，说明 `template-manager` 在用 `harbor` 这个主机名去拉取。若拉取侧宿主机没有内网 DNS 解析 `harbor`，会接着报 `dial tcp: lookup harbor: no such host`。

在 `141.61.5.131`（运行 `template-manager` 的那台）上配置：

```text
# /etc/hosts
141.61.5.131  harbor
```

---

## 8. 关键配置项汇总表

| 层级 | 配置位置 | 关键内容 |
| --- | --- | --- |
| Harbor 服务端 | `harbor.yml` | `https.port: 443` + `certificate` / `private_key` 路径 |
| Harbor 重载 | harbor 目录 | `docker-compose down -v` → `./prepare` → `docker-compose up -d` |
| 证书文件 | `/data/cert/server.{crt,key}` | 自签发，**必须带 SAN**（DNS + IP） |
| Docker 仓库信任 | `/etc/docker/certs.d/harbor:443/ca.crt` | 复制 server.crt 到此 |
| 系统全局信任 | CentOS: `/etc/pki/ca-trust/source/anchors/` 后 `update-ca-trust`<br>Ubuntu: `/usr/local/share/ca-certificates/` 后 `update-ca-certificates` | 让 `template-manager` 这个 Go 进程能校验通过 |
| Nomad 编排 | `template-manager.hcl` 的 `env.SSL_CERT_FILE` | 指向 `/etc/docker/certs.d/harbor:443/ca.crt` |
| 客户端 | `fsandbox.py` 的 `from_image` | 使用证书 SAN 中包含的标识（`harbor:443`） |
| DNS | 拉取侧宿主机 `/etc/hosts` | `141.61.5.131 harbor` |

---

## 9. 排查过程复盘：每一步为什么必要

1. **先看 Harbor 服务端到底开的是什么协议**：`harbor.yml` 决定一切。如果 HTTPS 整块被注释，那它就只有 HTTP，任何 HTTPS 请求都会协议冲突。
2. **再看拉取方到底被“逼”成什么协议**：`template-manager.hcl` 中的 `SSL_CERT_FILE` 把它绑死到 HTTPS+443。这是“跨服聊天”的源头。
3. **分清“提交构建的脚本”与“真正拉镜像的进程”**：脚本里的 DNS 拦截、`E2B_*` 环境变量只影响脚本自己；真正拉取的是宿主机上的 `template-manager` 裸进程，要用宿主机的 DNS、CA 信任库、容器运行时配置。
4. **选 HTTPS 路线时要“证书三件套”齐全**：
   - 服务端加载证书（`harbor.yml`）；
   - Docker 信任证书（`/etc/docker/certs.d/...`）；
   - 系统信任证书（`update-ca-trust` / `update-ca-certificates`）。
   缺任何一件，都会在某一层继续报错。
5. **证书格式要符合现代 Go 校验**：必须带 SAN。只填 CN 会被新版 Go 直接拒绝。
6. **端口冲突要先解决**：443 被占用会让 Harbor 的 nginx 起不来，必须释放端口或改用其它 HTTPS 端口（并同步改所有引用）。
7. **改完任何一层都要重启对应的进程**：改 `daemon.json` 重启 Docker；改 CA 库重启 `template-manager`（因为它已把旧 CA 加载进内存）。
8. **不要 `nomad job run` 原始模板**：模板里的 `${VAR}` 必须先被 E2B 安装脚本渲染，否则 Nomad 校验失败。在 UI 上编辑运行中的 Job 才是已渲染版本。

---

## 10. 一句话总结

> 报错的本质始终是“协议 / 证书在客户端和服务端没有完全对齐”。顺着 E2B 既有的 HTTPS 设计走，把一份带 SAN 的自签证书同时配到 Harbor、Docker、系统 CA 三处，并在拉取侧让 `harbor` 域名可解析，整条链路即可一次性打通；反之去对抗 HTTPS、走 insecure HTTP 路线，则要在 Docker / containerd / BuildKit 三套运行时同时放行才能勉强工作，且对未来升级极不友好。