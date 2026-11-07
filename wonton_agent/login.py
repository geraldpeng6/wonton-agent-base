"""终端登录：登录 socket（桥）和网页共用的串行锁、会话表、登录进程和帧协议。

一次登录一个，登录 socket 和网页共用一把串行锁；锁和会话表在 serve() 每次运行时新建
（asyncio 原语绑事件循环，relay.serve 里赋值）。
"""
import asyncio
import json
import os
import re
import socket

from . import config

# 桥把终端登录的链接和提示原样发进聊天：去掉终端配色码（照搬 wonton-plugin-codex）。
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# 输出里有不完整的一行、这么久没有新字节就先把这一段发出（{"out": …}），后面的接着拼：
# Claude Code 登录打印 "Paste code here if prompted > " 后不换行等输入，按行读会把提示
# 卡到下一次输出才发。登录 socket 和网页共用这一处读取。
IDLE_FLUSH = 0.2

# 登录成功回完 exit 帧等这么久再退出入口，让桥收尾；桥断开时 stdin 关掉等这么久，
# terminate 再等这么久，最后 kill。测试里调短，不必等满 2 秒。
RESTART_DELAY = 2
DISCONNECT_GRACE = 2

# 谁在跑记在 ACTIVE_LOGINS（只在该循环线程里改），网页的列表、SSE 和取消都从这里找会话；
# 跑完的按 id 留在 FINISHED_LOGINS，页面在 start 之后才开 SSE 也收得到输出和退出码。
LOGIN_LOCK: asyncio.Lock | None = None
ACTIVE_LOGINS: dict[str, "LoginSession"] = {}
FINISHED_LOGINS: dict[str, "LoginSession"] = {}


def retire_session(session: "LoginSession") -> None:
    login_id = session.section["id"]
    ACTIVE_LOGINS.pop(login_id, None)
    FINISHED_LOGINS[login_id] = session


async def send_frame(client: socket.socket, loop: asyncio.AbstractEventLoop, frame: dict) -> None:
    await loop.sock_sendall(client, (json.dumps(frame, ensure_ascii=False) + "\n").encode())


class LoginSession:
    """一次登录进程：起进程、stdout/stderr 去配色码放进 frames（不完整行按 IDLE_FLUSH
    先发）、写 stdin、收尾。
    frames 依次是 {"out": 行}/{"err": 行}，收完进 {"exit": 数字}；每条入口一个消费方，
    桥这边是 forward_frames，网页那边是 pump_frames。"""

    def __init__(self, section: dict, env: dict | None = None):
        self.section = section
        self.env = env or {}
        self.stdin_mode = section.get("stdin", "lines")
        self.process: asyncio.subprocess.Process | None = None
        self.frames: asyncio.Queue[dict] = asyncio.Queue()
        self.exited = asyncio.Event()
        self.exit_code: int | None = None

    def stdin_open(self) -> bool:
        return (self.process is not None and self.process.stdin is not None
                and not self.process.stdin.is_closing())

    async def start(self) -> None:
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.section["command"],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **self.env},
                cwd=config.WORKSPACE,
            )
        except OSError as error:
            self.frames.put_nowait({"err": f"启动登录命令 {self.section['command']} 失败：{error}"})
            self.exit_code = 1
            self.frames.put_nowait({"exit": 1})
            self.exited.set()
            return
        asyncio.create_task(self._run())

    async def write_input(self, text: str) -> None:
        """写一行进 stdin。line_then_eof：写完关掉 stdin（读 stdin 到 EOF 才收 key 的 CLI）。"""
        stdin = self.process.stdin
        stdin.write((text + "\n").encode())
        await stdin.drain()
        if self.stdin_mode == "line_then_eof":
            stdin.close()

    async def close_input(self) -> None:
        """输入端没了（桥断开、网页取消）：stdin 关掉等 2 秒，terminate 等 2 秒，还不退就
        kill。退出码由 _run 收进 frames。"""
        process = self.process
        if process is None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), DISCONNECT_GRACE)
            return
        except asyncio.TimeoutError:
            pass
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), DISCONNECT_GRACE)
            return
        except asyncio.TimeoutError:
            pass
        process.kill()
        await process.wait()

    async def _run(self) -> None:
        """收输出、等退出；退出码交给 frames 和 exited，成功且 restart_on_success 就退出
        整个入口：容器按 restart 策略重启，钩子按新登录态重新选路。"""
        stdout_task = asyncio.create_task(self._pump(self.process.stdout, "out"))
        stderr_task = asyncio.create_task(self._pump(self.process.stderr, "err"))
        code = await self.process.wait()
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        self.exit_code = code
        self.frames.put_nowait({"exit": code})
        self.exited.set()
        if code == 0 and self.section.get("restart_on_success", True):
            # 等消费方把退出帧收完（桥收到帧、网页画出来），然后退出。
            await asyncio.sleep(RESTART_DELAY)
            os._exit(0)

    async def _pump(self, stream: asyncio.StreamReader, key: str) -> None:
        """stdout/stderr 搬运：完整行照常发；不完整的一行等 IDLE_FLUSH 没有新字节就先发，
        之后的输出接着这一行拼（帧里不加换行）。EOF 时剩多少发多少。"""
        pending = b""
        while True:
            try:
                chunk = await asyncio.wait_for(stream.read(64 * 1024), IDLE_FLUSH)
            except asyncio.TimeoutError:
                if pending:
                    self._emit(key, pending)
                    pending = b""
                continue
            if not chunk:
                break
            pending += chunk
            while (newline := pending.find(b"\n")) >= 0:
                self._emit(key, pending[:newline])
                pending = pending[newline + 1:]
        if pending:
            self._emit(key, pending)

    def _emit(self, key: str, line: bytes) -> None:
        self.frames.put_nowait({key: ANSI.sub("", line.decode(errors="replace")).rstrip("\n")})


async def forward_frames(session: LoginSession, client: socket.socket,
                         loop: asyncio.AbstractEventLoop) -> None:
    """桥这边的帧消费方：逐帧发给桥，发完退出帧就完。桥断开时停发，会话照常跑完。"""
    while True:
        frame = await session.frames.get()
        try:
            await send_frame(client, loop, frame)
        except OSError:
            return
        if "exit" in frame:
            return


async def serve_login(listener: socket.socket, logins: list[dict]) -> None:
    """终端类登录：桥连这个 socket，入口按 [[login]] 配置起登录进程并搬运 stdio。

    协议（与桥 login/terminal.rs 约定）：桥先发一行 JSON {"methodId","args","env"}；methodId
    对上哪段 [[login]] 就用哪段的 command 和 stdin 模式（请求里的 args 忽略），env 合并进环境。
    之后桥发的每行文本写入登录进程 stdin；登录进程 stdout/stderr 去配色码回
    {"out"}/{"err"}（不完整的一行等 200 毫秒没有新字节就先发，提示不带换行的 CLI
    不用等下一次输出），输出收完回 {"exit": 数字}，0 才算成功。登录串行，一次一个，后来的在
    backlog 里等。
    """
    loop = asyncio.get_running_loop()
    while True:
        client, _ = await loop.sock_accept(listener)
        try:
            await relay_login(client, loop, logins)
        except OSError:
            pass  # 桥断开或登录进程死了的竞态（BrokenPipe 等）：这次登录作废，串行继续。
        finally:
            client.close()


async def relay_login(client: socket.socket, loop: asyncio.AbstractEventLoop,
                      logins: list[dict]) -> None:
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), client.makefile("rb"))
    request_line = await reader.readline()
    if not request_line:
        return
    try:
        request = json.loads(request_line)
    except ValueError:
        request = None
    if not isinstance(request, dict):
        await send_frame(client, loop, {"err": "登录请求第一行要是 JSON 对象"})
        await send_frame(client, loop, {"exit": 2})
        return
    section = next((s for s in logins if s["id"] == request.get("methodId")), None)
    if section is None:
        await send_frame(client, loop, {"err": f"没有这种登录方式：{request.get('methodId')}"})
        await send_frame(client, loop, {"exit": 2})
        return
    # 占住串行锁直到退出帧发出：这期间网页 start 会被拒，后来的桥连接在 backlog 里等。
    async with LOGIN_LOCK:
        session = LoginSession(section, request.get("env", {}))
        ACTIVE_LOGINS[section["id"]] = session
        try:
            forward = asyncio.create_task(forward_frames(session, client, loop))
            await session.start()
            if session.stdin_mode == "line_then_eof":
                line = await reader.readline()
                if line:
                    await session.write_input(line.decode(errors="replace").rstrip("\n"))
                else:
                    # 桥没发内容就断开（取消、超时）：结束登录进程，收回这次串行的登录。
                    await session.close_input()
            else:
                input_task = asyncio.create_task(pump_bridge_input(reader, session))
                exited_task = asyncio.create_task(session.exited.wait())
                done, _ = await asyncio.wait({input_task, exited_task},
                                             return_when=asyncio.FIRST_COMPLETED)
                if input_task in done:
                    # 桥断开（取消、超时、桥退出）：登录进程可能还在等 stdin，必须收掉，
                    # 否则串行的 serve_login 会一直卡在这次登录上。
                    await session.close_input()
                else:
                    input_task.cancel()
                await asyncio.gather(input_task, exited_task, return_exceptions=True)
            await forward
        finally:
            retire_session(session)


async def pump_bridge_input(reader: asyncio.StreamReader, session: LoginSession) -> None:
    while line := await reader.readline():
        await session.write_input(line.decode(errors="replace").rstrip("\n"))
