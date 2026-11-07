"""模型网关钩子：变量都有值调 apply，任一为空调 clear；钩子收得到环境变量；失败要报错。"""
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

import support

from wonton_agent import gateway as agent


def write_hook(directory: Path, body: str) -> Path:
    hook = directory / "gateway-hook"
    hook.write_text("#!/usr/bin/env python3\nimport sys\n" + body)
    hook.chmod(0o755)
    return hook


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ab-gateway-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.record = self.tmp / "record"

    def run_gateway(self, config: dict, env: dict) -> str:
        # 不用 clear=True：清掉 PATH 会让钩子的 #!/usr/bin/env python3 找不到解释器；
        # 缺省变量显式给空串，和核心注入空值的语义一致。
        values = {"WONTON_MODEL_BASE_URL": "", "WONTON_MODEL_API_KEY": "", "WONTON_MODEL_DEFAULT": ""}
        values.update(env)
        with mock.patch.dict(os.environ, values):
            agent.apply_gateway(config)
        return self.record.read_text()

    def test_all_variables_present_applies(self):
        hook = write_hook(self.tmp, f"from pathlib import Path\n"
                                    f"Path({str(self.record)!r}).write_text(sys.argv[1])\n")
        action = self.run_gateway({"gateway": {"hook": str(hook)}}, {
            "WONTON_MODEL_BASE_URL": "http://gateway.example.internal/v1",
            "WONTON_MODEL_API_KEY": "secret",
            "WONTON_MODEL_DEFAULT": "group/default",
        })
        self.assertEqual(action, "apply")

    def test_empty_variable_clears(self):
        hook = write_hook(self.tmp, f"from pathlib import Path\n"
                                    f"Path({str(self.record)!r}).write_text(sys.argv[1])\n")
        action = self.run_gateway({"gateway": {"hook": str(hook)}}, {
            "WONTON_MODEL_BASE_URL": "http://gateway.example.internal/v1",
            "WONTON_MODEL_API_KEY": "",
            "WONTON_MODEL_DEFAULT": "group/default",
        })
        self.assertEqual(action, "clear")

    def test_missing_variable_clears(self):
        hook = write_hook(self.tmp, f"from pathlib import Path\n"
                                    f"Path({str(self.record)!r}).write_text(sys.argv[1])\n")
        action = self.run_gateway({"gateway": {"hook": str(hook)}}, {
            "WONTON_MODEL_BASE_URL": "http://gateway.example.internal/v1",
        })
        self.assertEqual(action, "clear")

    def test_hook_receives_variable_values(self):
        # 钩子从环境里拿网关配置，底座原样传下去。
        hook = write_hook(self.tmp, "import os\n"
                                    f"from pathlib import Path\n"
                                    f"Path({str(self.record)!r}).write_text(os.environ['WONTON_MODEL_API_KEY'])\n")
        value = self.run_gateway({"gateway": {"hook": str(hook)}}, {
            "WONTON_MODEL_BASE_URL": "http://gateway.example.internal/v1",
            "WONTON_MODEL_API_KEY": "secret-value",
            "WONTON_MODEL_DEFAULT": "group/default",
        })
        self.assertEqual(value, "secret-value")

    def test_hook_failure_raises(self):
        hook = write_hook(self.tmp, "import sys\nsys.exit(3)\n")
        with self.assertRaises(RuntimeError) as caught:
            self.run_gateway({"gateway": {"hook": str(hook)}}, {
                "WONTON_MODEL_BASE_URL": "x", "WONTON_MODEL_API_KEY": "y", "WONTON_MODEL_DEFAULT": "z",
            })
        self.assertIn("退出码 3", str(caught.exception))

    def test_missing_hook_rejected(self):
        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                self.run_gateway({"gateway": {"hook": str(self.tmp / "no-such-hook")}}, {})
        self.assertIn("不可执行", stderr.getvalue())

    def test_non_executable_hook_rejected(self):
        hook = self.tmp / "not-executable"
        hook.write_text("#!/usr/bin/env python3\n")
        hook.chmod(stat.S_IRUSR | stat.S_IWUSR)
        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                self.run_gateway({"gateway": {"hook": str(hook)}}, {})
        self.assertIn("不可执行", stderr.getvalue())

    def test_no_gateway_section_is_noop(self):
        agent.apply_gateway({})  # 不该抛错，也不该起进程


class AcpEnvTests(unittest.TestCase):
    """[gateway.env]：网关接好才把映射的变量加进 [acp] 子进程环境，否则不加。"""

    def run_env(self, config: dict, env: dict) -> dict:
        # 缺省变量显式给空串，和核心注入空值的语义一致（照 GatewayTests.run_gateway）。
        values = {"WONTON_MODEL_BASE_URL": "", "WONTON_MODEL_API_KEY": "", "WONTON_MODEL_DEFAULT": ""}
        values.update(env)
        with mock.patch.dict(os.environ, values):
            return agent.acp_env(config)

    def test_gateway_ready_maps_variables(self):
        env = self.run_env(
            {"gateway": {"hook": "/x", "env": {
                "ANTHROPIC_AUTH_TOKEN": "WONTON_MODEL_API_KEY",
                "ANTHROPIC_BASE_URL": "WONTON_MODEL_BASE_URL"}}},
            {"WONTON_MODEL_BASE_URL": "http://gateway.example.internal/v1",
             "WONTON_MODEL_API_KEY": "secret", "WONTON_MODEL_DEFAULT": "group/default"})
        self.assertEqual(env, {"ANTHROPIC_AUTH_TOKEN": "secret",
                               "ANTHROPIC_BASE_URL": "http://gateway.example.internal/v1"})

    def test_no_gateway_section_maps_nothing(self):
        env = self.run_env({"acp": {"command": ["x"]}}, {
            "WONTON_MODEL_BASE_URL": "x", "WONTON_MODEL_API_KEY": "y", "WONTON_MODEL_DEFAULT": "z"})
        self.assertEqual(env, {})

    def test_gateway_without_env_table_maps_nothing(self):
        env = self.run_env({"gateway": {"hook": "/x"}}, {
            "WONTON_MODEL_BASE_URL": "x", "WONTON_MODEL_API_KEY": "y", "WONTON_MODEL_DEFAULT": "z"})
        self.assertEqual(env, {})

    def test_incomplete_gateway_variables_map_nothing(self):
        env = self.run_env(
            {"gateway": {"hook": "/x", "env": {"ANTHROPIC_AUTH_TOKEN": "WONTON_MODEL_API_KEY"}}},
            {"WONTON_MODEL_BASE_URL": "x", "WONTON_MODEL_API_KEY": "", "WONTON_MODEL_DEFAULT": "z"})
        self.assertEqual(env, {}, "钩子 clear 时子进程也不注入")

    def test_missing_source_variable_dies(self):
        # 映射的来源变量没值：配置和实际环境对不上，报错退出，不静默跳过。
        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                self.run_env(
                    {"gateway": {"hook": "/x", "env": {"ANTHROPIC_AUTH_TOKEN": "AB_NO_SUCH_SOURCE"}}},
                    {"WONTON_MODEL_BASE_URL": "x", "WONTON_MODEL_API_KEY": "y",
                     "WONTON_MODEL_DEFAULT": "z"})
        self.assertIn("AB_NO_SUCH_SOURCE", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
