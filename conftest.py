"""根 conftest —— 只负责让 `import app / db / media / domain.*` 在任意工作目录下可用。

真正的隔离逻辑和 fixture 都在 tests/conftest.py（包括「不许碰生产 data.db /
uploads」的保险丝）。这里保持极简，避免两个 conftest 互相 import 造成模块名歧义。
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
