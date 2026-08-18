"""Run every check. Called by `wallcal.sh selftest`."""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
suites = sorted(p for p in HERE.glob("test_*.py"))
failed = []
for suite in suites:
    print(f"\n=== {suite.name} " + "=" * (52 - len(suite.name)))
    if subprocess.run([sys.executable, str(suite)]).returncode:
        failed.append(suite.name)

print("\n" + ("=" * 60))
print("ALL SUITES PASS" if not failed else f"FAILED: {', '.join(failed)}")
sys.exit(1 if failed else 0)
