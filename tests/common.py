"""Synthetic-only fixtures. No real key, network or user installation."""
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


fx = module('fx_tests_producer', ROOT / 'update-fx.py')
manager = module('fx_tests_manager', ROOT / 'scripts/manage_install.py')
publisher = module('fx_tests_publisher', ROOT / 'publish_github.py')
FIXTURE = json.loads((ROOT / 'tests/fixtures/quote_synthetic.json').read_text())
NOW = FIXTURE['payload']['last_quote_at'] + 60
