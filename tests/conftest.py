"""让 tests/ 能 import src/ 下的模块（protocol / quality_gate）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
