# SPDX-License-Identifier: MIT
"""Run the tests. Needs numpy, nothing else: python3 tests/run_all.py

None of them need Maya: scene building runs against a fake maya module, and the
protocol runs against a mock Nomad. The real thing still needs a smoke test in
Maya -- see README.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = ["test_link.py", "test_scene.py"]

environment = dict(os.environ)
paths = [os.path.join(HERE, "..", "python")]
if environment.get("PYTHONPATH"):
    paths.append(environment["PYTHONPATH"])
environment["PYTHONPATH"] = os.pathsep.join(paths)

failed = []
for name in MODULES:
    print("=" * 60)
    print(name)
    print("=" * 60)
    if subprocess.call([sys.executable, os.path.join(HERE, name)], env=environment) != 0:
        failed.append(name)

print("\n%d/%d modules passed" % (len(MODULES) - len(failed), len(MODULES)))
sys.exit(1 if failed else 0)
