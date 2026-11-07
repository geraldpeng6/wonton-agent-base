"""[[install]]：npm 包和按架构 URL 两种来源，校验通过/失败、已装同版本不重下。
全部走本地假 HTTP 服务，不连外网。"""
import base64
import hashlib
import http.server
import os
import shutil
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import support

from wonton_agent import install as agent

# setUp 会把 npm_install 打成桩；这里留一份真函数，测 npm 源的 bin 路径时要真跑 npm。
real_npm_install = agent.npm_install

npm_calls: list[Path] = []


class FakeRegistry:
    """按 path 返回预置字节的最小 HTTP 服务，记录请求数。"""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.requests: list[str] = []
        registry = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                registry.requests.append(self.path)
                body = registry.files.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def url(self, path: str) -> str:
        return f"{self.base}{path}"

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()


def npm_package_tgz(version: str = "1.0.0") -> bytes:
    root = Path(tempfile.mkdtemp(prefix="ab-pkg-"))
    try:
        package = root / "package"
        (package / "bin").mkdir(parents=True)
        (package / "package.json").write_text(
            f'{{"name":"echo","version":"{version}","bin":{{"echo":"bin/echo"}}}}')
        (package / "bin" / "echo").write_text("#!/bin/sh\necho package-echo\n")
        (package / "bin" / "echo").chmod(0o755)  # 真实 CLI 包的 tarball 自带执行位
        archive = root / f"echo-{version}.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(package, arcname="package")
        return archive.read_bytes()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def sha512_integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def no_npm(prefix: Path) -> None:
    # npm_install 被 mock 替换：这里只记录，不真跑 npm。
    npm_calls.append(prefix)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ab-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tools = self.tmp / "tools"
        npm_calls.clear()
        patches = [
            mock.patch.object(agent, "TOOLS_ROOT", self.tools),
            mock.patch.object(agent, "npm_install", no_npm),
            # 本地假服务不能走宿主的代理环境变量。
            mock.patch.dict(os.environ, {
                key: "" for key in
                ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
            } | {"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def npm_config(self, integrity: str, base: str) -> dict:
        return {"install": [{
            "name": "echo", "version": "1.0.0", "source": "npm",
            "package": "@scope/echo", "integrity": integrity, "registry": base,
        }]}

    def test_npm_install_success(self):
        data = npm_package_tgz()
        with FakeRegistry({"/@scope/echo/-/echo-1.0.0.tgz": data}) as registry:
            agent.install_tools(self.npm_config(sha512_integrity(data), registry.base))
        final = self.tools / "echo" / "1.0.0"
        self.assertTrue((final / "package.json").is_file())
        self.assertTrue((final / "bin" / "echo").is_file())
        # 底座在解包后调 npm install --prefix 装依赖（测试里被 mock 成记录）：
        # 恰好一次，路径是 staging 目录（名字带 download- 标记，随后整体改名成 final）。
        self.assertEqual(len(npm_calls), 1)
        self.assertTrue(npm_calls[0].is_relative_to(self.tools))
        self.assertIn(".echo-1.0.0.download-", npm_calls[0].name)
        self.assertEqual(registry.requests, ["/@scope/echo/-/echo-1.0.0.tgz"])

    def test_npm_source_cli_lands_at_package_bin_path(self):
        # CLI 本体在包的 tgz 里，位置跟包里 package.json 的 bin 字段走（这里是 bin/echo）：
        # 真 npm install 只把依赖装进 node_modules/，不给包自己的 bin 建 node_modules/.bin
        # 链接。[acp] command 要按包的 bin 字段指到 <版本目录>/bin/<命令>。
        if shutil.which("npm") is None:
            self.skipTest("npm 不可用")
        data = npm_package_tgz()
        with FakeRegistry({"/@scope/echo/-/echo-1.0.0.tgz": data}) as registry:
            with mock.patch.object(agent, "npm_install", real_npm_install):
                # 缓存和更新检查指到临时目录：不碰测试机的 $HOME，也不连外网。
                with mock.patch.dict(os.environ, {
                        "npm_config_cache": str(self.tmp / "npm-cache"),
                        "npm_config_update_notifier": "false"}):
                    agent.install_tools(self.npm_config(sha512_integrity(data), registry.base))
        final = self.tools / "echo" / "1.0.0"
        cli = final / "bin" / "echo"
        self.assertTrue(cli.is_file(), "CLI 在包的 bin 字段位置（<版本目录>/bin/<命令>）")
        self.assertTrue(cli.stat().st_mode & 0o111, "tarball 里的执行位要保留")
        self.assertFalse((final / "node_modules" / ".bin" / "echo").exists(),
                         "npm install 不搬包自己的 bin")

    def test_npm_hex_integrity_accepted(self):
        data = npm_package_tgz()
        with FakeRegistry({"/@scope/echo/-/echo-1.0.0.tgz": data}) as registry:
            agent.install_tools(self.npm_config(hashlib.sha512(data).hexdigest(), registry.base))
        self.assertTrue((self.tools / "echo" / "1.0.0" / "package.json").is_file())

    def test_npm_checksum_failure_rejects(self):
        data = npm_package_tgz()
        wrong = sha512_integrity(b"not the package")
        with FakeRegistry({"/@scope/echo/-/echo-1.0.0.tgz": data}) as registry:
            with self.assertRaises(RuntimeError) as caught:
                agent.install_tools(self.npm_config(wrong, registry.base))
        message = str(caught.exception)
        self.assertIn("echo", message)
        self.assertIn("期望", message)
        self.assertIn("实际", message)
        self.assertFalse((self.tools / "echo" / "1.0.0").exists(), "校验失败不能留下半成品")
        self.assertFalse(list(self.tools.glob(".echo-*")), "staging 要清干净")

    def test_npm_package_without_package_json_rejected(self):
        blob = b"definitely not a tarball"  # 摘要对得上但内容不是包
        with FakeRegistry({"/@scope/echo/-/echo-1.0.0.tgz": blob}) as registry:
            with self.assertRaises(RuntimeError):
                agent.install_tools(self.npm_config(sha512_integrity(blob), registry.base))

    def test_npm_registry_404_fails(self):
        with FakeRegistry({}) as registry:
            with self.assertRaises(OSError):
                agent.install_tools(self.npm_config(sha512_integrity(b"x"), registry.base))

    def test_npm_default_registry_url(self):
        # registry 省略 = 官方源：mock download 抓 URL，不连外网也锁住缺省 URL 的构造，
        # 且 sha512 校验照跑（替身写出正确内容让后面的流程走通）。
        data = npm_package_tgz()
        captured: list[str] = []

        def fake_download(url: str, target: Path) -> None:
            captured.append(url)
            target.write_bytes(data)

        config = {"install": [{"name": "echo", "version": "1.0.0", "source": "npm",
                               "package": "@scope/echo", "integrity": sha512_integrity(data)}]}
        with mock.patch.object(agent, "download", fake_download):
            agent.install_tools(config)
        self.assertEqual(captured, ["https://registry.npmjs.org/@scope/echo/-/echo-1.0.0.tgz"])
        self.assertTrue((self.tools / "echo" / "1.0.0" / "package.json").is_file())

    def test_url_binary_install(self):
        body = b"#!/bin/sh\necho url-tool\n"
        with FakeRegistry({"/tool-1.0.0-linux-x64": body}) as registry:
            agent.install_tools({"install": [{
                "name": "tool", "version": "1.0.0", "source": "url", "format": "binary",
                "url_amd64": registry.url("/tool-1.0.0-linux-x64"),
                "url_arm64": registry.url("/tool-1.0.0-linux-arm64"),
                "sha256_amd64": sha256_hex(body), "sha256_arm64": sha256_hex(b"arm64"),
            }]})
        binary = self.tools / "tool" / "1.0.0" / "tool-1.0.0-linux-x64"
        self.assertTrue(binary.is_file())
        self.assertTrue(binary.stat().st_mode & 0o111, "binary 装完要可执行")

    def test_url_tgz_install(self):
        root = Path(tempfile.mkdtemp(prefix="ab-url-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "tool" / "bin").mkdir(parents=True)
        (root / "tool" / "bin" / "agent").write_text("#!/bin/sh\necho tgz-tool\n")
        archive = root / "tool-1.0.0.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(root / "tool", arcname="tool")
        data = archive.read_bytes()
        with FakeRegistry({"/tool-1.0.0-linux-x64.tar.gz": data}) as registry:
            agent.install_tools({"install": [{
                "name": "tool", "version": "1.0.0", "source": "url", "format": "tgz",
                "url_amd64": registry.url("/tool-1.0.0-linux-x64.tar.gz"),
                "url_arm64": registry.url("/tool-1.0.0-linux-arm64.tar.gz"),
                "sha256_amd64": sha256_hex(data), "sha256_arm64": sha256_hex(b"arm64"),
            }]})
        self.assertTrue((self.tools / "tool" / "1.0.0" / "tool" / "bin" / "agent").is_file())

    def test_url_uses_arm64_fields_on_arm64(self):
        body = b"arm64-binary"
        with FakeRegistry({"/tool-1.0.0-linux-arm64": body}) as registry:
            with mock.patch.object(agent, "current_arch", lambda: "arm64"):
                agent.install_tools({"install": [{
                    "name": "tool", "version": "1.0.0", "source": "url", "format": "binary",
                    "url_amd64": registry.url("/tool-1.0.0-linux-amd64-missing"),
                    "url_arm64": registry.url("/tool-1.0.0-linux-arm64"),
                    "sha256_amd64": sha256_hex(b"amd64"), "sha256_arm64": sha256_hex(body),
                }]})
        self.assertTrue((self.tools / "tool" / "1.0.0" / "tool-1.0.0-linux-arm64").is_file())

    def test_url_checksum_failure_rejects(self):
        with FakeRegistry({"/tool-1.0.0-linux-x64": b"real bytes"}) as registry:
            with self.assertRaises(RuntimeError) as caught:
                agent.install_tools({"install": [{
                    "name": "tool", "version": "1.0.0", "source": "url", "format": "binary",
                    "url_amd64": registry.url("/tool-1.0.0-linux-x64"),
                    "url_arm64": registry.url("/tool-1.0.0-linux-arm64"),
                    "sha256_amd64": sha256_hex(b"expected bytes"),
                    "sha256_arm64": sha256_hex(b"arm64"),
                }]})
        self.assertIn("tool", str(caught.exception))
        self.assertFalse((self.tools / "tool" / "1.0.0").exists())

    def test_same_version_not_downloaded_again(self):
        data = npm_package_tgz()
        with FakeRegistry({"/@scope/echo/-/echo-1.0.0.tgz": data}) as registry:
            config = self.npm_config(sha512_integrity(data), registry.base)
            agent.install_tools(config)
            registry.requests.clear()
            agent.install_tools(config)
        self.assertEqual(registry.requests, [], "版本目录已存在，不重下")

    def test_install_without_section_is_noop(self):
        agent.install_tools({})


if __name__ == "__main__":
    unittest.main()
