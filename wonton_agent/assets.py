"""[skills]/[prompt]：镜像里的 skill 树和系统提示拷到 agent.toml 指定的目录，每次启动覆盖。"""
import os
import shutil
from pathlib import Path

from . import log

SKILL_SOURCE = Path("/usr/local/share/wonton-agent/skills")
PROMPT_SOURCE = Path("/usr/local/share/wonton-agent/prompt.md")


def install_skills(config: dict) -> None:
    section = config.get("skills")
    if not section:
        return
    target = Path(os.path.expanduser(section["target"]))
    for source in SKILL_SOURCE.rglob("*"):
        if not source.is_file():
            continue
        destination = target / source.relative_to(SKILL_SOURCE)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    log(f"skill 装到 {target}")


def install_prompt(config: dict) -> None:
    section = config.get("prompt")
    if not section:
        return
    target = Path(os.path.expanduser(section["target"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROMPT_SOURCE, target)
    log(f"系统提示装到 {target}")
