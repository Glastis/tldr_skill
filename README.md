# tldr

**Short, hash-checked summaries of every file, folder and function of a repository, so coding agents read the summary instead of the source.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)

## What it does

tldr is an agent skill, in Claude Code's `SKILL.md` format, that keeps a `tldr/` folder mirroring your repository:

```text
tldr/
├── _index.tldr        the whole project: its modules and how they depend on each other
└── shop/
    ├── _dir.tldr      what the folder is for, its key files and classes
    └── cart.py.tldr   what the file does, one entry per function, with its calls
```

The agent's LLM writes the summaries. A small deterministic tool, `scripts/tldr.py`, does everything else, without knowing any programming language or calling an LLM. It hashes every file to flag missing or stale summaries, stamps their headers, and answers call-graph queries (`callers`, `callees`, `impact`, `path`) from the summaries alone. Any file type works, images and binaries included.

## Install

```bash
git clone https://github.com/Glastis/tldr_skill ~/.claude/skills/tldr
```

It needs Python 3.11 or newer, and nothing else. Then, in any repository, ask your agent to *generate the tldr summaries*. Later sessions read `tldr/` before opening the sources. `tldr check` exits with an error when a summary is missing or stale, so it fits a CI job or a pre-commit hook:

```bash
python3 ~/.claude/skills/tldr/scripts/tldr.py check
```
