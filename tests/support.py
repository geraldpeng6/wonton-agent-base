"""被测代码在 wonton_agent/ 包里：把仓库根插进 sys.path，测试按模块取要测的名字。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
