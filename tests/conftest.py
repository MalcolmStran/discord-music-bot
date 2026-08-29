"""Make `bot` importable when pytest is run from anywhere in the checkout."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
