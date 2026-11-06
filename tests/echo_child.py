"""测试用的 ACP 子进程替身：echo 模式先报 pid 再按行回显 stdin；hold 模式写 pid 后挂住，
用来测“连接断了杀掉子进程”。"""
import os
import sys
import time

if sys.argv[1] == "hold":
    with open(sys.argv[2], "w") as pidfile:
        pidfile.write(str(os.getpid()))
    time.sleep(3600)
else:
    sys.stdout.write(f"pid:{os.getpid()}\n")
    sys.stdout.flush()
    for line in sys.stdin.buffer:
        sys.stdout.buffer.write(line)
        sys.stdout.flush()
