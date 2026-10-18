"""Render scanner evidence for GitHub's job summary without hiding unfixed findings."""
import json
from collections import Counter
from pathlib import Path

report_dir = Path('/reports')
report = json.loads((report_dir / 'trivy.json').read_text())
sbom = json.loads((report_dir / 'sbom.cdx.json').read_text())
findings = [item for result in report.get('Results', [])
            for item in result.get('Vulnerabilities') or []]
totals = Counter(item['Severity'] for item in findings)
fixed = Counter(item['Severity'] for item in findings if item.get('FixedVersion'))
high_ids = {item['VulnerabilityID'] for item in findings if item['Severity'] == 'HIGH'}

print('## Runtime image security evidence\n')
print('Gate: block HIGH/CRITICAL findings with an available fix. All findings remain in the JSON artifact.\n')
print('| Severity | Package findings | With a listed fix |')
print('|---|---:|---:|')
for severity in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'UNKNOWN'):
    print(f'| {severity} | {totals[severity]} | {fixed[severity]} |')
print(f'\nDistinct HIGH advisories: **{len(high_ids)}**.')
print(f'SBOM components: **{len(sbom.get("components", []))}**.')
print('\nA passing gate does not mean the image is vulnerability-free. Review unfixed findings before deployment.')
