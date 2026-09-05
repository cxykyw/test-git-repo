#!/usr/bin/env bash
set -euo pipefail

python3 - <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone

def load(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

raw_semgrep = load('scan/semgrep.json')
raw_gitleaks = load('scan/gitleaks.json')
raw_trivy = load('scan/trivy.json')

skipped = [name for name, v in (('semgrep', raw_semgrep), ('gitleaks', raw_gitleaks), ('trivy', raw_trivy)) if v is None]
semgrep = raw_semgrep if isinstance(raw_semgrep, dict) else {}
gitleaks = raw_gitleaks if isinstance(raw_gitleaks, list) else []
trivy = raw_trivy if isinstance(raw_trivy, dict) else {}

sast_errors = [r for r in semgrep.get('results', []) if r.get('extra', {}).get('severity') == 'ERROR']
sast_warnings = [r for r in semgrep.get('results', []) if r.get('extra', {}).get('severity') != 'ERROR']
trivy_critical = sum(
    1 for r in trivy.get('Results', [])
    for v in (r.get('Vulnerabilities') or [])
    if v.get('Severity') == 'CRITICAL'
)

summary = {
    'semgrep_errors': len(sast_errors),
    'semgrep_warnings': len(sast_warnings),
    'gitleaks_secrets': len(gitleaks),
    'trivy_critical_vulns': trivy_critical,
    'skipped_scanners': skipped,
}

failures = []
if gitleaks:
    failures.append(f"gitleaks: {len(gitleaks)} secret(s) detected")
    for s in gitleaks[:5]:
        failures.append(f"  - {s.get('File')}:{s.get('StartLine')} rule={s.get('RuleID')}")
if sast_errors:
    failures.append(f"semgrep: {len(sast_errors)} ERROR-level finding(s)")
    for r in sast_errors[:5]:
        failures.append(f"  - {r.get('path')}:{r.get('start', {}).get('line')} rule={r.get('check_id')}")
if trivy_critical:
    failures.append(f"trivy: {trivy_critical} CRITICAL vulnerability(ies)")
for s in skipped:
    failures.append(f"warning: scanner '{s}' did not produce a report (skipped)")

verdict = 'FAIL' if any(not f.startswith('warning:') for f in failures) else 'PASS'
report = {
    'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    'quality_gate': {**summary, 'verdict': verdict, 'failures': failures},
    'ai_review': None,
}
with open('report.json', 'w') as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

print('gate summary:', json.dumps(summary, ensure_ascii=False))
if failures:
    print('QUALITY GATE:', verdict)
    print('\n'.join(failures))
    if verdict == 'FAIL':
        sys.exit(1)
else:
    print('QUALITY GATE: PASS')
PYEOF
