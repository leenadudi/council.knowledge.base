#!/bin/bash
cd "$(dirname "$0")"
kill $(lsof -ti :5001) 2>/dev/null
python3 app.py
