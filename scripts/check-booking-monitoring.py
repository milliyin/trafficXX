"""Read-only proof of stored metrics from both actual Booking pod targets."""
import importlib.util
from pathlib import Path


if __name__ == '__main__':
    spec = importlib.util.spec_from_file_location(
        'pool_monitoring', Path(__file__).with_name('check-browse-monitoring.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raise SystemExit(module.main(component='booking'))
