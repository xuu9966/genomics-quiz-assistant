"""QA 测试包；确保从任意工作目录发现用例时都能导入项目根模块。"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS = os.path.dirname(os.path.abspath(__file__))
for _path in (_ROOT, _TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)
