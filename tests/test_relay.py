"""ACP 转发：socket 往返、每个连接起一次命令、断连杀子进程、配色码可去可留、起不来要报错。"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support

from wonton_agent import config as paths, relay
ECHO_CHILD = Path(__file__).parent / "echo_child.py"
LOGIN_CHILD = Path(__file__).parent / "login_child.py"


class RelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ab-relay-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patches = [
            mock.patch.object(paths, "SOCKET", self.tmp / "acp.sock"),
            mock.patch.object(paths, "WORKSPACE", self.tmp / "workspace"),
            mock.patch.object(paths, "AGENT_HOME", self.tmp / "home"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    async def start_serve(self, config: dict) -> asyncio.Task:
        serve_task = asyncio.create_task(relay.serve(config))
        for _ in range(200):
            if paths.SOCKET.exists():
                return serve_task
            if serve_task.done():
                raise RuntimeError(f"serve 提前退出：{serve_task.exception()}")
            await asyncio.sleep(0.05)
        self.fail("socket 一直没建起来")

    async def stop_serve(self, serve_task: asyncio.Task) -> None:
        serve_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await serve_task

    async def test_roundtrip_and_per_connection_process(self):
        serve_task = await self.start_serve({"acp": {"command": [sys.executable, str(ECHO_CHILD), "echo"]}})
        try:
            reader1, writer1 = await asyncio.open_unix_connection(str(paths.SOCKET))
            reader2, writer2 = await asyncio.open_unix_connection(str(paths.SOCKET))
            pid1 = (await asyncio.wait_for(reader1.readline(), 5)).decode().strip()
            pid2 = (await asyncio.wait_for(reader2.readline(), 5)).decode().strip()
            self.assertRegex(pid1, r"^pid:\d+$")
            # 每个连接起一次命令，两个连接是两个子进程。
            self.assertNotEqual(pid1, pid2)
            writer1.write(b"hello\n")
            await writer1.drain()
            self.assertEqual(await asyncio.wait_for(reader1.readline(), 5), b"hello\n")
            writer2.write(b"\x1b[31mred\x1b[0m\n")
            await writer2.drain()
            # 配色码在转发时去掉（照搬 codex）。
            self.assertEqual(await asyncio.wait_for(reader2.readline(), 5), b"red\n")
            writer1.close()
            writer2.close()
        finally:
            await self.stop_serve(serve_task)

    async def test_strip_ansi_can_be_disabled(self):
        config = {"acp": {"command": [sys.executable, str(ECHO_CHILD), "echo"], "strip_ansi": False}}
        serve_task = await self.start_serve(config)
        try:
            reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET))
            await asyncio.wait_for(reader.readline(), 5)
            writer.write(b"\x1b[31mred\x1b[0m\n")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readline(), 5), b"\x1b[31mred\x1b[0m\n")
            writer.close()
        finally:
            await self.stop_serve(serve_task)

    async def test_connection_drop_kills_child(self):
        pidfile = self.tmp / "hold.pid"
        config = {"acp": {"command": [sys.executable, str(ECHO_CHILD), "hold", str(pidfile)]}}
        serve_task = await self.start_serve(config)
        try:
            reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET))
            for _ in range(100):
                if pidfile.exists():
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(pidfile.exists(), "子进程没起来")
            pid = int(pidfile.read_text())
            writer.close()
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.1)
            else:
                self.fail("连接断开后子进程没被杀掉")
        finally:
            await self.stop_serve(serve_task)

    async def test_gateway_env_reaches_acp_subprocess(self):
        # [gateway.env]：三个网关变量都有值时，映射的目标变量加进 [acp] 子进程环境。
        config = {
            "acp": {"command": [sys.executable, str(LOGIN_CHILD), "env", "ANTHROPIC_AUTH_TOKEN"]},
            "gateway": {"hook": "/usr/local/lib/wonton-agent/gateway",
                        "env": {"ANTHROPIC_AUTH_TOKEN": "WONTON_MODEL_API_KEY"}},
        }
        with mock.patch.dict(os.environ, {
                "WONTON_MODEL_BASE_URL": "http://gateway.example.internal/v1",
                "WONTON_MODEL_API_KEY": "secret",
                "WONTON_MODEL_DEFAULT": "group/default"}):
            serve_task = await self.start_serve(config)
            try:
                reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET))
                self.assertEqual((await asyncio.wait_for(reader.readline(), 5)).decode().strip(),
                                 "secret")
                writer.close()
            finally:
                await self.stop_serve(serve_task)

    async def test_acp_subprocess_gets_no_gateway_env_without_gateway(self):
        # 没接网关就不注入：目标变量在子进程里是 unset，跟环境里有没有 WONTON_MODEL_* 无关。
        config = {"acp": {"command": [sys.executable, str(LOGIN_CHILD), "env", "ANTHROPIC_AUTH_TOKEN"]}}
        with mock.patch.dict(os.environ):
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
            os.environ.pop("WONTON_MODEL_API_KEY", None)
            serve_task = await self.start_serve(config)
            try:
                reader, writer = await asyncio.open_unix_connection(str(paths.SOCKET))
                self.assertEqual((await asyncio.wait_for(reader.readline(), 5)).decode().strip(),
                                 "unset")
                writer.close()
            finally:
                await self.stop_serve(serve_task)

    async def test_spawn_failure_raises(self):
        config = {"acp": {"command": ["/nonexistent/acp-binary"]}}
        with self.assertRaises(RuntimeError) as caught:
            await relay.serve(config)
        self.assertIn("/nonexistent/acp-binary", str(caught.exception))

    async def test_socket_is_private(self):
        serve_task = await self.start_serve({"acp": {"command": [sys.executable, str(ECHO_CHILD), "echo"]}})
        try:
            mode = paths.SOCKET.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, "socket 只有桥和宿主诊断能连")
        finally:
            await self.stop_serve(serve_task)
            self.assertFalse(paths.SOCKET.exists(), "退出时收掉 socket")


if __name__ == "__main__":
    unittest.main()
