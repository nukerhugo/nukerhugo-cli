# Nukerhugo

Monorepo for the Nukerhugo command-line tools. Standard library only.

| Package | What | License |
|---|---|---|
| [`packages/nukerhugo`](packages/nukerhugo) | `nukerhugo` CLI: login, usage, config, runs other tools | NSL v1.1 |
| [`packages/nukerhugo-code`](packages/nukerhugo-code) | Nukerhugo Code, the terminal coding agent | NSL v1.2 |

```
pip install nukerhugo-code
nukerhugo code
```

Develop: `python tests/test_all.py && python tests/test_core.py` (Python 3.9+, no dependencies).

