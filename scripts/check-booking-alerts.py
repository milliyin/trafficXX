"""Read-only proof of installed Booking rules and fresh stored pool samples."""
import importlib.util
from pathlib import Path


if __name__ == '__main__':
    spec = importlib.util.spec_from_file_location(
        'pool_alerts', Path(__file__).with_name('check-browse-alerts.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raise SystemExit(module.main(component='booking'))
