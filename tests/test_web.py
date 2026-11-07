"""登录网页：列表状态（已登录 / 没登录 / 不确定 / 查询出错）、start→events 收到输出和结束、
input 送达（lines 和 line_then_eof）、cancel 收掉进程、串行（第二个 start 被拒，网页能看到
socket 那边的登录）、POST 只收 JSON。HTTP 走 http.client（能控制 Content-Type、逐行读 SSE），
asyncio 的活用 relay.serve 真起。"""
import asyncio
import http.client
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support

from wonton_agent import config as paths, login, relay, web
LOGIN_CHILD = Path(__file__).parent / "login_child.py"


def web_config(logins: list[dict]) -> dict:
    return {
        "acp": {"command": [sys.executable, "-c", "import time; time.sleep(3600)"]},
        "web": {"port": 0},
        "login": logins,
    }


class WebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ab-web-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patches = [
            mock.patch.object(paths, "SOCKET", self.tmp / "run" / "acp.sock"),
            mock.patch.object(paths, "WORKSPACE", self.tmp / "workspace"),
            mock.patch.object(paths, "AGENT_HOME", self.tmp / "home"),
            # 成功后的重启等待、断连宽限和状态查询超时调短，测试不必等满。
            mock.patch.object(login, "RESTART_DELAY", 0.1),
            mock.patch.object(login, "DISCONNECT_GRACE", 0.3),
            mock.patch.object(web, "STATUS_TIMEOUT", 0.5),
            mock.patch.object(web, "WEB_ROOT", self.tmp / "no-web-root"),
            mock.patch.object(web, "WEB_SERVER", None),
        ]
        for patch in patches:
            patch.start()
        self.addCleanup(mock.patch.stopall)
        # relay_login 登录成功会 os._exit(0)：打成桩记调用，测试进程活下来。
        self.exit_mock = mock.patch.object(os, "_exit").start()

    async def asyncTearDown(self):
        # 中途失败的测试可能留下还在跑的会话：清掉状态、放锁，别毒化后面的测试。
        login.ACTIVE_LOGINS.clear()
        login.FINISHED_LOGINS.clear()
        if login.LOGIN_LOCK is not None and login.LOGIN_LOCK.locked():
            login.LOGIN_LOCK.release()

    def port(self) -> int:
        return web.WEB_SERVER.server_address[1]

    async def start_serve(self, config: dict) -> asyncio.Task:
        serve_task = asyncio.create_task(relay.serve(config))
        for _ in range(200):
            if paths.SOCKET.exists() and web.WEB_SERVER is not None:
                return serve_task
            if serve_task.done():
                raise RuntimeError(f"serve 提前退出：{serve_task.exception()}")
            await asyncio.sleep(0.05)
        self.fail("ACP socket 或网页一直没就绪")

    async def stop_serve(self, serve_task: asyncio.Task) -> None:
        serve_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await serve_task

    # ---------- HTTP 小工具（阻塞调用都丢进线程，别挡住主循环上的网页处理） ----------

    def sync_request(self, method: str, path: str, body: bytes | None = None,
                     headers: dict | None = None) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port(), timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    async def request(self, method: str, path: str, body: bytes | None = None,
                      headers: dict | None = None) -> tuple[int, dict]:
        return await asyncio.to_thread(
            lambda: (lambda status, raw: (status, json.loads(raw or b"null")))(
                *self.sync_request(method, path, body, headers)))

    async def post_json(self, path: str, payload) -> tuple[int, dict]:
        return await self.request("POST", path, json.dumps(payload).encode(),
                                  {"Content-Type": "application/json"})

    def sync_read_events(self, path: str, count: int) -> list[dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port(), timeout=10)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            assert response.status == 200, response.status
            assert response.getheader("Content-Type", "").startswith("text/event-stream")
            frames = []
            while len(frames) < count:
                line = response.readline()
                if not line:
                    break
                if line.startswith(b"data: "):
                    frames.append(json.loads(line[len(b"data: "):]))
            return frames
        finally:
            conn.close()

    async def read_events(self, path: str, count: int, timeout: float = 10) -> list[dict]:
        return await asyncio.wait_for(asyncio.to_thread(self.sync_read_events, path, count), timeout)

    async def wait_until(self, predicate, timeout: float = 5, interval: float = 0.05) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not await predicate():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("条件一直没满足")
            await asyncio.sleep(interval)

    # ---------- 测试 ----------

    async def test_list_reports_login_states(self):
        config = web_config([
            {"id": "yes", "name": "已登录", "command": [sys.executable, str(LOGIN_CHILD), "echo"],
             "status": [sys.executable, str(LOGIN_CHILD), "exit", "0"]},
            {"id": "no", "name": "没登录", "command": [sys.executable, str(LOGIN_CHILD), "echo"],
             "status": [sys.executable, str(LOGIN_CHILD), "exit", "1"]},
            {"id": "maybe", "name": "没写 status", "command": [sys.executable, str(LOGIN_CHILD), "echo"]},
            {"id": "stuck", "name": "状态挂住", "command": [sys.executable, str(LOGIN_CHILD), "echo"],
             "status": [sys.executable, str(LOGIN_CHILD), "hold", str(self.tmp / "stuck.pid")]},
        ])
        serve_task = await self.start_serve(config)
        try:
            status, body = await self.request("GET", "/api/logins")
            self.assertEqual(status, 200)
            self.assertFalse(body["busy"])
            by_id = {item["id"]: item for item in body["logins"]}
            self.assertEqual(by_id["yes"]["status"], "logged_in")
            self.assertEqual(by_id["no"]["status"], "logged_out")
            self.assertEqual(by_id["maybe"]["status"], "unknown")
            self.assertIsNone(by_id["maybe"]["detail"])
            self.assertEqual(by_id["stuck"]["status"], "error")
            self.assertIn("没退出", by_id["stuck"]["detail"])
            # 网页要用的元信息都在：stdin 模式和 restart_on_success 缺省值。
            self.assertEqual(by_id["maybe"]["stdin"], "lines")
            self.assertIs(by_id["maybe"]["restart_on_success"], True)
        finally:
            await self.stop_serve(serve_task)

    async def test_start_events_deliver_output_and_exit(self):
        config = web_config([
            {"id": "probe", "name": "探针",
             "command": [sys.executable, str(LOGIN_CHILD), "env", "AB_WEB_PROBE"]},
        ])
        serve_task = await self.start_serve(config)
        try:
            # 会话还没开始：events 和 input/cancel 都如实拒绝。
            self.assertEqual((await self.request("GET", "/api/logins/probe/events"))[0], 409)
            self.assertEqual((await self.post_json("/api/logins/probe/input", {"text": "x"}))[0], 409)
            self.assertEqual((await self.post_json("/api/logins/probe/cancel", {}))[0], 409)
            status, body = await self.post_json("/api/logins/probe/start", {})
            self.assertEqual(status, 200)
            self.assertEqual(body, {"ok": True})
            # 输出帧和退出帧都推到 SSE；列表同时显示在登录。
            self.assertEqual(await self.read_events("/api/logins/probe/events", 2),
                             [{"out": "unset"}, {"exit": 0}])
            _, listing = await self.request("GET", "/api/logins")
            self.assertFalse(listing["busy"], "退出帧之后锁应该放掉")
        finally:
            await self.stop_serve(serve_task)

    async def test_input_reaches_login_process(self):
        config = web_config([
            {"id": "sample", "name": "样例",
             "command": [sys.executable, str(LOGIN_CHILD), "echo", "1"]},
        ])
        serve_task = await self.start_serve(config)
        try:
            await self.post_json("/api/logins/sample/start", {})
            status, body = await self.post_json("/api/logins/sample/input", {"text": "the-key"})
            self.assertEqual(status, 200)
            self.assertEqual(await self.read_events("/api/logins/sample/events", 2),
                             [{"out": "got:the-key"}, {"exit": 0}])
        finally:
            await self.stop_serve(serve_task)

    async def test_line_then_eof_input_closes_stdin(self):
        config = web_config([
            {"id": "sample", "name": "样例",
             "command": [sys.executable, str(LOGIN_CHILD), "eof"],
             "stdin": "line_then_eof"},
        ])
        serve_task = await self.start_serve(config)
        try:
            await self.post_json("/api/logins/sample/start", {})
            status, _ = await self.post_json("/api/logins/sample/input", {"text": "the-key"})
            self.assertEqual(status, 200)
            # eof:<行数>:<首行> 证明只转了第一行就关了 stdin（fix2 的语义）。
            self.assertEqual(await self.read_events("/api/logins/sample/events", 2),
                             [{"out": "eof:1:the-key"}, {"exit": 0}])
            # 再发一帧输入：stdin 已关，如实拒绝。
            self.assertEqual((await self.post_json("/api/logins/sample/input", {"text": "x"}))[0], 409)
        finally:
            await self.stop_serve(serve_task)

    async def test_cancel_kills_login_process(self):
        pidfile = self.tmp / "login.pid"
        config = web_config([
            {"id": "sample", "name": "样例",
             "command": [sys.executable, str(LOGIN_CHILD), "hold", str(pidfile)]},
        ])
        serve_task = await self.start_serve(config)
        try:
            await self.post_json("/api/logins/sample/start", {})
            for _ in range(200):
                if pidfile.exists():
                    break
                await asyncio.sleep(0.05)
            pid = int(pidfile.read_text())
            os.kill(pid, 0)  # 进程活着
            status, body = await self.post_json("/api/logins/sample/cancel", {})
            self.assertEqual(status, 200)
            self.assertEqual(await self.read_events("/api/logins/sample/events", 1),
                             [{"exit": -15}])  # terminate 的 SIGTERM
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            await self.stop_serve(serve_task)

    async def test_second_start_rejected_while_running(self):
        pidfile = self.tmp / "login.pid"
        config = web_config([
            {"id": "slow", "name": "慢的", "command": [sys.executable, str(LOGIN_CHILD), "hold", str(pidfile)]},
            {"id": "other", "name": "另一段", "command": [sys.executable, str(LOGIN_CHILD), "echo"]},
        ])
        serve_task = await self.start_serve(config)
        try:
            await self.post_json("/api/logins/slow/start", {})
            _, listing = await self.request("GET", "/api/logins")
            self.assertTrue(listing["busy"])
            self.assertTrue(next(item["busy"] for item in listing["logins"] if item["id"] == "slow"))
            # 同一段和别的段都起不了：一次一个。
            for login_id in ("slow", "other"):
                status, body = await self.post_json(f"/api/logins/{login_id}/start", {})
                self.assertEqual(status, 409)
                self.assertEqual(body, {"error": "busy"})
            # 收掉进程后锁放掉，能再起。
            await self.post_json("/api/logins/slow/cancel", {})

            async def not_busy() -> bool:
                _, listing = await self.request("GET", "/api/logins")
                return listing["busy"] is False

            await self.wait_until(not_busy)
            status, _ = await self.post_json("/api/logins/other/start", {})
            self.assertEqual(status, 200)
            # 测试收尾：把刚起的登录收掉，别留挂住的会话。
            await self.post_json("/api/logins/other/cancel", {})
        finally:
            await self.stop_serve(serve_task)

    async def test_web_sees_login_started_from_socket(self):
        pidfile = self.tmp / "login.pid"
        config = web_config([
            {"id": "sample", "name": "样例",
             "command": [sys.executable, str(LOGIN_CHILD), "hold", str(pidfile)]},
        ])
        serve_task = await self.start_serve(config)
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(paths.SOCKET.with_suffix(".login.sock")))
            writer.write((json.dumps({"methodId": "sample", "args": [], "env": {}}) + "\n").encode())
            await writer.drain()
            for _ in range(200):
                if pidfile.exists():
                    break
                await asyncio.sleep(0.05)
            # socket 那边的登录进行中：网页列表如实显示，start 被拒。
            _, listing = await self.request("GET", "/api/logins")
            self.assertTrue(listing["busy"])
            status, body = await self.post_json("/api/logins/sample/start", {})
            self.assertEqual(status, 409)
            # 桥断开收掉登录进程，锁放掉。
            writer.close()

            async def not_busy() -> bool:
                _, listing = await self.request("GET", "/api/logins")
                return listing["busy"] is False

            await self.wait_until(not_busy)
        finally:
            await self.stop_serve(serve_task)

    async def test_non_json_and_unknown_requests_rejected(self):
        config = web_config([
            {"id": "sample", "name": "样例",
             "command": [sys.executable, str(LOGIN_CHILD), "echo", "1"]},
        ])
        serve_task = await self.start_serve(config)
        try:
            # 表单式跨站 POST 挡掉：缺 Content-Type 和别的类型都 415。
            status, body = await self.request("POST", "/api/logins/sample/start", b"{}")
            self.assertEqual(status, 415)
            self.assertEqual(body, {"error": "bad_content_type"})
            status, body = await self.request("POST", "/api/logins/sample/start", b"text=hi",
                                              {"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(status, 415)
            # 头对但内容不是 JSON：400。
            status, body = await self.request("POST", "/api/logins/sample/start", b"not json",
                                              {"Content-Type": "application/json"})
            self.assertEqual(status, 400)
            self.assertEqual(body, {"error": "bad_json"})
            # 不存在的登录和路径：404。
            self.assertEqual((await self.post_json("/api/logins/nope/start", {}))[0], 404)
            self.assertEqual((await self.request("GET", "/api/nothing"))[0], 404)
        finally:
            await self.stop_serve(serve_task)


if __name__ == "__main__":
    unittest.main()
