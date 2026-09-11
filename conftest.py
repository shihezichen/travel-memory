"""根 conftest —— 只负责让 `import app / db / media / domain.*` 在任意工作目录下可用。

真正的隔离逻辑和 fixture 都在 tests/conftest.py（包括「不许碰生产 data.db /
uploads」的保险丝）。这里保持极简，避免两个 conftest 互相 import 造成模块名歧义。
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 测试默认按「媒体走本机 /uploads/」跑，不受部署配置影响。
# config.py 会去读仓库里的 media_base.txt —— 那是**生产**配置（指向 OSS），
# 一旦线上开了 CDN，所有断言 /uploads/... 的测试就会集体变红，而代码其实没坏。
# 这里必须在 import config 之前把环境变量钉成空串（setdefault 保留显式覆盖，
# 想测 CDN 行为的用例自己 setenv 就好）。
os.environ.setdefault("TRAVEL_MEDIA_BASE_URL", "")
