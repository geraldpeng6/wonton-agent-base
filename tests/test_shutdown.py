"""入口收到 SIGTERM 要优雅退出：退出码 0、2 秒内退出、ACP 子进程收掉、socket 文件删掉。
起真入口（配置路径和 socket 用环境变量指到临时目录），连上一个连接再发信号，
保证杀的是"转发中的子进程"而不是空转的监听。"""
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ENTRY = Path(__file__).resolve().parent.parent / "wonton-agent"
ECHO_CHILD = Path(__file__).parent / "echo_child.py"


class ShutdownTests(unittest.TestCase):
    def test_sigterm_stops_entry_and_child(self):
        tmp = Path(tempfile.mkdtemp(prefix="ab-shutdown-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        socket_path = tmp / "run" / "acp.sock"
        pidfile = tmp / "child.pid"
        config = tmp / "agent.toml"
        config.write_text(
            "[acp]\n"
            f'command = ["{sys.executable}", "{ECHO_CHILD}", "hold", "{pidfile}"]\n'
        )
        env = {
            **os.environ,
            "WONTON_AGENT_CONFIG": str(config),
            "WONTON_ACP_SOCKET": str(socket_path),
            "WONTON_AGENT_WORKSPACE": str(tmp / "workspace"),
            "HOME": str(tmp / "home"),
        }
        entry = subprocess.Popen([sys.executable, str(ENTRY)], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def reap():
            entry.kill()
            entry.wait()

        self.addCleanup(reap)

        # 等 socket 就绪并连上（bind 和 listen 之间有缝隙，拒连就重试）。
        deadline = time.monotonic() + 10
        while True:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(str(socket_path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                client.close()
                if time.monotonic() > deadline:
                    self.fail("ACP socket 一直没就绪")
                time.sleep(0.05)
        self.addCleanup(client.close)

        # 等 ACP 子进程起来；发信号前确认它活着，不然断言没意义。
        deadline = time.monotonic() + 10
        while not pidfile.exists():
            if time.monotonic() > deadline:
                self.fail("ACP 子进程一直没起来")
            time.sleep(0.05)
        child_pid = int(pidfile.read_text())
        os.kill(child_pid, 0)

        started = time.monotonic()
        entry.send_signal(signal.SIGTERM)
        try:
            _, stderr = entry.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            self.fail(f"SIGTERM 后 2 秒没退出，日志：{entry.communicate()[1].decode()}")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0, f"退出耗时 {elapsed:.2f}s")
        self.assertEqual(entry.returncode, 0)
        self.assertFalse(socket_path.exists(), "退出时要删掉 socket 文件")
        # 入口退出前会等子进程退（stop_process 里 wait 过），退出后子进程必然不在了。
        with self.assertRaises(ProcessLookupError, msg="ACP 子进程还活着"):
            os.kill(child_pid, 0)


if __name__ == "__main__":
    unittest.main()
