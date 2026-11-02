import sys
import warnings
from pathlib import Path

# Tests import their doubles as `mt_fakes` (pytest runs with --import-mode=importlib).
sys.path.insert(0, str(Path(__file__).parent))
warnings.filterwarnings("ignore", message=r"\[EXPERIMENTAL\]")
