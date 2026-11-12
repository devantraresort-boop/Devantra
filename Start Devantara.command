#!/bin/bash
# Run this file from Finder to start the local development website.
cd -- "$(dirname -- "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "The project's Python environment is missing. Follow the local setup in README.md."
  read -r -p "Press Enter to close. "
  exit 1
fi

.venv/bin/python scripts/start_local.py
result=$?
if [ "$result" -ne 0 ]; then
  echo
  echo "The app could not start. Copy the error above so we can fix it."
  read -r -p "Press Enter to close. "
fi
exit "$result"
