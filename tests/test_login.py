"""[[login]] 终端登录：authMethods 改写、登录 socket 协议（lines / line_then_eof / 未知 id /
env 合并）、桥断开收进程、成功后入口退出（os._exit 打成桩，等 2 秒调短）。
登录进程是 tests/login_child.py，用 relay.serve 真起监听，再连 socket 按桥的协议走。"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support

from wonton_agent import config as paths, login, relay
ECHO_CHILD = Path(__file__).parent / "echo_child.py"
LOGIN_CHILD = Path(__file__).parent / "login_child.py"

INITIALIZE_RESULT = json.dumps({
    "jsonrpc": "2.0", "id": 1,
    "result": {"protocolVersion": 1, "authMethods": [{"id": "x", "name": "X", "type": "prompt"}]},
})

LOGIN_SOCKET = lambda: paths.SOCKET.with_suffix(".login.sock")


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ab-login-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patches = [
            mock.patch.object(paths, "SOCKET", self.tmp / "run" / "acp.sock"),
            mock.patch.object(paths, "WORKSPACE", self.tmp / "workspace"),
            mock.patch.object(paths, "AGENT_HOME", self.tmp / "home"),
            # 成功后的重启等待和断连宽限调短，测试不必等满 2 秒。
            mock.patch.object(login, "RESTART_DELAY", 0.1),
            mock.patch.object(login, "DISCONNECT_GRACE", 0.3),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        # relay_login 登录成功会 os._exit(0)：打成桩记调用，测试进程活下来。
        self.exit_mock = mock.patch.object(os, "_exit").start()
        self.addCleanup(mock.patch.stopall)

    async def start_serve(self, config: dict) -> asyncio.Task:
        serve_task = asyncio.create_task(relay.serve(config))
        for _ in range(200):
            if paths.SOCKET.exists():
                return serve_task
            if serve_task.done():
                raise RuntimeError(f"serve 提前退出：{serve_task.exception()}")
            await asyncio.sleep(0.05)
        self.fail("ACP socket 一直没建起来")

    async def stop_serve(self, serve_task: asyncio.Task) -> None:
        serve_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await serve_task

    async def connect_login(self):
        return await asyncio.open_unix_connection(str(LOGIN_SOCKET()))

    async def send_request(self, writer, method_id: str = "sample",
                           env: dict | None = None, args: list | None = None) -> None:
        request = {"methodId": method_id, "args": args or [], "env": env or {}}
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()

    async def read_frame(self, reader, timeout: float = 5) -> dict:
        return json.loads(await asyncio.wait_for(reader.readline(), timeout))

    async def test_auth_methods_rewritten_when_login_configured(self):
        config = {
            "acp": {"command": [sys.executable, str(ECHO_CHILD), "echo"]},
            "login": [{"id": "chatgpt", "name": "ChatGPT 账号（设备码）",
                       "command": [sys.executable, str(LOGIN_CHILD), "echo"]}],
        }
        serve_task = await self.start_serve(config)
        try:
            # 登录 socket 先于 ACP socket 建好，权限 0600。
            self.assertTrue(LOGIN_SOCKET().exists())
            self.assertEqual(LOGIN_SOCKET().stat().st_mode & 0o777, 0o600)
            reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET))
            await asyncio.wait_for(reader.readline(), 5)  # echo 子进程先报 pid
            writer.write((INITIALIZE_RESULT + "\n").encode())
            await writer.drain()
            reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
            self.assertEqual(reply["result"]["authMethods"], [
                {"id": "chatgpt", "name": "ChatGPT 账号（设备码）", "type": "terminal", "args": []},
            ])
            # 非 authMethods 的行原样过。
            other = json.dumps({"jsonrpc": "2.0", "method": "session/new", "params": {}})
            writer.write((other + "\n").encode())
            await writer.drain()
            self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 5)),
                             json.loads(other))
            writer.close()
        finally:
            await self.stop_serve(serve_task)

    def test_initialize_without_auth_methods_gets_the_logins(self):
        methods = [{"id": "sample", "name": "S", "type": "terminal", "args": []}]
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}})
        self.assertEqual(json.loads(relay.rewrite_auth_methods(line, methods))["result"]["authMethods"],
                         methods)
        other = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s"}})
        self.assertEqual(relay.rewrite_auth_methods(other, methods), other)

    async def test_auth_methods_forwarded_without_login(self):
        serve_task = await self.start_serve(
            {"acp": {"command": [sys.executable, str(ECHO_CHILD), "echo"]}})
        try:
            self.assertFalse(LOGIN_SOCKET().exists(), "没有 [[login]] 不建登录 socket")
            reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET))
            await asyncio.wait_for(reader.readline(), 5)
            writer.write((INITIALIZE_RESULT + "\n").encode())
            await writer.drain()
            reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
            self.assertEqual(reply["result"]["authMethods"],
                             [{"id": "x", "name": "X", "type": "prompt"}])
            writer.close()
        finally:
            await self.stop_serve(serve_task)

    async def test_lines_mode_forwards_each_line(self):
        config = {
            "acp": {"command": [sys.executable, str(ECHO_CHILD), "echo"]},
            "login": [{"id": "sample", "name": "样例",
                       "command": [sys.executable, str(LOGIN_CHILD), "echo", "2"]}],
        }
        serve_task = await self.start_serve(config)
        try:
            reader, writer = await self.connect_login()
            # args 和 env 都带上：args 要被忽略（配置命令照常跑），env 要合并进进程。
            await self.send_request(writer, args=["--evil", "junk"], env={"AB_LOGIN_PROBE": "v1"})
            # echo 子进程收满 2 行才退出，先把两行都送进去。
            writer.write(b"first line\n")
            await writer.drain()
            writer.write(b"second line\n")
            await writer.drain()
            self.assertEqual(await self.read_frame(reader), {"out": "got:first line"})
            self.assertEqual(await self.read_frame(reader), {"out": "got:second line"})
            self.assertEqual(await self.read_frame(reader), {"exit": 0})
            # 退出码 0 且 restart_on_success 缺省 true：回完 exit 帧等 RESTART_DELAY 后入口退出。
            for _ in range(100):
                if self.exit_mock.call_count:
                    break
                await asyncio.sleep(0.02)
            self.exit_mock.assert_called_once_with(0)
            writer.close()
        finally:
            await self.stop_serve(serve_task)

    async def test_env_merged_into_login_process(self):
        config = {
            "acp": {"command": [sys.executable, str(ECHO_CHILD), "echo"]},
            "login": [{"id": "sample", "name": "样例",
                       "command": [sys.executable, str(LOGIN_CHILD), "env", "AB_LOGIN_PROBE"]}],
        }
        serve_task = await self.start_serve(config)
        try:
            reader, writer = await self.connect_login()
            await self.send_request(writer, env={"AB_LOGIN_PROBE": "secret123"})
            self.assertEqual(await self.read_frame(reader), {"out": "secret123"})
            self.assertEqual(await self.read_frame(reader), {"exit": 0})
            writer.close()
        finally:
            await self.stop_serve(serve_task)

    async def test_line_then_eof_closes_stdin_after_first_line(self):
        config = {
            "acp": {"command": [sys.executable, str(ECHO_CHILD), "echo"]},
            "login": [{"id": "sample", "name": "样例",
                       "command": [sys.executable, str(LOGIN_CHILD), "eof"],
                       "stdin": "line_then_eof"}],
        }
        serve_task = await self.start_serve(config)
        try:
            reader, writer = await self.connect_login()
            await self.send_request(writer)
            writer.write(b"the-key\n")
            await writer.drain()
            # 子进程读到一行后收到 EOF 才收尾，eof:<行数> 证明只转了第一行就关了 stdin。
            self.assertEqual(await self.read_frame(reader), {"out": "eof:1:the-key"})
            self.assertEqual(await self.read_frame(reader), {"exit": 0})
            for _ in range(100):
                if self.exit_mock.call_count:
                    break
                await asyncio.sleep(0.02)
            self.exit_mock.assert_called_once_with(0)
            writer.close()
        finally:
            await self.stop_serve(serve_task)

    async def test_partial_line_flushed_before_next_output(self):
        config = {
            "acp": {"command": [sys.executable, str(ECHO_CHILD), "echo"]},
            "login": [{"id": "sample", "name": "样例",
                       "command": [sys.executable, str(LOGIN_CHILD), "prompt"]}],
        }
        serve_task = await self.start_serve(config)
        try:
            reader, writer = await self.connect_login()
            await self.send_request(writer)
            # 提示不带换行：200ms 没有新字节就要先发出来，1 秒内收到，不等下一次输出。
            self.assertEqual(await self.read_frame(reader, timeout=1),
                             {"out": "Paste code here if prompted > "})
            # 后面的接着拼：回答之后进程回一行、退出，帧里不加换行也不重发提示。
            writer.write(b"the-key\n")
            await writer.drain()
            self.assertEqual(await self.read_frame(reader), {"out": "got:the-key"})
            self.assertEqual(await self.read_frame(reader), {"exit": 0})
            writer.close()
        finally:
            await self.stop_serve(serve_task)

    async def test_unknown_method_id_rejected_and_serial_loop_continues(self):
        config = {
            "acp": {"command": [sys.executable, str(ECHO_CHILD), "echo"]},
            "login": [{"id": "sample", "name": "样例",
                       "command": [sys.executable, str(LOGIN_CHILD), "echo", "1"]}],
        }
        serve_task = await self.start_serve(config)
        try:
            reader, writer = await self.connect_login()
            await self.send_request(writer, method_id="nope")
            self.assertEqual(await self.read_frame(reader),
                             {"err": "没有这种登录方式：nope"})
            self.assertEqual(await self.read_frame(reader), {"exit": 2})
            writer.close()
            # 登录串行，这次作废后循环还活着：下一个连接照常服务。
            reader2, writer2 = await self.connect_login()
            await self.send_request(writer2)
            writer2.write(b"only\n")
            await writer2.drain()
            self.assertEqual(await self.read_frame(reader2), {"out": "got:only"})
            self.assertEqual(await self.read_frame(reader2), {"exit": 0})
            writer2.close()
        finally:
            await self.stop_serve(serve_task)

    async def test_bridge_disconnect_ends_login_process(self):
        pidfile = self.tmp / "login.pid"
        config = {
            "acp": {"command": [sys.executable, str(ECHO_CHILD), "echo"]},
            "login": [{"id": "sample", "name": "样例",
                       "command": [sys.executable, str(LOGIN_CHILD), "hold", str(pidfile)]}],
        }
        serve_task = await self.start_serve(config)
        try:
            reader, writer = await self.connect_login()
            await self.send_request(writer)
            for _ in range(200):
                if pidfile.exists():
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(pidfile.exists(), "登录进程一直没起来")
            pid = int(pidfile.read_text())
            os.kill(pid, 0)
            # 桥中途断开：stdin 关掉、terminate、kill，登录进程被收掉。
            writer.close()
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                if asyncio.get_running_loop().time() > deadline:
                    self.fail("桥断开后登录进程没被收掉")
                await asyncio.sleep(0.05)
        finally:
            await self.stop_serve(serve_task)

    async def test_no_restart_on_failure_or_opt_out(self):
        config = {
            "acp": {"command": [sys.executable, str(ECHO_CHILD), "echo"]},
            "login": [
                {"id": "fails", "name": "失败", "command": [sys.executable, str(LOGIN_CHILD), "exit", "3"]},
                {"id": "manual", "name": "不重启",
                 "command": [sys.executable, str(LOGIN_CHILD), "exit", "0"],
                 "restart_on_success": False},
            ],
        }
        serve_task = await self.start_serve(config)
        try:
            reader, writer = await self.connect_login()
            await self.send_request(writer, method_id="fails")
            self.assertEqual(await self.read_frame(reader), {"exit": 3})
            writer.close()
            reader2, writer2 = await self.connect_login()
            await self.send_request(writer2, method_id="manual")
            self.assertEqual(await self.read_frame(reader2), {"exit": 0})
            writer2.close()
            # 非 0 退出码和显式关掉 restart_on_success 都不退出入口。
            await asyncio.sleep(login.RESTART_DELAY * 3)
            self.exit_mock.assert_not_called()
        finally:
            await self.stop_serve(serve_task)


if __name__ == "__main__":
    unittest.main()
