"""[skills]/[prompt]：镜像里的内容拷到指定目录，每次启动覆盖。"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support

from wonton_agent import assets as agent


class SkillsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ab-skills-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        source = self.tmp / "image-skills" / "wonton-help"
        (source / "refs").mkdir(parents=True)
        (source / "SKILL.md").write_text("---\nname: wonton-help\ndescription: 帮助\n---\n正文\n")
        (source / "refs" / "x.md").write_text("参考\n")
        prompt = self.tmp / "image-prompt.md"
        prompt.write_text("你是 Wonton 里的 Agent。\n")
        patches = [
            mock.patch.object(agent, "SKILL_SOURCE", self.tmp / "image-skills"),
            mock.patch.object(agent, "PROMPT_SOURCE", prompt),
            mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home")}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_skills_tree_copied(self):
        agent.install_skills({"skills": {"target": "~/.agents/skills"}})
        target = self.tmp / "home" / ".agents" / "skills" / "wonton-help"
        self.assertTrue((target / "SKILL.md").is_file())
        self.assertEqual((target / "refs" / "x.md").read_text(), "参考\n")

    def test_skills_overwritten_on_rerun(self):
        target = self.tmp / "home" / ".agents" / "skills" / "wonton-help"
        agent.install_skills({"skills": {"target": "~/.agents/skills"}})
        (target / "SKILL.md").write_text("被改过的内容\n")
        agent.install_skills({"skills": {"target": "~/.agents/skills"}})
        self.assertIn("正文", (target / "SKILL.md").read_text(), "每次启动按镜像里的版本覆盖")

    def test_skills_absolute_target(self):
        target = self.tmp / "custom-skills"
        agent.install_skills({"skills": {"target": str(target)}})
        self.assertTrue((target / "wonton-help" / "SKILL.md").is_file())

    def test_no_skills_section_is_noop(self):
        agent.install_skills({})
        self.assertFalse((self.tmp / "home" / ".agents").exists())

    def test_prompt_copied_with_directories(self):
        agent.install_prompt({"prompt": {"target": "~/.deep/PROMPT.md"}})
        copied = self.tmp / "home" / ".deep" / "PROMPT.md"
        self.assertEqual(copied.read_text(), "你是 Wonton 里的 Agent。\n")

    def test_no_prompt_section_is_noop(self):
        agent.install_prompt({})
        self.assertFalse((self.tmp / "home").exists())


if __name__ == "__main__":
    unittest.main()
