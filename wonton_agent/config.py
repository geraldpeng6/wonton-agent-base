"""agent.toml 的加载和校验；容器的路径配置（配置路径、ACP socket、工作区、HOME）也在这里。
路径从环境变量读：容器里由镜像 ENV 和 compose 给出，测试指到临时目录。"""
import base64
import os
import re
import tomllib
from pathlib import Path

from . import die

CONFIG_PATH = Path(os.environ.get("WONTON_AGENT_CONFIG", "/etc/wonton-agent/agent.toml"))
NPM_REGISTRY = "https://registry.npmjs.org"
SOCKET = Path(os.environ.get("WONTON_ACP_SOCKET", "/data/run/acp.sock"))
WORKSPACE = Path(os.environ.get("WONTON_AGENT_WORKSPACE", "/workspace"))
AGENT_HOME = Path(os.environ.get("HOME", "/data/agent/home"))

HEX_SHA512 = re.compile(r"[0-9a-fA-F]{128}\Z")
HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def load_config(path: Path = CONFIG_PATH) -> dict:
    try:
        with open(path, "rb") as file:
            config = tomllib.load(file)
    except FileNotFoundError:
        die(f"找不到配置 {path}：子镜像要 COPY agent.toml /etc/wonton-agent/")
    except tomllib.TOMLDecodeError as error:
        die(f"配置 {path} 不是合法 TOML：{error}")
    unknown = set(config) - {"install", "skills", "prompt", "gateway", "acp", "login", "web"}
    if unknown:
        die(f"配置 {path} 有不认识的段 {'、'.join(sorted(unknown))}，检查字段名有没有拼错")
    if "web" in config and not config.get("login"):
        die("配置 [web] 列不出登录方式：网页列的就是 [[login]]，没有 [[login]] 就删掉 [web]")
    validate_acp(config.get("acp"))
    validate_installs(config.get("install", []))
    validate_logins(config.get("login", []))
    validate_web(config.get("web"))
    for section in ("skills", "prompt"):
        if section in config and not isinstance(config[section].get("target"), str):
            die(f"配置 [{section}] 缺 target（字符串路径）")
    if "gateway" in config:
        if not isinstance(config["gateway"].get("hook"), str):
            die("配置 [gateway] 缺 hook（可执行文件路径）")
        env_map = config["gateway"].get("env", {})
        if not isinstance(env_map, dict) or not all(
                isinstance(target, str) and isinstance(source, str)
                and ENV_NAME.fullmatch(target) and ENV_NAME.fullmatch(source)
                for target, source in env_map.items()):
            die('[gateway.env] 必须是 "目标变量 = 来源变量" 的表，'
                '例如 ANTHROPIC_AUTH_TOKEN = "WONTON_MODEL_API_KEY"')
    return config


def validate_acp(section: dict | None) -> None:
    if not isinstance(section, dict):
        die('配置缺 [acp]：至少要写 command = ["ACP 命令", ...]')
    command = section.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
        die('[acp] command 必须是非空字符串列表，例如 command = ["/usr/local/bin/foo-acp"]')
    if not isinstance(section.get("strip_ansi", True), bool):
        die("[acp] strip_ansi 必须是布尔值（缺省 true：转发时去掉终端配色码）")


def validate_installs(items: list) -> None:
    if not isinstance(items, list):
        die("配置 [[install]] 必须是列表（每个闭源 CLI 一段）")
    for item in items:
        name = item.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            die(f"[[install]] 的 name 非法：{name!r}（只允许字母数字点下划线连字符，不能有路径分隔符）")
        if not isinstance(item.get("version"), str) or not item["version"]:
            die(f"[[install]] {name} 缺 version")
        source = item.get("source")
        if source == "npm":
            if not isinstance(item.get("package"), str) or not item["package"]:
                die(f"[[install]] {name} 缺 package（npm 包名）")
            try:
                parse_integrity(item.get("integrity", ""))
            except ValueError as error:
                die(f"[[install]] {name} 的 integrity 非法：{error}")
            registry = item.get("registry", NPM_REGISTRY)
            if not isinstance(registry, str) or not registry.startswith(("http://", "https://")):
                die(f"[[install]] {name} 的 registry 必须是 http(s) 地址")
        elif source == "url":
            for field in ("url_amd64", "url_arm64", "sha256_amd64", "sha256_arm64"):
                if not isinstance(item.get(field), str) or not item[field]:
                    die(f"[[install]] {name} 缺 {field}（按架构给下载地址和 sha256）")
            if not HEX_SHA256.fullmatch(item["sha256_amd64"]) or not HEX_SHA256.fullmatch(item["sha256_arm64"]):
                die(f"[[install]] {name} 的 sha256_amd64/sha256_arm64 必须是 64 位十六进制")
            if item.get("format", "binary") not in ("binary", "tgz"):
                die(f"[[install]] {name} 的 format 只能是 binary（单个可执行文件）或 tgz（解包安装）")
        else:
            die(f"[[install]] {name} 的 source 只能是 npm 或 url")


def validate_logins(items: list) -> None:
    if not isinstance(items, list):
        die("配置 [[login]] 必须是列表（每个终端登录一段）")
    seen: set[str] = set()
    for item in items:
        login_id = item.get("id")
        if not isinstance(login_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", login_id):
            die(f"[[login]] 的 id 非法：{login_id!r}（聊天里 /wonton login 用，只允许字母数字点下划线连字符）")
        if login_id in seen:
            die(f"[[login]] 的 id 重复：{login_id}")
        seen.add(login_id)
        if not isinstance(item.get("name"), str) or not item["name"]:
            die(f"[[login]] {login_id} 缺 name（聊天里列出的名字）")
        command = item.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
            die(f'[[login]] {login_id} command 必须是非空字符串列表，例如 command = ["/usr/local/bin/foo", "login"]')
        if item.get("stdin", "lines") not in ("lines", "line_then_eof"):
            die(f"[[login]] {login_id} 的 stdin 只能是 lines（缺省，桥发的每行原样转给进程）"
                "或 line_then_eof（转第一行后关 stdin，给读 stdin 到 EOF 收 key 的 CLI）")
        if not isinstance(item.get("restart_on_success", True), bool):
            die(f"[[login]] {login_id} 的 restart_on_success 必须是布尔值（缺省 true：登录成功后重启容器）")
        status = item.get("status")
        if status is not None and (
                not isinstance(status, list) or not status
                or not all(isinstance(arg, str) and arg for arg in status)):
            die(f'[[login]] {login_id} status 必须是非空字符串列表，'
                '例如 status = ["codex", "login", "status"]（退出码 0 = 已登录）')


def validate_web(section: dict | None) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        die("[web] 必须是表：port = 8080")
    port = section.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        die("[web] 缺 port 或不是 1–65535 的整数，例如 port = 8080")


def login_methods(config: dict) -> list[dict] | None:
    """[[login]] 要改写成的 authMethods；没有这段时返回 None，转发保持原样。"""
    logins = config.get("login")
    if not logins:
        return None
    return [{"id": section["id"], "name": section["name"], "type": "terminal", "args": []}
            for section in logins]


def parse_integrity(value: str) -> tuple[str, str]:
    """npm 的 integrity 是 sha512-<base64>；插件里也常用十六进制（照 codex 插件的 Dockerfile）。"""
    if value.startswith("sha512-"):
        return "sha512", base64.b64decode(value[len("sha512-"):]).hex()
    if HEX_SHA512.fullmatch(value):
        return "sha512", value.lower()
    raise ValueError("要 sha512-<base64>（npm dist.integrity）或 128 位十六进制")
