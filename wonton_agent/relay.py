"""[acp] 转发：socket ↔ ACP 子进程逐行双向搬，去掉配色码，[[login]] 时改写 initialize 的
authMethods。serve() 按启动顺序建登录 socket、ACP socket，再起登录和网页服务。"""
import asyncio
import json
import os
import shutil
import socket
from pathlib import Path

from . import config, gateway, login, log, web


def rewrite_auth_methods(line: str, methods: list[dict]) -> str:
    """有 [[login]] 时把 initialize 应答改写成配置的终端登录，其余行原样过（照搬
    wonton-plugin-codex 的 rewrite_auth_methods）。ACP 里 authMethods 可省略（缺省为空），所以
    带 protocolVersion 的 result 也算 initialize 应答，不然不报登录方式的 Agent 在桥里没有登录入口。
    改写后的行要补回换行：ACP 和桥都按行读 JSON，丢了行尾对端就一直等。"""
    try:
        message = json.loads(line)
    except ValueError:
        return line
    if isinstance(message, dict) and isinstance(message.get("result"), dict) \
            and ("authMethods" in message["result"] or "protocolVersion" in message["result"]):
        message["result"]["authMethods"] = methods
        return json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    return line


async def relay(client: socket.socket, process: asyncio.subprocess.Process,
                strip_ansi: bool, methods: list[dict] | None) -> None:
    """双向转发：socket↔子进程 stdio，逐行去配色码；有 [[login]] 时改写 authMethods。"""
    loop = asyncio.get_running_loop()

    async def socket_to_agent() -> None:
        while data := await loop.sock_recv(client, 512 * 1024):
            if process.stdin is None:
                return
            process.stdin.write(data)
            await process.stdin.drain()

    async def agent_to_socket() -> None:
        while line := await process.stdout.readline():
            if not (strip_ansi or methods):
                await loop.sock_sendall(client, line)
                continue
            text = line.decode(errors="replace")
            if strip_ansi:
                text = login.ANSI.sub("", text)
            if methods:
                text = rewrite_auth_methods(text, methods)
            await loop.sock_sendall(client, text.encode())

    tasks = [asyncio.create_task(socket_to_agent()), asyncio.create_task(agent_to_socket())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    for task in done:
        task.result()


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """先 SIGTERM 给子进程自己收尾的机会，5 秒不退再 SIGKILL；已经退出就不动。"""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def run_connection(client: socket.socket, process: asyncio.subprocess.Process,
                         strip_ansi: bool, methods: list[dict] | None, children: set) -> None:
    try:
        await relay(client, process, strip_ansi, methods)
    except (OSError, ValueError):
        pass  # 对端断开时的竞态（BrokenPipe 等），子进程在下面统一收掉
    finally:
        children.discard(process)
        try:
            client.close()
        finally:
            await stop_process(process)


def listen_unix(path: Path, backlog: int) -> socket.socket:
    """建 unix 监听 socket，权限 0600：只有桥和宿主诊断连得上。"""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    os.chmod(path, 0o600)
    listener.listen(backlog)
    listener.setblocking(False)
    return listener


async def serve(cfg: dict) -> None:
    command = cfg["acp"]["command"]
    strip_ansi = cfg["acp"].get("strip_ansi", True)
    methods = config.login_methods(cfg)
    # [gateway.env] 在监听之前解析：来源变量没值直接报错退出，健康检查不过，别等连接。
    child_env = {**os.environ, **gateway.acp_env(cfg)}
    # 起不来是子镜像配置坏了：在监听之前就报错，健康检查不过，别等第一个连接才暴露。
    if not shutil.which(command[0]):
        raise RuntimeError(
            f"ACP 命令 {command[0]} 不存在或不可执行：检查 [acp] command，或 [install] 是否装到位")
    for section in cfg.get("login", []):
        if not shutil.which(section["command"][0]):
            raise RuntimeError(
                f"[[login]] {section['id']} 的命令 {section['command'][0]} 不存在或不可执行："
                "检查 [[login]] command，或 [install] 是否装到位")
        if section.get("status") and not shutil.which(section["status"][0]):
            raise RuntimeError(
                f"[[login]] {section['id']} 的状态命令 {section['status'][0]} 不存在或不可执行："
                "检查 [[login]] status，或 [install] 是否装到位")
    for directory in (config.AGENT_HOME, config.WORKSPACE, config.SOCKET.parent):
        directory.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    children: set[asyncio.subprocess.Process] = set()
    # 登录 socket 在 ACP socket 之前建好（健康检查看的是 ACP socket）；没有 [[login]] 就不建。
    # 串行锁在登录模块里、登录 socket 和网页共用，每次运行新建（asyncio 原语绑事件循环）。
    login.LOGIN_LOCK = asyncio.Lock()
    login_socket = config.SOCKET.with_suffix(".login.sock") if methods else None
    listener = login_listener = login_task = web_server = None
    try:
        if login_socket:
            login_listener = listen_unix(login_socket, 2)
        listener = listen_unix(config.SOCKET, 8)
        log(f"ACP 转发就绪：{config.SOCKET} -> {command[0]}")
        if login_listener:
            login_task = asyncio.create_task(login.serve_login(login_listener, cfg["login"]))
            log(f"终端登录就绪：{login_socket}（{'、'.join(s['id'] for s in cfg['login'])}）")
        if cfg.get("web"):
            web_server = web.start_web_server(cfg["login"], cfg["web"]["port"])
        while True:
            client, _ = await loop.sock_accept(listener)
            client.setblocking(False)
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    # stderr=None：子进程 stderr 继承容器的，直接进容器日志。
                    stderr=None,
                    cwd=config.WORKSPACE,
                    env=child_env,
                    # ACP 一行就是一条消息，带上下文块时远超默认的 64KiB。
                    limit=2 ** 24,
                )
            except OSError as error:
                client.close()
                # 命令起不来是子镜像配置坏了，重试连接不会好：退出让核心看到失败。
                raise RuntimeError(f"启动 ACP 命令 {command} 失败：{error}") from error
            children.add(process)
            asyncio.create_task(run_connection(client, process, strip_ansi, methods, children))
    finally:
        if login_task:
            login_task.cancel()
            await asyncio.gather(login_task, return_exceptions=True)
        if web_server:
            web_server.shutdown()
            web_server.server_close()
        for sock in (listener, login_listener):
            if sock:
                sock.close()
        for path in (config.SOCKET, login_socket):
            if path:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        # 正常不走到这（收到 SIGTERM 被取消时才走）：把还活着的子进程收干净再退。
        await asyncio.gather(*(stop_process(process) for process in children))
