#!/bin/bash
trap "kill %1 2>/dev/null; exit" EXIT INT TERM
cd "$(dirname "$0")"
source venv/bin/activate
open http://127.0.0.1:5050
python app.py &
wait
