"""演示/测试用的进程入口：把项目根加入 sys.path 后委托给 CLI。

存在的意义：``python -I`` 隔离模式下既不读 PYTHONPATH 也不把 cwd 加进
sys.path，而 Git Bash 对 ``python -c "..."`` 的引号转义不稳定；用一个
实体文件作为入口最可靠。功能与 ``python -m solution`` 完全一致。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solution.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
