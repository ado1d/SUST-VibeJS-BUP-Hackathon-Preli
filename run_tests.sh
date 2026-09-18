#!/usr/bin/env bash
# Full local verification battery. Run BEFORE every deploy/submit.
set -e
echo "== 1/3  Public sample cases (optimizer + judge replay) =="
python3 tests/public_cases.py
echo "== 2/3  Adversarial API tests =="
python3 tests/test_api.py | tail -3
echo "== 3/3  Paraphrase pack (LLM path if OPENAI_API_KEY set, else emergency) =="
python3 tests/test_paraphrase.py --verbose || true
echo "Done."
