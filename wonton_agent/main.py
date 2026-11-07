"""Wonton Agent 底座入口：按 /etc/wonton-agent/agent.toml 装闭源 CLI、装 skill 和提示、调网关钩子，
然后在 unix socket 上转发 ACP。Agent 插件的子镜像只写 agent.toml（和自己的钩子）。

每次容器启动依次：
1. [install]（可选）：闭源 CLI 不打进发布的镜像，首次启动从官方源下载到数据卷
   /data/agent/tools/<名字>/<版本>/；版本目录已存在就不重下，校验失败报错退出。
2. [skills]/[prompt]（可选）：把镜像里 /usr/local/share/wonton-agent/ 下的内容拷到 agent.toml
   指定的目录，每次启动覆盖。
3. [gateway]（可选）：核心注入的 WONTON_MODEL_* 变量都有值时调钩子 apply，任一为空调 clear；
   网关后来就绪时核心会重建容器，钩子在新一轮启动里生效。密钥只引用变量名，不写进文件。
   [gateway.env]（可选）把映射的变量加进 [acp] 子进程的环境（[[login]] 子进程不加）。
4. [acp]：在 $WONTON_ACP_SOCKET 上监听，每个连接起一次 ACP 命令（stdio 说 ACP）双向转发，
   连接断了杀掉子进程。转发时去掉终端配色码；子进程 stderr 直接进容器日志。
   socket 在前面几步都成功之后才建，compose 的健康检查以 socket 文件为准，建好之前不过。
   收到 SIGTERM/SIGINT 优雅退出：给活着的 ACP 子进程 terminate（5 秒后 kill）、删 socket、
   退出码 0，docker stop 不用等满 10 秒的 SIGKILL。
5. [[login]]（可选，可多段）：终端登录。把 initialize 应答里的 authMethods 改写成这些登录，
   并在 ACP socket 旁边监听 <ACP socket 去掉扩展名>.login.sock：桥连上来发一行
   {"methodId","args","env"}，入口按 agent.toml 里配的命令起登录进程，双向搬运
   stdin/stdout/stderr（去配色码，不完整的一行 200ms 没有新字节先发），登录成功后入口
   自己退出，容器按 restart 策略重启。
6. [web]（可选）：在 0.0.0.0:<port> 起登录网页（插件清单声明成 webui，核心网关挡在前面，
   从 App 的插件页打开）。页面列出 [[login]] 和状态（[[login]] 的 status 命令，退出码 0 =
   已登录），点登录起同一段命令：输出逐行显示、回一行进 stdin、可取消。和登录 socket 共用
   一把串行锁，一次一个；输入内容不记日志。
"""
import asyncio
import signal
import subprocess

from . import assets, config, die, gateway, install, log, relay


def shutdown(signum: int, frame: object) -> None:
    # 容器里入口是 PID 1，内核忽略默认处置：不装处理，docker stop 要等满 10 秒才 SIGKILL，
    # ACP 子进程也被硬杀。抛 SystemExit 让 serve 的 finally 收尾（子进程、socket），
    # asyncio 的清理跑完后退出码 0。
    log(f"收到 {signal.Signals(signum).name}，退出")
    raise SystemExit(0)


def main() -> None:
    for received in (signal.SIGTERM, signal.SIGINT):
        signal.signal(received, shutdown)
    settings = config.load_config()
    try:
        install.install_tools(settings)
        assets.install_skills(settings)
        assets.install_prompt(settings)
        gateway.apply_gateway(settings)
        asyncio.run(relay.serve(settings))
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        die(str(error))
