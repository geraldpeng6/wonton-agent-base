"""被测的 wonton-agent 是无后缀脚本，用 SourceFileLoader 装进来给各测试共享。"""
import importlib.machinery
import importlib.util
from pathlib import Path


def load_agent_module():
    script = Path(__file__).resolve().parent.parent / "wonton-agent"
    loader = importlib.machinery.SourceFileLoader("wonton_agent", str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


agent = load_agent_module()
