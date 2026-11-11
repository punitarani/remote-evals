#!/bin/bash
set -euo pipefail
python /tests/verify.py /workspace/report.json /logs/verifier
