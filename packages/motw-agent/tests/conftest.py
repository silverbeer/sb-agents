import sys
from pathlib import Path

# Tests import their doubles as `motw_fakes` (pytest runs with --import-mode=importlib).
sys.path.insert(0, str(Path(__file__).parent))
