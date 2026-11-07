"""测试用的登录进程替身（[[login]] 的 command 指到这里，模式由 command 的参数选）：
echo [n]    收一行回一行 got:<行>，收满 n 行或 stdin EOF 后退出 0（测 lines 模式；带上
            无关的请求 args 时配置命令照常跑，正好证明 args 被忽略）；
env NAME    打印环境变量 NAME 的值退出 0（测 env 合并进登录进程）；
eof         读 stdin 到 EOF，打印 eof:<行数>:<首行> 退出 0（测 line_then_eof 关 stdin）；
hold PIDFILE  把 pid 写进 PIDFILE 后挂住（测桥断开时登录进程被收掉）；
exit CODE   以退出码 CODE 退出（测退出码帧和不重启路径）；
prompt      打印 "Paste code here if prompted > "（不带换行）后等一行 stdin，回 got:<行>
            退出 0（测不完整的一行 200ms 无新字节就先发）。
"""
import os
import sys
import time

mode = sys.argv[1]
if mode == "echo":
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    seen = 0
    for line in sys.stdin.buffer:
        sys.stdout.buffer.write(b"got:" + line)
        sys.stdout.buffer.flush()
        seen += 1
        if limit is not None and seen >= limit:
            break
elif mode == "env":
    sys.stdout.write(os.environ.get(sys.argv[2], "unset") + "\n")
elif mode == "eof":
    lines = sys.stdin.buffer.readlines()
    head = lines[0].decode(errors="replace").rstrip("\n") if lines else ""
    sys.stdout.write(f"eof:{len(lines)}:{head}\n")
elif mode == "hold":
    with open(sys.argv[2], "w") as pidfile:
        pidfile.write(str(os.getpid()))
    time.sleep(3600)
elif mode == "exit":
    sys.exit(int(sys.argv[2]))
elif mode == "prompt":
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    line = sys.stdin.buffer.readline()
    sys.stdout.buffer.write(b"got:" + line)
    sys.stdout.buffer.flush()
