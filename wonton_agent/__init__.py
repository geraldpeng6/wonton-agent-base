"""Wonton Agent 底座包：按职责拆模块，入口脚本只做 sys.path.insert 再调 main.main。

- config：agent.toml 加载和校验、容器的路径配置（配置路径、ACP socket、工作区、HOME）
- install：[[install]] 按需下载闭源 CLI
- assets：[skills]/[prompt] 铺 skill 和系统提示
- gateway：[gateway] 按核心注入的模型变量调钩子
- relay：[acp] 转发和 authMethods 改写
- login：终端登录 socket 和登录进程（网页共用）
- web：[web] 登录网页和接口
- main：启动顺序和信号处理

log/die 放在这里：每个模块都要用，它们在最底层，不依赖包内其他模块。
"""
import sys


def log(message: str) -> None:
    print(f"[wonton-agent] {message}", file=sys.stderr, flush=True)


def die(message: str) -> None:
    log(message)
    sys.exit(1)
