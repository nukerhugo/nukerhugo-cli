# Nukerhugo Code

A terminal coding agent for the Nukerhugo AI Enterprise API. Pure Python standard
library: no packages to install.

```
$ nukerhugo code
  Nukerhugo Code v0.1.0
  deepseek-v4-flash · /home/you/project
  session 0/40k · 100k left today · resets 02:00
> add a --verbose flag to cli.py
● Read(cli.py)
  ⎿ 84 lines
● Edit(cli.py)
    -    p.add_argument("--quiet")
    +    p.add_argument("--quiet")
    +    p.add_argument("--verbose", action="store_true")
  Allow? [y]es / [n]o / [a]lways this session:
```

## Install and run

Needs Python 3.9+ (developed and tested on 3.12 and 3.13).

```
pip install nukerhugo-code      # also installs the `nukerhugo` CLI it depends on
nukerhugo-code                  # or: nukerhugo code
```

On first run, with no key configured, you choose:

1. **Get a free Nukerhugo key** - enter your email, the key is created and saved for you
   (100,000 tokens/day, shown only once).
2. **I already have a key** - paste it (input is hidden).
3. **Different endpoint** - any OpenAI-compatible base URL (Ollama, LM Studio, ...).

The key is shared with all Nukerhugo tools (`nukerhugo login` does the same setup).
Settings live in `~/.nukerhugo/config.json` (mode 600). Re-run setup with `nukerhugo-code --setup`.

## Settings

Precedence: flags > environment > config file > defaults.

| Setting | Flag | Environment |
|---|---|---|
| Base URL | `--base-url` | `NUKERHUGO_BASE_URL` |
| API key | `--api-key` | `NUKERHUGO_API_KEY` |
| Model | `--model` | `NUKERHUGO_MODEL` |

A key from the environment or a flag is never written to disk. `NUKERHUGO_HOME`
moves the config directory.

## Using it

- `nukerhugo-code` (or `nukerhugo code`) starts the interactive session in the current directory.
- `nukerhugo-code "explain main.py"` runs one prompt and exits.
- Ctrl+C interrupts the current reply; Ctrl+C twice or Ctrl+D exits. End a line with `\` for multi-line input.
- Put standing instructions in `NUKERHUGO.md` in the project root.

| Command | What it does |
|---|---|
| `/usage` `/cost` | tokens used / left today (from the server), session total |
| `/limit` | show or change limits: `daily 90%`, `session 60000`, `rounds 40`, `off`, `on` |
| `/model [name]` | list models or switch |
| `/compact` | shrink old tool output in the history (free) |
| `/clear` | forget the conversation |
| `/thinking` | show or hide the model's reasoning |
| `/yolo` | toggle permission prompts |
| `/config` | show settings (key masked) |

## Tools and permissions

`read_file`, `write_file`, `edit_file`, `glob`, `grep`, `bash`. Reads and searches run
freely inside the project. Writes, edits and commands ask first and show a diff or the
command; `a` allows that kind of action for the rest of the session (for commands, per
program name). Paths outside the project always ask. Catastrophic commands (`rm -rf /`,
`mkfs`, ...) ask even with `--yolo`.

## Usage limits

The free key has a hard cap of 100,000 tokens/day (resets 00:00 UTC), enforced by the server.
On top of that the client keeps overridable soft limits, because an agent resends its history
every turn and can burn through a budget fast:

| Limit | Default | When hit |
|---|---|---|
| Daily soft limit | 80% of budget | pause and ask |
| Session limit | 40,000 tokens | pause and ask |
| Tool rounds per turn | 25 | pause and ask |
| Low reserve | under 2,000 tokens left | warn (stays on even with limits off) |

At a pause you can continue this turn, raise the limit, disable limits, or stop.
Warnings show at 50% and 80% of the daily budget. Old tool output is trimmed automatically
once the history passes about 12,000 tokens (`history_budget`).

## Windows

The `bash` tool runs **PowerShell** on Windows (and bash elsewhere); the model is told which
shell it is using. Override with `"shell": "bash"` (e.g. Git Bash) in the `code` section of the config. Settings are stored in `C:\Users\<you>\.nukerhugo\config.json`, protected
by your user profile's normal folder permissions.

### "certificate has expired" / CERTIFICATE_VERIFY_FAILED

If it happens, Nukerhugo retries once with a context that skips expired certificates
(verification stays on), so you normally will not see this. Windows' own tools accept the site but Python rejects it. This is a known Windows + Python
problem caused by expired certificates cached in the Windows certificate store (for example an
old cross-signed Let's Encrypt intermediate such as the expired `ISRG Root X2`, or expired
`R3` / `DST Root CA X3`). Rebooting or running Windows Update often clears it. Otherwise list
the expired certificates and remove the ones used for HTTPS, after exporting a backup:

```
Get-ChildItem Cert:\CurrentUser\CA, Cert:\LocalMachine\CA | Where-Object NotAfter -lt (Get-Date) |
  Select-Object @{n='Store';e={$_.PSParentPath -replace '.*::',''}}, Subject, NotAfter | Format-List
```

Never turn certificate checking off: the API key travels over this connection.

## Tool calling

The client sends OpenAI-style `tools`. If the endpoint rejects them, it switches to a
text protocol (fenced `tool` blocks) automatically; force it with `--text-tools`.

## License

Nukerhugo Source License v1.2 (see LICENSE): free to use with the Nukerhugo AI Service,
personally or inside your organization, and to modify privately; no public redistribution,
no commercial use of the software itself, no rebranded forks.
