"""Offline seeded-failure benchmark for the investigation engine and a single-call baseline.

Investigator input (``data/inputs``), ground-truth labels (``data/labels``) and scripted fake
model responses (``data/fake``) live in separate files, types and modules. Labels are only read
by scoring and never reach the investigator or the provider.
"""

from pathlib import Path

BENCHMARK_VERSION = "benchmark-v2"
DEFAULT_DATA_DIR = Path(__file__).parent / "data"
