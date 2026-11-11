"""Answer ./install.sh's questions like a person at a terminal: wait until a
question appears, then type the answer. Used by dev/install-script-test.sh.

    python3 install_driver.py answers.txt -- ./install.sh [--ask-all]

answers.txt: one "question text => answer" per line ("=>" alone: just Enter).
After the last answer the installer keeps running (it starts OpenClave)."""
import os
import pty
import sys
import time

path, cmd = sys.argv[1], sys.argv[sys.argv.index("--") + 1:]
pairs = []
for line in open(path, encoding="utf-8"):
    if "=>" in line:
        q, a = line.rstrip("\n").split("=>", 1)
        pairs.append((q.strip(), a.strip()))
pid, fd = pty.fork()
if pid == 0:
    os.execvp(cmd[0], cmd)
seen = b""
while True:
    try:
        data = os.read(fd, 4096)
    except OSError:
        break
    if not data:
        break
    sys.stdout.buffer.write(data); sys.stdout.flush()
    seen += data
    if pairs and pairs[0][0].encode() in seen:
        q, a = pairs.pop(0)
        time.sleep(0.3)
        os.write(fd, a.encode() + b"\r")
        seen = b""
os.waitpid(pid, 0)
