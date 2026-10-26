#!/bin/zsh
cd "$(dirname "$0")" || exit 1
python3 -m pitwall.configure --provider gemini
