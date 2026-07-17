#!/usr/bin/env bash
# Shim: the implementation moved to factory/af/. This preserves the hook that agents'
# settings files invoke by absolute path. See SKILL.md. A non-executable hook fails
# SILENTLY (Claude Code runs the tool anyway) — the exact fail-open this system warns
# about — so this file must stay chmod +x.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$here/..${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m af.hooks delegate-progress "$@"
