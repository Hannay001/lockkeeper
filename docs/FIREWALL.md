# The Lockkeeper firewall

Skills, plugins and MCP configs are text your AI agent reads and follows. A skill
copied from a public repository can carry instructions you never see: text that
tells the agent to disregard you, commands that send your keys to someone else's
server, or code that decodes and runs a hidden payload. `lockkeeper audit` checks
them before your agent does, and `lockkeeper hook` checks live tool calls before
they run.

Everything here is dependency-free and runs locally.

## Audit a skill, plugin or config

```sh
lockkeeper audit ~/Downloads/some-skill --recursive --strict
```

It detects:

- text that tries to override the agent's instructions,
- commands that send secrets or files to the network, and reads of credential stores,
- code that decodes and runs a hidden payload,
- destructive commands,
- hidden directive comments, and invisible or look-alike Unicode characters.

It scans Markdown, JSON, TOML, YAML and common script and config types (shell,
PowerShell, batch, Python, JavaScript/TypeScript, Ruby, `.env`, Makefiles, and any
file with a shebang). Bytecode and binaries (`.pyc`, `.so`, `.dll`, `.wasm`) can't be
read as text, so they are hashed and never audit clean. A symlink that points outside
the audited folder is flagged, because its target was never scanned.

### Verdicts and exit codes

| Verdict | Meaning | Exit code with `--strict` |
|---|---|:-:|
| `clean` | nothing found | 0 |
| `suspect` | medium-severity findings: review before trusting | 1 |
| `hostile` | high or critical findings: do not install | 2 |

Use the exit code to gate installs and CI. `--json` prints every finding, each tagged
with its SkillTrustBench taxonomy
category (T01–T09) so results are comparable across skill-security tools.

### Optional checks

- `--check-deps` checks pinned dependencies (`requirements.txt`, `package.json`, ...)
  against [osv.dev](https://osv.dev) for known vulnerabilities.
- `--llm-scan` adds a second-pass review by an OpenAI-compatible model. It is opt-in
  twice (the flag plus environment variables), and the offline scanner never depends
  on it.

## Signed receipts

Prove what was scanned, and that the evidence wasn't altered afterwards:

```sh
lockkeeper audit ~/skills/some-skill --recursive --strict \
  --receipt-out receipt.json --receipt-key key.hex
lockkeeper audit --verify-receipt receipt.json --receipt-key key.hex   # exit 0 valid, 1 tampered
```

Receipts are HMAC-SHA256 signed and bind to the requested targets and the paths
actually scanned. Keep them as CI artifacts or audit trails.

## Block hostile tool calls live

Register the firewall as a Claude Code hook and hostile tool calls are blocked before
they run:

```sh
lockkeeper hooks install claude --firewall
```

It scans the values a tool will actually execute (not their JSON-escaped form), so
tabs and quotes can't hide a command. It fails closed on oversized input and on any
high or critical finding. Medium-only (`suspect`) traffic is allowed with a warning,
so low-confidence signals don't block your work.

Any harness that passes a tool call as JSON on stdin can call `lockkeeper hook`
directly: exit code 2 blocks the call and the reason goes to stderr.

## Reporting a bypass

Found a way past the scanner? Please report it privately per
[SECURITY.md](../SECURITY.md).
