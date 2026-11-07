"""[web] 登录网页和接口：静态页 + /api/logins。起的是 [[login]] 的同一段命令，和登录
socket 共用 login 的串行锁和会话表；列表状态用 status 命令。"""
import asyncio
import json
import queue
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import config, log, login

# 状态命令最多等这么久（每个并发查，超时算查询出错）；静态文件在镜像里的位置。
STATUS_TIMEOUT = 10
WEB_ROOT = Path("/usr/local/share/wonton-agent/web")
# 网页要用的 [[login]] 段、事件循环和 HTTP 服务器，start_web_server 里赋值；测试读
# WEB_SERVER 拿实际端口。
WEB_LOGINS: list[dict] = []
WEB_LOOP: asyncio.AbstractEventLoop | None = None
WEB_SERVER: ThreadingHTTPServer | None = None
LOGIN_API_RE = re.compile(r"^/api/logins/([A-Za-z0-9][A-Za-z0-9._-]*)/(start|events|input|cancel)$")


async def start_web_login(section: dict) -> str | None:
    """网页点登录：锁被占（任何入口的登录在跑）就拒绝，否则占住锁、起好进程再返回。
    返回 None 表示已启动，"busy" 表示有一个登录正在进行。起进程放在返回前，页面随后
    的 input/events 一定找得到会话。"""
    if login.LOGIN_LOCK.locked():
        return "busy"
    await login.LOGIN_LOCK.acquire()
    session = login.LoginSession(section)
    login.ACTIVE_LOGINS[section["id"]] = session
    await session.start()
    asyncio.create_task(run_web_login(session))
    return None


async def run_web_login(session: login.LoginSession) -> None:
    """网页登录的收尾：退出帧进队就留档、放锁，下一位才能登录；成功重启在 _run 里做。"""
    try:
        await session.exited.wait()
    finally:
        login.retire_session(session)
        if login.LOGIN_LOCK.locked():
            login.LOGIN_LOCK.release()


async def login_status(section: dict) -> dict:
    """一段 [[login]] 的状态：status 命令退出码 0 = 已登录，非 0 = 没登录，没写 = 不确定。
    起不来、超时算查询出错，原因放进 detail。正在登录的跳过查询，如实显示在登录。"""
    item = {"id": section["id"], "name": section["name"],
            "stdin": section.get("stdin", "lines"),
            "restart_on_success": section.get("restart_on_success", True),
            "busy": section["id"] in login.ACTIVE_LOGINS,
            "status": "unknown", "detail": None}
    command = section.get("status")
    if item["busy"] or not command:
        return item
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            cwd=config.WORKSPACE)
    except OSError as error:
        item["status"] = "error"
        item["detail"] = f"起状态命令失败：{error}"
        return item
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), STATUS_TIMEOUT)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        item["status"] = "error"
        item["detail"] = f"状态命令超过 {STATUS_TIMEOUT} 秒没退出"
        return item
    item["status"] = "logged_in" if process.returncode == 0 else "logged_out"
    return item


async def query_logins(sections: list[dict]) -> list[dict]:
    return list(await asyncio.gather(*(login_status(section) for section in sections)))


async def pump_frames(session: login.LoginSession, sink: queue.Queue) -> None:
    """网页 SSE 这边的帧消费方：逐帧搬给 HTTP 线程。会话结束、帧也取干净了就放哨兵收尾
    （挂到已结束的会话上：把剩下的帧发完就关）。"""
    while True:
        try:
            frame = await asyncio.wait_for(session.frames.get(), 0.5)
        except asyncio.TimeoutError:
            if session.exited.is_set() and session.frames.empty():
                sink.put(None)
                return
            continue
        sink.put(frame)
        if "exit" in frame:
            sink.put(None)
            return


class LoginHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # 跳过 HTTPServer.server_bind 里的 socket.getfqdn（填 server_name 用的反向 DNS）：
        # 网页用不上 server_name，宿主机 DNS 慢时它能把起网页卡住几秒。
        if self.allow_reuse_address:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()


def start_web_server(logins: list[dict], port: int) -> ThreadingHTTPServer:
    """在 0.0.0.0:<port> 起登录网页（插件清单声明成 webui，核心网关挡在前面）。HTTP 跑在
    daemon 线程里，asyncio 的活经 run_coroutine_threadsafe 回当前循环。"""
    global WEB_LOOP, WEB_LOGINS, WEB_SERVER
    WEB_LOOP = asyncio.get_running_loop()
    WEB_LOGINS = logins
    server = LoginHTTPServer(("0.0.0.0", port), LoginWebHandler)
    server.daemon_threads = True
    WEB_SERVER = server
    threading.Thread(target=server.serve_forever, name="wonton-agent-web", daemon=True).start()
    log(f"登录网页就绪：0.0.0.0:{server.server_address[1]}")
    return server


class LoginWebHandler(BaseHTTPRequestHandler):
    """登录网页：静态页（WEB_ROOT 下的 index.html 和 tokens.css）+ /api/logins 接口。
    POST 只收 JSON（挡跨站表单）；输入内容不进日志。"""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in ("", "/"):
            self.send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
        elif path == "/tokens.css":
            self.send_file(WEB_ROOT / "tokens.css", "text/css; charset=utf-8")
        elif path == "/api/logins":
            self.send_json(200, self.list_logins())
        elif match := LOGIN_API_RE.match(path):
            if match.group(2) == "events":
                self.events(match.group(1))
            else:
                self.send_json(405, {"error": "method_not_allowed"})
        else:
            self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        match = LOGIN_API_RE.match(urlsplit(self.path).path)
        if not match or match.group(2) == "events":
            self.send_json(404, {"error": "not_found"})
            return
        login_id, action = match.groups()
        section = next((s for s in WEB_LOGINS if s["id"] == login_id), None)
        if section is None:
            self.send_json(404, {"error": "not_found"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self.send_json(415, {"error": "bad_content_type"})
            return
        body = self.read_json_body()
        if body is None:
            return
        if action == "start":
            self.start(section)
        elif action == "input":
            self.input(login_id, body)
        else:
            self.cancel(login_id)

    def list_logins(self) -> dict:
        future = asyncio.run_coroutine_threadsafe(query_logins(WEB_LOGINS), WEB_LOOP)
        logins = future.result(STATUS_TIMEOUT + 10)
        return {"logins": logins, "busy": any(item["busy"] for item in logins)}

    def start(self, section: dict) -> None:
        future = asyncio.run_coroutine_threadsafe(start_web_login(section), WEB_LOOP)
        if future.result(10) == "busy":
            self.send_json(409, {"error": "busy"})
        else:
            self.send_json(200, {"ok": True})

    def input(self, login_id: str, body: dict) -> None:
        text = body.get("text")
        if not isinstance(text, str):
            self.send_json(400, {"error": "bad_request"})
            return
        session = login.ACTIVE_LOGINS.get(login_id)
        if session is None:
            self.send_json(409, {"error": "not_running"})
            return
        if not session.stdin_open():
            self.send_json(409, {"error": "stdin_closed"})
            return
        try:
            asyncio.run_coroutine_threadsafe(session.write_input(text), WEB_LOOP).result(10)
        except Exception:
            # 进程刚好退了，输入没送进去：按输入已关处理，不把内容写进任何日志。
            self.send_json(409, {"error": "stdin_closed"})
            return
        self.send_json(200, {"ok": True})

    def cancel(self, login_id: str) -> None:
        session = login.ACTIVE_LOGINS.get(login_id)
        if session is None:
            self.send_json(409, {"error": "not_running"})
            return
        # stdin 关掉等 2 秒、terminate 等 2 秒、kill：宽限上限之内回话。
        asyncio.run_coroutine_threadsafe(session.close_input(), WEB_LOOP).result(
            login.DISCONNECT_GRACE * 2 + 10)
        self.send_json(200, {"ok": True})

    def events(self, login_id: str) -> None:
        session = login.ACTIVE_LOGINS.get(login_id) or login.FINISHED_LOGINS.get(login_id)
        if session is None:
            self.send_json(409, {"error": "not_running"})
            return
        sink: queue.Queue = queue.Queue()
        asyncio.run_coroutine_threadsafe(pump_frames(session, sink), WEB_LOOP)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                try:
                    frame = sink.get(timeout=15)
                except queue.Empty:
                    # 没输出时也发个注释行，别让中间的代理把空闲连接掐了。
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if frame is None:
                    break
                self.wfile.write(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                if "exit" in frame:
                    break
        except OSError:
            pass  # 页面关了：SSE 断开，登录进程照常跑完（成功照样重启）。

    def read_json_body(self) -> dict | None:
        raw_length = self.headers.get("Content-Length")
        if not raw_length or not raw_length.isdigit() or int(raw_length) > 64 * 1024:
            self.send_json(400, {"error": "bad_request"})
            return None
        try:
            body = json.loads(self.rfile.read(int(raw_length)))
        except ValueError:
            self.send_json(400, {"error": "bad_json"})
            return None
        if not isinstance(body, dict):
            self.send_json(400, {"error": "bad_json"})
            return None
        return body

    def send_json(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self.send_json(404, {"error": "not_found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        # 访问日志只有方法和路径；请求体（可能藏着回码）不打。
        log(f"网页 {format % args}")
