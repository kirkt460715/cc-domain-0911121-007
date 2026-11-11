#!/bin/sh
set -eu
python -m compileall -q meterbook demo_support
python -m unittest discover -s tests -v
python -m meterbook replay fixtures/settlement.json
python demo_support/check.py
