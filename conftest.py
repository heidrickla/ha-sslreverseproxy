"""Puts the repository root on sys.path for the custom_components import."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
