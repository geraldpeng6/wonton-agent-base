#!/usr/bin/env python3
"""Wonton 的硬规矩：源码文件 ≤ 800 行，函数 ≤ 100 行。超了逐条列出并退出 1。

扫描仓库里所有 .py 和入口脚本 wonton-agent；CI 和发布前都先跑它。
"""
import ast
import sys
from pathlib import Path

MAX_FILE_LINES = 800
MAX_FUNCTION_LINES = 100
ROOT = Path(__file__).resolve().parent.parent


def targets() -> list[Path]:
    files = [path for path in sorted(ROOT.rglob("*.py"))
             if not {".git", "__pycache__"} & set(path.parts)]
    return files + [ROOT / "wonton-agent"]


def check_file(path: Path) -> list[str]:
    text = path.read_text()
    problems = []
    line_count = len(text.splitlines())
    if line_count > MAX_FILE_LINES:
        problems.append(f"{path.relative_to(ROOT)}：{line_count} 行（上限 {MAX_FILE_LINES}）")
    tree = ast.parse(text, filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        length = node.end_lineno - node.lineno + 1
        if length > MAX_FUNCTION_LINES:
            problems.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.name}：{length} 行"
                            f"（上限 {MAX_FUNCTION_LINES}）")
    return problems


def main() -> int:
    problems = [problem for path in targets() for problem in check_file(path)]
    for problem in problems:
        print(problem)
    if problems:
        print(f"共 {len(problems)} 处超长")
        return 1
    print("文件和函数长度都在上限内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
