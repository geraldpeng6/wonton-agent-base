"""[gateway]：核心注入的模型网关变量都有值时调钩子 apply，任一为空调 clear。
密钥只引用变量名，不写进文件。[gateway.env] 再把网关变量映射成 Agent 要的变量名
（目标变量 = 来源变量），加进 [acp] 子进程环境（[[login]] 子进程不加）。"""
import os
import subprocess

from . import die, log

GATEWAY_ENV = ("WONTON_MODEL_BASE_URL", "WONTON_MODEL_API_KEY", "WONTON_MODEL_DEFAULT")


def apply_gateway(config: dict) -> None:
    section = config.get("gateway")
    if not section:
        return
    missing = [key for key in GATEWAY_ENV if not os.environ.get(key, "").strip()]
    action = "clear" if missing else "apply"
    hook = section["hook"]
    if not os.access(hook, os.X_OK):
        die(f"网关钩子 {hook} 不存在或不可执行：子镜像要 COPY 自己的钩子到这里，或在 [gateway] 里改 hook")
    log(f"模型网关：调 {hook} {action}" + (f"（缺 {'、'.join(missing)}）" if missing else ""))
    result = subprocess.run([hook, action], stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"网关钩子 {hook} {action} 失败（退出码 {result.returncode}），看上面日志")


def acp_env(config: dict) -> dict[str, str]:
    """[gateway.env]（目标变量 = 来源变量）解析成加进 [acp] 子进程的环境。网关接好
    （三个 WONTON_MODEL_* 都有值，和钩子 apply 同一个条件）才解析；没接网关或变量不全
    （钩子 clear）时不加。来源变量没值说明配置和实际环境对不上，报错别静默跳过。
    [[login]] 子进程不加：网关只进对话进程，登录进程的 env 来自请求和 os.environ。"""
    section = config.get("gateway")
    if not section:
        return {}
    missing = [key for key in GATEWAY_ENV if not os.environ.get(key, "").strip()]
    if missing:
        return {}
    for source in section.get("env", {}).values():
        if not os.environ.get(source, "").strip():
            die(f"[gateway.env] 的来源变量 {source} 没有值：检查 compose 是否注入了这个变量")
    return {target: os.environ[source] for target, source in section.get("env", {}).items()}
