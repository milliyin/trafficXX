"""Catch Windows line endings that make Bash fail before a helper can run."""
from pathlib import Path
import unittest


class ShellLineEndings(unittest.TestCase):
    def test_shipped_shell_helpers_use_lf(self):
        root = Path(__file__).resolve().parents[1]
        paths = [path for folder in ('scripts', 'ci')
                 for path in (root / folder).rglob('*.sh')]
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(script=str(path.relative_to(root))):
                self.assertNotIn(b'\r', path.read_bytes(),
                                 'Bash helpers must have LF line endings before packaging')
