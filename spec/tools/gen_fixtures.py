#!/usr/bin/env python3
# pyright: strict
"""Regenerate every case under spec/conformance/cases/.

    python3 spec/tools/gen_fixtures.py           # rewrite the cases
    python3 spec/tools/gen_fixtures.py --check   # regenerate in a temp dir; fail on any byte diff

Stdlib only, Python 3.12+. The builders live in the fixtures/ package (one module per case
family, canonical JSON in fixtures/jcs.py, the main logic in fixtures/__main__.py). This file
runs that package, so relative imports resolve the same way for Python and for pyright.
Hashes, chains and artifacts are computed, never typed by hand.
"""

import runpy

runpy.run_module("fixtures", run_name="__main__", alter_sys=True)
