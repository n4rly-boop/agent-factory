"""agent-factory core.

The Python half of the factory, landing alongside the bash it will replace. Both write the
same files on disk, so every format here is fixed by what ai.sh/mail.sh already do — see
paths.py (where things live), patterns.py (how the TUI is read) and mailbox.py (the wire
format and its lock).

Stdlib only, on purpose: this code runs inside agents' panes and in a warden loop that must
survive a machine with no venv activated.
"""

__all__ = ["mailbox", "paths", "patterns", "probe", "spec", "tmux"]
