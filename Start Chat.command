#!/bin/zsh
# Double-click to open the chat window. Close this Terminal window to stop it.
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python app.py
