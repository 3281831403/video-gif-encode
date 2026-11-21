"""让 pytest 始终能 import 仓库根目录下的 gifenc 包。"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
