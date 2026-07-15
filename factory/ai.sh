#!/usr/bin/env bash
# Shim: the implementation moved to factory/af/. This preserves the CLI that live
# agents and settings files call by path. See SKILL.md.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m af "$@"
