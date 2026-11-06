# echo-agent 子镜像：证明底座之上只写 agent.toml、agent.py（和钩子、skill）就能跑，不需要 RUN。
# 底座发布后把 digest 固定上：FROM ghcr.io/geraldpeng6/wonton-agent-base:0.1.0@sha256:<发布后的摘要>
ARG BASE_IMAGE=ghcr.io/geraldpeng6/wonton-agent-base:0.1.0
FROM ${BASE_IMAGE}
COPY agent.toml /etc/wonton-agent/
COPY --chmod=0755 agent.py /usr/local/bin/echo-agent
COPY --chmod=0755 gateway /usr/local/lib/wonton-agent/gateway
# skill 目录 COPY 整棵树：COPY --chmod 会连自动创建的目录一起改（0644 会丢掉目录的 x 位）。
COPY skills/ /usr/local/share/wonton-agent/skills/
