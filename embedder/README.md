# Semantic sidecar

Runs in its own pinned venv (python3.13 -- onnxruntime has no reliable cp314 wheel, and
system python 3.14 is PEP-668 externally-managed, so an in-process import is impossible).

    python3.13 -m venv ~/.agents/capabilities/embedder/.venv
    ~/.agents/capabilities/embedder/.venv/bin/pip install -r embedder/requirements.txt

Build the index:

    lockkeeper reindex

For source installs, Lockkeeper atomically refreshes the generated `embed.py` from the
checked-out repository before every query or reindex. If a sandbox blocks that shared-file
write, it executes the current repository copy directly instead of falling back to stale code.

`rebuild` and query auto-heal keep the index fingerprint current. Index files are written
atomically, so an interrupted reindex never leaves torn vectors behind. A multi-intent route
embeds the task and all its intents in one sidecar call (`query --batch`). The router degrades to
lexical-only, with an operator-visible warning, if the sidecar is missing, stale, crashed, or
hanging.
