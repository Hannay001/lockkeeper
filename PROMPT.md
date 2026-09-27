# Install lockkeeper with one prompt

Copy the block below and paste it into **any AI coding agent you already have**
(Claude Code, Codex, Cursor, OpenCode, Gemini CLI, Jcode, ...). The agent will
install lockkeeper from PyPI, connect it to every agent it finds on your machine, and report back
in plain language. You don't need to know anything about terminals.

---

```text
Install the tool "lockkeeper" (https://pypi.org/project/lockkeeper/) on this
machine, then verify it works.

Do exactly these steps:

1. Check Python 3.11+ is available (python3 --version, or py --version on
   Windows). If not, stop and tell me.
2. Install it from PyPI. Prefer pipx if it is available:
   pipx install lockkeeper
   Otherwise:
   python3 -m pip install --user lockkeeper
3. Connect it to every AI coding agent installed on this machine (Claude Code,
   Codex, Cursor, Jcode, Hermes, OpenCode, Gemini, or anything else):
   lockkeeper init
4. Build the capability index:
   lockkeeper snapshot-runtimes
   lockkeeper rebuild
5. Run the health check:
   lockkeeper doctor

Then report back to me in plain language:
- which AI agents you found on this machine and how many skills each has,
- the total number of capabilities indexed,
- one example of a task I could route, using:
   lockkeeper route --stdin <<'TASK'
   fix a failing test in my project
   TASK

If any step fails, show me the error and suggest the simplest fix. Do not
change anything else on this machine.
```

---

After this, `lockkeeper` is on your PATH and bound to every agent on your machine.
Ask your agent: *"run lockkeeper doctor"* any time you want to see the current state.

Prefer to choose which agents get connected? Tell your agent instead:
*"run lockkeeper init --runtimes claude,codex so only those two get connected."*
