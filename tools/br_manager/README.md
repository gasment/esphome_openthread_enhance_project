# Thread BR 管理台 · 第一版

独立 Web 应用，不修改 BR 固件、ESPHome 组件或 YAML。支持标准 `_meshcop._udp.local.` 发现、手动接入、名称/TAG/能力角色、登录认证、凭据加密保存、每 BR 独立端口的 Basic Auth 代理，以及 iframe / 新窗口访问。

## 推荐部署：Linux + Docker Compose + host 网络

`host` 网络让应用直接使用 Linux 主机网络接口接收 IPv4/IPv6 mDNS，同时动态监听 BR 代理端口，无需为每个 BR 修改 Compose 端口映射。管理台自身仅提供 HTTP，TLS 和证书由外部反向代理负责。宿主机需要 Docker Engine 与 Compose 插件。

```sh
cd tools/br_manager
# 直接编辑 compose.yaml 的 environment：
# 必须替换 BR_ADMIN_PASSWORD，至少 12 个字符。
# 密码中的字面量 $ 写成 $$，避免 Compose 变量插值。
mkdir -p data
sudo chown 10001:10001 data
sudo chmod 700 data
docker compose up -d --build
docker compose logs -f
```

Compose 仅保留构建、host 网络、自动重启、初始用户名/密码与本地目录挂载配置，其余使用程序默认值，无需创建 `.env`。默认用户名为 `admin`，可通过 `BR_ADMIN_USER` 修改（首次初始化时生效）。`build: .` 需要同目录下的 Dockerfile、源码和 requirements.txt，因此应部署整个 `br_manager` 目录。`.env.example` 仅保留为可选环境变量参考，默认 Compose 不读取它。

打开 `http://Linux主机IP:8080/`。首次启动创建单管理员；已有数据库时不会用环境变量覆盖密码。完成初始化后，可从 `compose.yaml` 的 `environment` 删除 `BR_ADMIN_PASSWORD` 并重建容器；数据库中的密码哈希保留。

默认监听：

| 端口 | 用途 |
|---|---|
| TCP 8080 | 管理台 |
| TCP 18001–18100 | 自动分配的 BR Web 代理入口；只监听已接入设备使用的端口 |
| UDP 5353 | 标准 mDNS 发现，需要允许所在 LAN 的组播收发 |

默认 HTTP 监听 IPv4 `0.0.0.0`；BR 上游支持 IPv4、IPv6 和域名，不受管理台监听地址限制。浏览器侧 IPv6 可由外层反代监听。需要原生 IPv6 监听时可设置 `BR_BIND=::`；IPv4 是否同时可达取决于主机的 IPv6 双栈设置。

若 Linux 上已运行 Avahi，应允许 mDNS 共享套接字；遇到发现问题，检查日志和管理台右侧提示，必要时设置 `BR_MDNS_INTERFACES` 指定本机网卡地址。跨 VLAN 不会自动穿透，需要网络侧提供 mDNS 转发，或手动添加。

## 接入 BR

1. 从右侧“局域网发现”选择接入，或点击“手动添加 BR”。发现不需要 BR Web 凭据。
2. 填写名称、TAG、Thread 网络分组、Web 地址和能力角色。角色是手动标签，不自动改变 BR 配置。
3. 填写 BR 的 Basic Auth 用户名、密码。设备未开启认证时两项留空。编辑时默认保留已存凭据，勾选“更新 BR 凭据”才替换；勾选后两项留空可清除。
4. 分配独立代理端口，例如 `18001`。端口冲突会拒绝保存；可更换端口。
5. 保存后点击“打开 Web”“网关”或“配网”，在 iframe 中访问；也可新窗口打开。

已接入设备使用紧凑单列列表。点击 Web 入口后，iframe 在对应条目下方展开，后续条目向下顺延；同一时间展开一个 BR。打开时自动将该条目对齐到窗口顶部，条目信息、访问工具栏和 iframe 容器共同适配当前窗口高度。BR 页面较长时在 iframe 内滚动，无需滚动管理台来寻找 iframe 底部。切换入口、调整窗口大小时会重新适配。

定时刷新与保留当前条目的筛选不会重载 iframe，也不会清除 BR 页面中未提交的输入。筛选结果排除当前条目、移除条目或退出登录时收起 iframe。

局域网发现仅展示未接入的 BR：按 MeshCoP 服务标识匹配，也按已保存上游的主机名/IP 匹配手动添加的设备；不会仅因网络名称或 Extended PAN ID 相同就隐藏其他 BR。

`_meshcop` 中的 UDP 端口是 Border Agent 端口，**不能用作 Web 端口**。发现只预填地址与 Thread 信息，Web 默认 80，需要用户确认。保存的内部 ID 独立于名称和 IP；同名 Thread 网络仍可用 Extended PAN ID 和 TAG 区分。

支持上游地址：

```text
http://192.168.32.50:80/
http://[fd12:3456::1234]:80/
http://[fe80::1234%25eth0]:80/
```

IPv6 链路本地地址需要 Linux 主机的正确网卡作用域（可以使用接口编号），不同 Linux 主机的网卡编号可能不同。地址只接受根 URL，不接受用户名、密码、路径或查询参数。

第一版发现结果不会自动改写已保存的上游地址，避免把凭据自动发送到未经确认的新地址。BR 更换 IP 时，从发现结果确认地址并编辑设备；推荐对 BR 配置 DHCP 保留地址。公告离线与 Web 离线分别显示，不相互推断。

## 不用域名：按端口代理

纯 HTTP 测试可以直接使用同一个 IP：

```text
http://192.168.32.10:8080/    管理台
http://192.168.32.10:18001/   BR A 代理
http://192.168.32.10:18002/   BR B 代理
```

同一主机名/IP 的 Cookie 可跨端口使用。管理台签发 HttpOnly 登录 Cookie，各 BR 代理入口验证同一个服务端会话，再向对应 BR 添加 Basic Auth。上游请求剥离管理台 Cookie、浏览器 Authorization 和转发头；上游 Cookie 不写回浏览器。当前 BR Web 使用 Basic Auth，不依赖 Cookie 登录。

用户不必在 iframe 中再次登录。登出会撤销服务端会话，旧 Cookie 也无法继续发起代理请求；已显示的页面不会被远程抹除，iframe 会在当前管理台退出时清空。会话默认 12 小时，重启应用仍可继续使用，过期后需重新登录。

## 对接你自己的 HTTPS 反代

管理台与每个 BR 代理端口分别增加一个外部入口，全部使用**同一个主机名或 IP、不同端口**。例如：

| 浏览器入口 | 反代上游 |
|---|---|
| `https://192.168.32.10:8443/` | `http://127.0.0.1:8080/` |
| `https://192.168.32.10:18443/` | `http://127.0.0.1:18001/` |
| `https://192.168.32.10:18444/` | `http://127.0.0.1:18002/` |

BR A 的“外层反代访问地址”填 `https://192.168.32.10:18443/`，BR B 填 `https://192.168.32.10:18444/`。这些地址仅供浏览器打开代理，不改变实际 BR 上游地址。

在 `compose.yaml` 的 `environment` 下按需添加 `BR_COOKIE_SECURE: "true"` 后重建容器，浏览器应始终使用 HTTPS。若证书使用 IP，证书必须包含相应 IP SAN 并受客户端信任；本应用不签发证书。

外层反代需要：

- 保留浏览器的原始 `Host`（包含外部端口）、`Origin` 和 Cookie；Nginx 使用 `proxy_set_header Host $http_host;`。本应用不依据不受信任的 `X-Forwarded-*` 判断身份。
- 将完整路径原样转发，避免用路径前缀挂载；BR 原网页使用根路径。
- 允许管理台嵌入 BR 代理入口，不要给 BR 代理响应添加 `X-Frame-Options: SAMEORIGIN/DENY`；不同端口属于不同 origin。若设置 CSP，`frame-ancestors` 需明确允许管理台的完整 HTTPS origin，管理台的 `frame-src` 则需允许 BR 入口。
- 拓扑诊断可能较慢，建议反代读取超时不低于 60 秒；需要流式显示时关闭代理缓冲。
- 若外层反代也运行在 Docker bridge 网络中，`127.0.0.1` 是该反代容器自身，需改为 Linux 宿主机地址，或让反代同样使用 host 网络。

不要在管理台使用 IP、BR 代理使用另一域名，或混用 `localhost` 与 IP，否则浏览器不会共享登录 Cookie。不同端口不需要额外 CORS 设置，因为 BR 页面与其 API 都在各自代理入口内部访问。管理台不读取跨 origin iframe 的 DOM。

## 运行边界与数据

- 每 60 秒检测一次已保存 BR 的 Web 可达性，区分正常、认证失败、HTTP 异常、无法连接；这是 Web 状态，不代表 Thread 已附着或边界路由转发正常。
- 不自动运行拓扑诊断，不修改 Dataset，不接管或解绑 gateway 节点。“移除”只删除管理台记录和监听入口。
- 代理支持当前组件的 HTTP 页面、gzip 静态资源、GET/POST API 和流式响应；第一版不支持 WebSocket，不代理任意第三方登录体系。
- 单管理员、无多用户权限体系。管理台的写请求和 BR 代理写请求均校验 Origin；同主机不同端口也不能跨入口直接发起写请求。
- `_meshcop` 公告不证明设备可信。只有管理员确认接入的上游会收到凭据；代理目标由设备记录和监听端口确定，客户端不能指定任意转发目标。
- 通过外部 HTTPS 入口保护浏览器到管理主机的凭据；管理主机到 HTTP BR 的 Basic Auth 仍是可逆编码，部署在可信 LAN。

持久数据通过 `./data:/data` 绑定挂载（bind mount）保存在 `compose.yaml` 同目录的 `data` 文件夹中，不使用 Docker 命名卷。容器以 UID/GID `10001:10001` 运行，因此首次部署先按上面的命令设置目录权限。

```text
/data/manager.sqlite3     设备配置、管理员 scrypt 密码哈希、会话哈希
/data/credentials.key     BR 凭据 Fernet 加密密钥
```

两者必须一起备份，密钥不应放进 Git。加密用于避免仅数据库文件泄露时直接暴露 BR 密码；同时取得密钥和数据库仍可解密。停容器后备份整个本地 `data` 目录，或使用 SQLite backup API 获得一致快照。不要只复制运行中的数据库主文件而忽略 WAL。

重置管理员密码（交互输入，不在命令行明文传递）：

```sh
docker compose exec br-manager python app.py reset-password
```

此操作撤销全部已登录会话，但不修改 BR 密码。

## 不用 Docker 的运行方式

Linux Python 3.12：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export BR_ADMIN_PASSWORD='your-long-random-password'
python app.py
```

原生运行使用环境变量传入配置。正式常驻运行推荐 Docker Compose，便于管理依赖、重启和备份。

## 验证

```sh
python -m unittest discover -s tests -v
```

测试创建本地模拟 BR，不访问真实设备。验证发现记录解析、凭据加密持久化、缺失密钥拒绝启动、登录/退出、跨端口 Cookie 代理、Origin 校验、Basic Auth、gzip 字节保真、原始 URL、重定向、端口冲突、凭据保留与删除。

本轮 11 项测试全部通过，包含 IPv6 loopback 上游、重启恢复代理/会话和过期会话拦截。另外使用本机 Edge 完成了浏览器检查：登录、从发现列表接入、TAG 筛选、编辑保留凭据、iframe 内绝对路径静态资源和 POST API、手机布局、退出；未发现 JavaScript 错误。

布局回归另见 `tests/browser_layout.py`（可选测试依赖 Playwright，不进入运行镜像）：

```sh
pip install playwright
playwright install chromium
python tests/browser_layout.py
```

也可通过 `BR_BROWSER_EXECUTABLE` 指定现有 Chrome/Edge 可执行文件路径。测试仅创建本地模拟 BR，验证多条目的单列紧凑布局、条目内展开、1440×900 / 1280×720 / 390×844 / 844×390 下的一屏适配、刷新/筛选保留 iframe 状态、发现列表排除已接入设备，以及切换、收起、删除和退出。上述检查已通过本机 Edge 验证。

Linux Docker 实际构建、局域网 mDNS、IPv6 链路本地连接和用户现有反代需要在部署主机联调。没有运行 ESPHome 固件编译。
