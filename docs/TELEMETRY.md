# Telemetry

Lockkeeper is a local security tool, so telemetry is **off by default** and stays
off unless you turn it on. If you do, you help us see which features people use,
how fast routing is on real machines, and how many people use Lockkeeper at all.

```sh
lockkeeper telemetry            # status: on or off, and where data would go
lockkeeper telemetry on         # share anonymous daily usage counts
lockkeeper telemetry show       # print exactly what would be sent
lockkeeper telemetry off        # stop, and delete the local summary and install id
```

## What is collected

One summary per day, never individual events:

| Field | Example | Why |
|---|---|---|
| `install_id` | `894789660a67…` | A random id (uuid4) created when you opt in, so daily summaries count distinct installs. It is not derived from your machine, user name or anything else, and `telemetry off` deletes it. |
| `version`, `python`, `os` | `1.2.0`, `3.12`, `darwin` | Which versions and platforms to support. |
| `commands` | `route: 42 runs, 0 failed, latency buckets` | Which features are used, how often they fail, and how fast they are. Latency is bucketed (`<100ms`, `100-300ms`, `300ms-1s`, `1-3s`, `3-10s`, `10s+`). |
| `runtimes` | `claude: 40, codex: 2` | Which agents Lockkeeper routes for. Unknown values are reported as `other`. |
| `registry_size` | `1k-10k` | How large libraries are, as a bucket (`<100`, `100-1k`, `1k-10k`, `10k-100k`, `100k+`). |

Commands Lockkeeper doesn't know are reported as `other`, so nothing you type as a
subcommand is passed through.

## What is never collected

Prompts or task text, capability names, file paths, host names, user names, IP
addresses in the payload, environment variables, config contents, or error
messages. The collector necessarily sees the sending IP address, like any web
request; the payload itself never includes it.

## When anything is sent

- Only while telemetry is on, **and** an endpoint is configured. The project does
  not ship one yet: until it does, telemetry that is on only keeps the local
  summary that `lockkeeper telemetry show` prints.
- Complete days are sent once, with a 2-second timeout, during maintenance
  commands (`rebuild`, `doctor`, `check`, `init`, `reindex`, `snapshot-runtimes`)
  or `lockkeeper telemetry flush`. Never while an agent waits on `route`,
  `search` or the live `hook`.
- Failures are silent and never affect the command. At most 14 unsent days are
  kept.
- Endpoints must use `https://` (loopback addresses excepted, for testing).

## Always off when

- `DO_NOT_TRACK=1` is set ([consoledonottrack.com](https://consoledonottrack.com)),
- `LOCKKEEPER_TELEMETRY=0` (or `off`) is set,
- a CI environment is detected (`CI` is set).

These override `telemetry on`.

## Where it is stored

- Setting and install id: `~/.config/lockkeeper/telemetry.json` (`$XDG_CONFIG_HOME` respected).
- Unsent daily summaries: `~/.local/state/lockkeeper/telemetry-pending.json` (`$XDG_STATE_HOME` respected).

## For maintainers: pointing it at a collector

Set a default endpoint in `scripts/telemetry.py` (`DEFAULT_ENDPOINT`) or per
machine with `LOCKKEEPER_TELEMETRY_ENDPOINT` / `lockkeeper telemetry on --endpoint URL`.
The collector receives one JSON object per day per install, via `POST` with
`Content-Type: application/json`, and should answer 2xx. The schema is exactly what
`lockkeeper telemetry show` prints (`schema: 1`).

Adoption can also be measured without any telemetry: PyPI download counts
(`pypistats`, or the public PyPI BigQuery dataset), GitHub stars, clones and traffic
(the repository's *Insights → Traffic* page), and release download counts.
