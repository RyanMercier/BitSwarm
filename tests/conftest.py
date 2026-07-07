"""
Shared test configuration.

Unit tests always exercise host-mode gate execution: sandbox behavior
is covered by dedicated command-construction tests in
test_sandbox.py, not by spinning containers inside the suite.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BITSWARM_SANDBOX", "off")
