#!/usr/bin/env python3
"""最小 ACP Agent 示例：把收到的 prompt 原样回显，证明底座的转发链路。
只说 JSON-RPC stdio，够 initialize / session/new / session/prompt 三步。"""
import json
import os
import sys

PROTOCOL_VERSION = 1


def reply(request_id, result):
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)


def prompt_text(blocks):
    return "".join(block.get("text", "") for block in blocks if isinstance(block, dict))


def gateway_note():
    # gateway.json 由网关钩子写（见 gateway），密钥不在文件里，用的时候读环境变量。
    path = os.path.expanduser("~/.echo-agent/gateway.json")
    try:
        with open(path) as file:
            gateway = json.load(file)
        return f"（模型走 {gateway['base_url']} 的 {gateway['model']}，key 读环境变量 {gateway['api_key_env']}）"
    except (OSError, ValueError):
        return "（没接模型网关）"


for line in sys.stdin.buffer:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        reply(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {},
            "agentInfo": {"name": "echo-agent", "title": "Echo Agent", "version": "0.1.0"},
        })
    elif method == "session/new":
        reply(request_id, {"sessionId": "echo-1"})
    elif method == "session/prompt":
        params = message["params"]
        update = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": params["sessionId"],
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": f"echo: {prompt_text(params['prompt'])} {gateway_note()}"},
                },
            },
        }
        print(json.dumps(update, ensure_ascii=False), flush=True)
        reply(request_id, {"stopReason": "end_turn"})
    elif method == "session/cancel":
        pass
    # 其余通知忽略
