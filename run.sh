#!/bin/bash
cd /home/seruji/Projects/screenscore
export PYTHONPATH="$PWD:$PYTHONPATH"
exec .venv/bin/python -m uvicorn main:adk_app --host 0.0.0.0 --port 8000