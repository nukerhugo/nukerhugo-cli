# nukerhugo

The Nukerhugo command line. Standard library only, Python 3.9+.

```
pip install nukerhugo           # the CLI
pip install nukerhugo-code      # the coding agent, optional
nukerhugo login                 # free key, paste a key, or custom endpoint
nukerhugo usage                 # tokens used / left today
nukerhugo code                  # starts nukerhugo-code
```

| Command | What it does |
|---|---|
| `nukerhugo login` / `logout` | set up or remove the saved API key |
| `nukerhugo usage` | tokens used and left today |
| `nukerhugo config` | show shared settings (key masked); `config path` prints the file |
| `nukerhugo list` | show installed tools |
| `nukerhugo <tool> ...` | run the program `nukerhugo-<tool> ...` |

Tools plug in git-style: any program named `nukerhugo-<name>` on your PATH (or installed in
the same Python environment as a `nukerhugo_<name>` module) becomes `nukerhugo <name>`.

Settings: `~/.nukerhugo/config.json` (override the folder with `NUKERHUGO_HOME`). Account
settings (base URL, key) are shared by all tools; each tool keeps its own section.
`NUKERHUGO_API_KEY` / `NUKERHUGO_BASE_URL` override the file and are never written to it.

Note: the key is stored in that file, so any program you run as your user can read it, and
so can tools you run through `nukerhugo`. Only install tools you trust.

Licensed under the Nukerhugo Source License v1.1 (see LICENSE): free for any use, including
commercial; no public redistribution or reselling of the software itself.
