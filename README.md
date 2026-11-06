# wonton-agent-base

Wonton 的 ACP Agent 插件（Codex、omp、Claude Code、Kimi……）共用这个底座镜像：入口脚本 +
ACP 转发 + 模型网关约定 + 闭源 CLI 首次启动下载。做新 Agent 插件时不再各写一份入口脚本，
子镜像只写「装哪个 CLI、ACP 怎么启动、网关怎么写」——见 `examples/echo-agent/`，四个文件就能跑。

## 每次容器启动，入口做什么

`/usr/local/bin/wonton-agent`（Python 3 标准库）读 `/etc/wonton-agent/agent.toml`，依次：

1. **按需安装闭源 CLI**（`[install]`，可选）：首次启动从官方源下载到数据卷
   `/data/agent/tools/<名字>/<版本>/`，摘要核对通过才落盘；版本目录已存在就不重下。
   下载走容器里核心注入的代理变量（`HTTPS_PROXY` 等），入口不自己写代理。
2. **装 skill 和系统提示**（`[skills]`/`[prompt]`，可选）：把镜像里
   `/usr/local/share/wonton-agent/skills/*` 和 `prompt.md` 拷到 agent.toml 指定的目录，
   每次启动覆盖，跟着插件版本走。
3. **模型网关**（`[gateway]`，可选）：核心总会注入 `WONTON_MODEL_BASE_URL`、
   `WONTON_MODEL_API_KEY`、`WONTON_MODEL_DEFAULT`（没接网关时是空字符串，网关就绪后核心
   重建容器并注入值）。三个变量都有值时调钩子 `apply`，任一为空调 `clear`。
4. **启动 ACP 转发**：在 `$WONTON_ACP_SOCKET`（默认 `/data/run/acp.sock`）监听；每个连接
   起一次 `[acp] command`（stdio 说 ACP）双向转发，连接断了杀掉子进程；转发时去掉终端
   配色码（`[acp] strip_ansi = false` 可关）；子进程 stderr 直接进容器日志。

socket 在前面几步全部成功之后才建，compose 的健康检查以 socket 文件为准——socket 不存在
就是还没就绪（首启下载 CLI 可能要几分钟），桥和核心会重试。

## agent.toml

```toml
# 闭源 CLI：首次启动从官方源下载（可选；开源 CLI 直接打进子镜像就省掉这段）。
[[install]]
name = "kimi"                      # 装到 /data/agent/tools/kimi/<version>/
version = "2.4.0"

# 来源一：npm 包（registry.npmjs.org 的 tgz）。integrity 是 npm dist.integrity
# 的 sha512-<base64>（或 128 位十六进制）。registry 字段可省，缺省官方源。
source = "npm"
package = "@scope/kimi-cli"
integrity = "sha512-h8Fw1D3l…"

# 来源二：按架构给 URL 加 sha256。format = "binary"（单个可执行文件，缺省）
# 或 "tgz"（解包安装）。
# source = "url"
# url_amd64  = "https://example.com/kimi-2.4.0-linux-x64"
# url_arm64  = "https://example.com/kimi-2.4.0-linux-arm64"
# sha256_amd64  = "<64 位十六进制>"
# sha256_arm64  = "<64 位十六进制>"
# format = "binary"

[skills]
target = "~/.kimi/skills"          # 镜像里 skills/* 拷到这里

[prompt]
target = "~/.kimi/PROMPT.md"       # 可选：镜像里 prompt.md 拷到这里

[gateway]
hook = "/usr/local/lib/wonton-agent/gateway"   # 插件自己的可执行文件，收 apply/clear

[acp]
command = ["/data/agent/tools/kimi/2.4.0/node_modules/.bin/kimi-acp"]
strip_ansi = true                  # 缺省 true
```

npm 来源解包后会在目录里跑 `npm install --prefix <目录> --ignore-scripts` 装依赖：
不执行包里的安装脚本，闭源 CLI 的安装代码不碰容器其余部分。
`[install]` 的 name 只允许字母数字点下划线连字符；配置有拼错的段、缺字段、摘要不合法，
入口启动即报错退出，写清是哪一项。

### 开源 CLI：打进子镜像

```toml
[acp]
command = ["/usr/local/bin/omp", "acp"]

[skills]
target = "~/.agents/skills"
```

### 闭源 CLI：首次启动下载

见上面完整例子。`[acp] command` 直接指向数据卷里的安装位置；容器重建（升级、网关就绪）
时数据卷还在，不会重下。

## 模型网关钩子

钩子是子镜像提供的可执行文件，底座带着全部环境变量调它：

- `apply`：三个 `WONTON_MODEL_*` 变量都有值。钩子把网关写进这个 Agent 的配置。
- `clear`：任一变量为空。钩子把网关从配置里移出去。

**密钥规则**：配置里只引用变量名（如 `env_key = "WONTON_MODEL_API_KEY"`），值留在环境变量里；
Agent 不支持从环境变量读时才把值写进文件，且文件权限 0600。钩子非零退出、不可执行，
容器启动失败并写明原因。

## 子镜像怎么写

```dockerfile
# 发布后把 digest 固定上，防镜像漂移：
# FROM ghcr.io/geraldpeng6/wonton-agent-base:0.1.0@sha256:<发布后的摘要>
ARG BASE_IMAGE=ghcr.io/geraldpeng6/wonton-agent-base:0.1.0
FROM ${BASE_IMAGE}
COPY agent.toml /etc/wonton-agent/
COPY --chmod=0755 gateway /usr/local/lib/wonton-agent/gateway
COPY skills/ /usr/local/share/wonton-agent/skills/
```

两个权限的坑：底座以 `USER 65532` 收尾，子镜像里 `RUN chmod` 会因非 root 失败（要跑命令就先
`USER root` 再 `USER 65532:65532` 切回去）；`COPY --chmod` 会连自动创建的目录一起改，往不存在的
目录里塞 0644 会把目录的 x 位也去掉（读不了）。skill 树直接 `COPY`，源码里的权限位随 git 走。

用户（65532:65532）、`ENTRYPOINT`、`HOME=/data/agent/home`、`WONTON_ACP_SOCKET`、
`WONTON_AGENT_WORKSPACE` 都从底座继承，不用重写。compose 侧：

```yaml
services:
  agent:
    image: ghcr.io/geraldpeng6/wonton-agent-xxx:<版本>
    user: "65532:65532"
    restart: unless-stopped
    environment:
      WONTON_ACP_SOCKET: /data/run/acp.sock
      WONTON_AGENT_WORKSPACE: /workspace
      # 空值表示没接网关，网关就绪后核心重建容器并注入；底座按有没有值调钩子。
      WONTON_MODEL_BASE_URL: ${WONTON_MODEL_BASE_URL:-}
      WONTON_MODEL_API_KEY: ${WONTON_MODEL_API_KEY:-}
      WONTON_MODEL_DEFAULT: ${WONTON_MODEL_DEFAULT:-}
      # 出网代理由核心注入，这里不要写代理变量，写了空值会把核心的注入顶掉。
    volumes:
      - ${WONTON_BOT_DATA:?需要 Bot 数据目录}/agent:/data/agent
      - ${WONTON_BOT_DATA}/run:/data/run
      - ${WONTON_BOT_DATA}/workspace:/workspace
    healthcheck:
      test: ["CMD", "test", "-S", "/data/run/acp.sock"]
      interval: 30s
      timeout: 5s
      start_period: 120s      # 首启要下载闭源 CLI，留够
      start_interval: 2s
```

## 样例：examples/echo-agent

最小 ACP Agent（回显 prompt）+ `agent.toml` + `Dockerfile`，演示 skill 安装和网关钩子。
本机验证（构建底座和样例、起容器、连 socket 发 `initialize` 和一次 `session/prompt`）：

```sh
docker build -t ab-agent-base:0.1.0 .
docker build -t ab-echo-agent:0.1.0 examples/echo-agent \
    --build-arg BASE_IMAGE=ab-agent-base:0.1.0
docker run -d --name ab-echo-test ab-echo-agent:0.1.0
# 等几秒后连 /data/run/acp.sock 发 ACP 消息，拿回显；测完 docker rm -f ab-echo-test
```

## 测试和发布

- `tests/` 是 Python unittest（只用标准库，不连外网，假 HTTP 服务装包）：配置解析、
  网关 apply/clear、npm 和 URL 两种安装的摘要通过/失败、同版本不重下、socket 往返。
  `python3 -m unittest discover -s tests -v`
- 发布：GitHub Actions 手动触发（**发布** → Run workflow），先跑测试再出 amd64+arm64
  镜像，推 `ghcr.io/geraldpeng6/wonton-agent-base:<VERSION>`，版本号在仓库根 `VERSION`。

## 设计取舍

- **闭源 CLI 不打进发布的镜像**：底座镜像和摘要要能公开复现，闭源 CLI 从官方源按版本和
  摘要核对后下载到数据卷，由 agent.toml 声明。
- **codex 的两段专用逻辑不在底座里**：`rewrite_auth_methods`（initialize 应答把登录方式
  改写成终端登录）和 `serve_login`（第二个 socket 跑终端登录进程）绑定 codex 的登录命令
  和桥的登录协议，且没有桥就无法端到端验证。留在 wonton-plugin-codex 自己的入口里；
  等第二个需要终端登录的 Agent 出现，再把通用部分提上来。
- **skill 每次覆盖、不清理旧文件**：HOME 是挂载点，镜像里的内容拷不进去，只能启动时铺；
  照搬 wonton-plugin-codex 的既有行为。
- **socket 权限 0600**：只有桥和宿主诊断连得上。
- **底座不做的事**：不写代理、不放假数据、不静默降级；取不到、校验不过、钩子失败，
  报错退出让核心看到。
