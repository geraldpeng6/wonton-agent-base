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

agent = support.agent
ECHO_CHILD = Path(__file__).parent / "echo_child.py"


class RelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ab-relay-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patches = [
            mock.patch.object(agent, "SOCKET", self.tmp / "acp.sock"),
            mock.patch.object(agent, "WORKSPACE", self.tmp / "workspace"),
            mock.patch.object(agent, "AGENT_HOME", self.tmp / "home"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    async def start_serve(self, config: dict) -> asyncio.Task:
        serve_task = asyncio.create_task(agent.serve(config))
        for _ in range(200):
            if agent.SOCKET.exists():
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
            reader1, writer1 = await asyncio.open_unix_connection(str(agent.SOCKET))
            reader2, writer2 = await asyncio.open_unix_connection(str(agent.SOCKET))
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
            reader, writer = await asyncio.open_unix_connection(str(agent.SOCKET))
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
            reader, writer = await asyncio.open_unix_connection(str(agent.SOCKET))
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

    async def test_spawn_failure_raises(self):
        config = {"acp": {"command": ["/nonexistent/acp-binary"]}}
        with self.assertRaises(RuntimeError) as caught:
            await agent.serve(config)
        self.assertIn("/nonexistent/acp-binary", str(caught.exception))

    async def test_socket_is_private(self):
        serve_task = await self.start_serve({"acp": {"command": [sys.executable, str(ECHO_CHILD), "echo"]}})
        try:
            mode = agent.SOCKET.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, "socket 只有桥和宿主诊断能连")
        finally:
            await self.stop_serve(serve_task)
            self.assertFalse(agent.SOCKET.exists(), "退出时收掉 socket")


if __name__ == "__main__":
    unittest.main()
