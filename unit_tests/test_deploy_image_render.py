"""Run release helpers with cloud commands mocked, including a no-op image."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def bash_path():
    if os.name != 'nt':
        return shutil.which('bash')
    git = shutil.which('git')
    candidate = Path(git).parent.parent / 'bin/bash.exe' if git else None
    return str(candidate) if candidate and candidate.is_file() else None


BASH = bash_path()
DRIVER = '''set -euo pipefail
az() { echo 35aaf788-f4cf-40c3-9ea5-bc8fe8708a85; }
kubectl() {
  case "$*" in
    'config current-context') echo trafficxx-lab-aks ;;
    'set image '*) return 0 ;; # Old helper emitted nothing for the same digest.
    'patch --local '*)
      printf '%s\\n' "$@" > "$PROBE_FOLDER/patch.args"
      case "$RENDER_MODE" in empty) return 0 ;; fail) return 7 ;; esac
      printf '{"kind":"Deployment","metadata":{"name":"%s"},"image":"%s"}\\n' "$API_NAME" "$REFERENCE"
      ;;
    'apply -f -') cat > "$PROBE_FOLDER/applied.json" ;;
    'apply -f '*|'rollout status '*|'get pods '*) return 0 ;;
    *) echo 'Unexpected command in mocked release helper' >&2; return 9 ;;
  esac
}
export -f az kubectl
bash "$PROBE_FOLDER/scripts/deploy-$API_NAME.sh"
'''


@unittest.skipUnless(BASH, 'Bash is required for release-helper regression checks')
class DeployImageRender(unittest.TestCase):
    def run_helper(self, name, reference, mode='normal'):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'scripts').mkdir()
            shutil.copyfile(ROOT / ('scripts/deploy-' + name + '.sh'),
                            root / ('scripts/deploy-' + name + '.sh'))
            (root / (name + '-image.txt')).write_text(reference + '\n')
            (root / 'driver.sh').write_text(DRIVER, newline='\n')
            environment = dict(os.environ, PROBE_FOLDER=root.as_posix(), API_NAME=name,
                               REFERENCE=reference, RENDER_MODE=mode)
            result = subprocess.run([BASH, (root / 'driver.sh').as_posix()], env=environment,
                                    capture_output=True, text=True, timeout=15)
            applied = (root / 'applied.json').read_text() if (root / 'applied.json').exists() else None
            arguments = (root / 'patch.args').read_text().splitlines() if (root / 'patch.args').exists() else []
            return result.returncode, applied, arguments

    def test_same_and_changed_digest_emit_complete_manifest(self):
        for name in ('booking', 'browse'):
            manifest = (ROOT / ('kubernetes/apps/' + name + '-deployment.yaml')).read_text()
            current = re.search(r'^\s*image: (\S+)', manifest, re.MULTILINE).group(1)
            if '@sha256:' not in current:
                # Browse currently has a build-required placeholder; also cover
                # a future source revision that pins an already-reviewed image.
                current = 'trafficxxacre8708a85.azurecr.io/trafficxx@sha256:' + 'b' * 64
            changed = 'trafficxxacre8708a85.azurecr.io/trafficxx@sha256:' + 'a' * 64
            for reference in (current, changed):
                with self.subTest(name=name, unchanged=reference == current):
                    status, applied, arguments = self.run_helper(name, reference)
                    self.assertEqual(status, 0)
                    self.assertEqual(json.loads(applied)['image'], reference)
                    patch = json.loads(arguments[arguments.index('--patch') + 1])
                    self.assertEqual(patch, [
                        {'op': 'test', 'path': '/spec/template/spec/containers/0/name', 'value': name},
                        {'op': 'replace', 'path': '/spec/template/spec/containers/0/image', 'value': reference}])

    def test_empty_or_failed_render_never_reaches_apply(self):
        reference = 'trafficxxacre8708a85.azurecr.io/trafficxx@sha256:' + 'a' * 64
        for name in ('booking', 'browse'):
            for mode in ('empty', 'fail'):
                with self.subTest(name=name, mode=mode):
                    status, applied, arguments = self.run_helper(name, reference, mode)
                    self.assertNotEqual(status, 0)
                    self.assertIsNone(applied)
                    self.assertTrue(arguments)

    def test_invalid_digest_stops_before_render_or_apply(self):
        for name in ('booking', 'browse'):
            with self.subTest(name=name):
                status, applied, arguments = self.run_helper(name, 'invalid-image')
                self.assertNotEqual(status, 0)
                self.assertIsNone(applied)
                self.assertEqual(arguments, [])
