"""[gateway]：核心注入的模型网关变量都有值时调钩子 apply，任一为空调 clear。
密钥只引用变量名，不写进文件。"""
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
