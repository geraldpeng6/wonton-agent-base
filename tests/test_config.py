"""agent.toml 解析和校验。"""
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

import support

from wonton_agent import config as agent


def write_config(body: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="ab-config-"))
    path = tmp / "agent.toml"
    path.write_text(body)
    return path


def load(body: str) -> tuple[dict | None, str | None]:
    stderr = StringIO()
    with redirect_stderr(stderr):
        try:
            return agent.load_config(write_config(body)), None
        except SystemExit as error:
            return None, stderr.getvalue()


class LoadConfigTests(unittest.TestCase):
    def test_full_config_parses(self):
        config, message = load("""
            [acp]
            command = ["/usr/local/bin/foo-acp", "--stdio"]
            strip_ansi = false

            [[install]]
            name = "foo"
            version = "1.2.3"
            source = "npm"
            package = "@scope/foo"
            integrity = "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="

            [[install]]
            name = "bar"
            version = "0.1.0"
            source = "url"
            url_amd64 = "https://example.com/bar-linux-x64"
            url_arm64 = "https://example.com/bar-linux-arm64"
            sha256_amd64 = "%s"
            sha256_arm64 = "%s"

            [skills]
            target = "~/.agents/skills"

            [prompt]
            target = "~/.foo/PROMPT.md"

            [gateway]
            hook = "/usr/local/lib/wonton-agent/gateway"
        """ % ("a" * 64, "b" * 64))
        self.assertIsNone(message)
        self.assertEqual(config["acp"]["command"], ["/usr/local/bin/foo-acp", "--stdio"])
        self.assertIs(config["acp"]["strip_ansi"], False)
        self.assertEqual(len(config["install"]), 2)
        self.assertEqual(config["skills"]["target"], "~/.agents/skills")

    def test_minimal_config(self):
        config, message = load('[acp]\ncommand = ["x"]')
        self.assertIsNone(message)
        # strip_ansi 缺省 true：转发时去掉终端配色码（缺省在用时取，解析结果没有这个键）。
        self.assertIs(config["acp"].get("strip_ansi", True), True)

    def test_missing_acp_rejected(self):
        _, message = load("[skills]\ntarget = '~/.x'\n")
        self.assertIn("[acp]", message)

    def test_empty_command_rejected(self):
        _, message = load("[acp]\ncommand = []\n")
        self.assertIn("command", message)

    def test_non_string_command_rejected(self):
        _, message = load("[acp]\ncommand = [1]\n")
        self.assertIn("command", message)

    def test_unknown_section_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[gatewayy]\nhook = "/x"\n')
        self.assertIn("gatewayy", message)

    def test_bad_strip_ansi_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\nstrip_ansi = "yes"\n')
        self.assertIn("strip_ansi", message)

    def test_missing_target_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[skills]\n')
        self.assertIn("target", message)

    def test_missing_hook_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[gateway]\n')
        self.assertIn("hook", message)

    def test_gateway_env_table_accepted(self):
        config, message = load('[acp]\ncommand = ["x"]\n\n[gateway]\nhook = "/x"\n\n'
                               '[gateway.env]\nANTHROPIC_AUTH_TOKEN = "WONTON_MODEL_API_KEY"\n')
        self.assertIsNone(message)
        self.assertEqual(config["gateway"]["env"], {"ANTHROPIC_AUTH_TOKEN": "WONTON_MODEL_API_KEY"})

    def test_gateway_env_bad_mapping_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[gateway]\nhook = "/x"\n\n'
                          '[gateway.env]\nANTHROPIC_AUTH_TOKEN = "not a var name"\n')
        self.assertIn("gateway.env", message)

    def test_install_bad_name_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[install]]\nname = "../evil"\nversion = "1"\nsource = "url"\n')
        self.assertIn("name", message)

    def test_install_missing_version_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[install]]\nname = "foo"\nsource = "url"\n')
        self.assertIn("version", message)

    def test_install_unknown_source_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[install]]\nname = "foo"\nversion = "1"\nsource = "git"\n')
        self.assertIn("source", message)

    def test_install_npm_missing_integrity_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[install]]\nname = "foo"\nversion = "1"\n'
                          'source = "npm"\npackage = "foo"\n')
        self.assertIn("integrity", message)

    def test_install_url_missing_arm64_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[install]]\nname = "foo"\nversion = "1"\nsource = "url"\n'
                          'url_amd64 = "https://example.com/x"\nsha256_amd64 = "%s"\n' % ("c" * 64))
        self.assertIn("url_arm64", message)

    def test_install_bad_sha256_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[install]]\nname = "foo"\nversion = "1"\nsource = "url"\n'
                          'url_amd64 = "https://example.com/x"\nurl_arm64 = "https://example.com/y"\n'
                          'sha256_amd64 = "zz"\nsha256_arm64 = "%s"\n' % ("c" * 64))
        self.assertIn("sha256", message)

    def test_install_bad_format_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[install]]\nname = "foo"\nversion = "1"\nsource = "url"\n'
                          'url_amd64 = "https://example.com/x"\nurl_arm64 = "https://example.com/y"\n'
                          'sha256_amd64 = "%s"\nsha256_arm64 = "%s"\nformat = "zip"\n' % ("c" * 64, "d" * 64))
        self.assertIn("format", message)

    def test_install_bad_registry_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[install]]\nname = "foo"\nversion = "1"\nsource = "npm"\n'
                          'package = "foo"\nintegrity = "%s"\nregistry = "registry.example.com"\n' % ("e" * 128))
        self.assertIn("registry", message)

    def test_install_registry_optional(self):
        # registry 省略 = 官方 npm 源；解析要放行（下载 URL 的缺省在运行时冒烟里真网验证）。
        config, message = load('[acp]\ncommand = ["x"]\n\n[[install]]\nname = "foo"\nversion = "1"\n'
                               'source = "npm"\npackage = "foo"\nintegrity = "%s"\n' % ("e" * 128))
        self.assertIsNone(message)
        self.assertEqual(config["install"][0]["name"], "foo")

    def test_login_section_parses_with_defaults(self):
        config, message = load('[acp]\ncommand = ["x"]\n\n[[login]]\nid = "chatgpt"\n'
                               'name = "ChatGPT 账号（设备码）"\n'
                               'command = ["/usr/local/bin/codex", "login", "--device-auth"]\n')
        self.assertIsNone(message)
        section = config["login"][0]
        # stdin / restart_on_success 缺省在用时取（lines、true），解析结果没有这个键。
        self.assertEqual(section.get("stdin", "lines"), "lines")
        self.assertIs(section.get("restart_on_success", True), True)

    def test_login_multiple_sections_parse(self):
        config, message = load('[acp]\ncommand = ["x"]\n\n[[login]]\nid = "a"\nname = "A"\ncommand = ["/a"]\n'
                               '[[login]]\nid = "b"\nname = "B"\ncommand = ["/b"]\nstdin = "line_then_eof"\n'
                               'restart_on_success = false\n')
        self.assertIsNone(message)
        self.assertEqual([s["id"] for s in config["login"]], ["a", "b"])
        self.assertEqual(config["login"][1]["stdin"], "line_then_eof")
        self.assertIs(config["login"][1]["restart_on_success"], False)

    def test_login_bad_id_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[login]]\nid = "../evil"\nname = "A"\ncommand = ["/a"]\n')
        self.assertIn("id", message)

    def test_login_duplicate_id_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[login]]\nid = "a"\nname = "A"\ncommand = ["/a"]\n'
                          '[[login]]\nid = "a"\nname = "A2"\ncommand = ["/a2"]\n')
        self.assertIn("重复", message)

    def test_login_missing_name_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[login]]\nid = "a"\ncommand = ["/a"]\n')
        self.assertIn("name", message)

    def test_login_empty_command_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[login]]\nid = "a"\nname = "A"\ncommand = []\n')
        self.assertIn("command", message)

    def test_login_bad_stdin_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[login]]\nid = "a"\nname = "A"\ncommand = ["/a"]\n'
                          'stdin = "bytes"\n')
        self.assertIn("stdin", message)

    def test_login_bad_restart_rejected(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[login]]\nid = "a"\nname = "A"\ncommand = ["/a"]\n'
                          'restart_on_success = "yes"\n')
        self.assertIn("restart_on_success", message)

    def test_login_status_validates(self):
        _, message = load('[acp]\ncommand = ["x"]\n\n[[login]]\nid = "a"\nname = "A"\ncommand = ["/a"]\n'
                          'status = []\n')
        self.assertIn("status", message)
        _, message = load('[acp]\ncommand = ["x"]\n\n[[login]]\nid = "a"\nname = "A"\ncommand = ["/a"]\n'
                          'status = ["a", 3]\n')
        self.assertIn("status", message)
        config, message = load('[acp]\ncommand = ["x"]\n\n[[login]]\nid = "a"\nname = "A"\n'
                               'command = ["/a"]\nstatus = ["codex", "login", "status"]\n')
        self.assertIsNone(message)
        self.assertEqual(config["login"][0]["status"], ["codex", "login", "status"])

    def test_web_section_validates(self):
        login = '[[login]]\nid = "a"\nname = "A"\ncommand = ["/a"]\n'
        # 没有 [[login]] 而写了 [web]：启动即报错。
        _, message = load("[web]\nport = 8080\n")
        self.assertIn("[[login]]", message)
        # 缺 port、端口出界、类型不对都报错。
        _, message = load(f'[acp]\ncommand = ["x"]\n\n[web]\n{login}')
        self.assertIn("port", message)
        _, message = load(f'[acp]\ncommand = ["x"]\n\n[web]\nport = 0\n{login}')
        self.assertIn("port", message)
        _, message = load(f'[acp]\ncommand = ["x"]\n\n[web]\nport = 70000\n{login}')
        self.assertIn("port", message)
        _, message = load(f'[acp]\ncommand = ["x"]\n\n[web]\nport = "8080"\n{login}')
        self.assertIn("port", message)
        config, message = load(f'[acp]\ncommand = ["x"]\n\n[web]\nport = 8080\n{login}')
        self.assertIsNone(message)
        self.assertEqual(config["web"]["port"], 8080)

    def test_invalid_toml_rejected(self):
        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                agent.load_config(write_config("[acp\ncommand ="))
        self.assertIn("TOML", stderr.getvalue())

    def test_missing_config_file_names_the_fix(self):
        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                agent.load_config(Path("/nonexistent/agent.toml"))
        self.assertIn("COPY agent.toml /etc/wonton-agent/", stderr.getvalue())


class ParseIntegrityTests(unittest.TestCase):
    def test_npm_base64_form(self):
        import base64
        import hashlib
        digest = hashlib.sha512(b"x").digest()
        algo, expected = agent.parse_integrity("sha512-" + base64.b64encode(digest).decode())
        self.assertEqual((algo, expected), ("sha512", digest.hex()))

    def test_hex_form(self):
        algo, expected = agent.parse_integrity("A" * 128)
        self.assertEqual((algo, expected), ("sha512", "a" * 128))

    def test_rejects_other_algorithms_and_lengths(self):
        for value in ("sha256-AAAA", "a" * 64, "", "sha512-not-base64!!"):
            with self.assertRaises(ValueError):
                agent.parse_integrity(value)


if __name__ == "__main__":
    unittest.main()
