# Wonton Agent 插件的共用底座：入口包（按职责拆模块）、ACP 转发、网关钩子约定、闭源 CLI 首次启动下载。
# 闭源 CLI 不打进发布的镜像（要跟官方源、按版本和摘要核对），由入口按 agent.toml 在
# 首次启动时下载到数据卷 /data/agent/tools/。
FROM public.ecr.aws/docker/library/debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

ARG TARGETARCH
# Node.js LTS（nodejs.org 官方二进制）：[install] source = "npm" 时，入口用它
# npm install --ignore-scripts 装依赖。版本和 sha256 取自 nodejs.org/dist 的 SHASUMS256.txt。
ARG NODE_VERSION=24.22.2
ARG NODE_X64_SHA256=361718c444e49af2ca5a110c83ba6a8901143f23ab285ebbef0341f391f482a9
ARG NODE_ARM64_SHA256=95c264150aceb7f3437a29fd6984924f5f8d1511ea3cbd6c508b83f6c555dae2

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl python3 \
    && rm -rf /var/lib/apt/lists/*
RUN case "$TARGETARCH" in \
      amd64) node_arch=x64; node_sha="$NODE_X64_SHA256" ;; \
      arm64) node_arch=arm64; node_sha="$NODE_ARM64_SHA256" ;; \
      *) echo "不支持的架构 $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl --fail --silent --show-error --location \
         "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.gz" \
         --output /tmp/node.tar.gz \
    && echo "$node_sha  /tmp/node.tar.gz" | sha256sum --check --strict \
    && mkdir -p /usr/local/lib/nodejs \
    && tar -xzf /tmp/node.tar.gz -C /usr/local/lib/nodejs \
    && for bin in node npm npx corepack; do \
         ln -s "/usr/local/lib/nodejs/node-v${NODE_VERSION}-linux-${node_arch}/bin/$bin" "/usr/local/bin/$bin"; \
       done \
    && rm /tmp/node.tar.gz \
    && mkdir -p /data/agent/home /data/agent/tools /data/run /workspace /etc/wonton-agent \
                /usr/local/share/wonton-agent/skills /usr/local/lib/wonton-agent \
    && groupadd --system --gid 65532 wonton \
    && useradd --system --uid 65532 --gid 65532 --no-create-home wonton \
    && chown -R 65532:65532 /data/agent /workspace /data/run

COPY wonton-agent /usr/local/bin/wonton-agent
COPY wonton_agent/ /usr/local/lib/wonton-agent/wonton_agent/
COPY web/ /usr/local/share/wonton-agent/web/
RUN chmod 0755 /usr/local/bin/wonton-agent

ENV HOME=/data/agent/home \
    WONTON_ACP_SOCKET=/data/run/acp.sock \
    WONTON_AGENT_WORKSPACE=/workspace

USER 65532:65532
ENTRYPOINT ["/usr/local/bin/wonton-agent"]
