#!/usr/bin/env bash
# Run the backend test suite. Pure stdlib Python 3, no dependencies.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec python3 -m unittest discover -s tests -p 'test_*.py' -v
