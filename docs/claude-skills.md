# Claude Code skills environment

This repo ships a shared Claude Code setup in `.claude/`. When you open the repo in Claude Code and trust the folder, Claude Code offers to install the marketplaces and plugins listed in `.claude/settings.json`. Accept, or run the commands below by hand.

## Enabled by default

| Plugin | Source | What it does |
|---|---|---|
| **superpowers** | `superpowers@claude-plugins-official` ([obra/superpowers](https://github.com/obra/superpowers)) | Workflow skills: brainstorming, writing and executing plans, red/green TDD, systematic debugging, verification before completion, code review, git worktrees. |
| **caveman** | `caveman@caveman` ([JuliusBrussee/caveman](https://github.com/JuliusBrussee/caveman)) | Terse output to save tokens. Same rules as the caveman block already in `AGENTS.md` and the Cursor/Cline/Windsurf rule files. `/caveman`, `/ultracave`, `/megacave`; say "normal mode" to stop. |
| **ponytail** | `ponytail@ponytail` ([DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail)) | "Write the least code that works": reuse the codebase, stdlib and installed deps before adding code. Never removes validation or security checks. `/ponytail lite\|full\|ultra`, `/ponytail-review`, `/ponytail-audit`, `/ponytail-debt`. Needs `node` on PATH for its always-on hooks. |
| **security-guidance** | `claude-plugins-official` | Warns on risky edits (injection, secrets, unsafe deserialisation) and reviews the diff when Claude stops. Matters here: the bot holds broker keys and runs a web UI and Telegram bot. |
| **pr-review-toolkit** | `claude-plugins-official` | Review agents for tests, error handling, type design and silent failures. |
| **pyright-lsp** | `claude-plugins-official` | Python type checking and go-to-definition for Claude. Needs `pyright` installed (`npm i -g pyright` or `pip install pyright`). |
| **commit-commands** | `claude-plugins-official` | `/commit`, `/commit-push-pr`. |

## Project skills (in `.claude/skills/`)

| Skill | When Claude uses it |
|---|---|
| `live-money-safety` | Before any change to the real-money path: `live.py`, `broker.py`, `risk.py`, `fees.py`, sizing, kill switch, `TRADING_MODE`. Lists the interlocks to keep and the tests to run. |
| `backtest-discipline` | When building or judging a backtest or shadow experiment: no look-ahead, real fees from `fees.py`, whole shares, survivorship bias, hold-out period, benchmark. |

There was no maintained public backtesting skill worth adding, so these two are written for this codebase.

## Optional (marketplaces are registered, not enabled)

Turn one on with `/plugin install <name>@<marketplace>` or by adding it to `enabledPlugins`.

| Plugin | Why you might want it |
|---|---|
| `claude-security@claude-plugins-official` | Deep vulnerability scan of the whole repo. Run before going live with more money. |
| `code-review@claude-plugins-official` | Multi-agent PR review with confidence scoring. |
| `code-simplifier@claude-plugins-official` | Cleanup pass on recent changes. Overlaps with ponytail. |
| `feature-dev@claude-plugins-official` | Explore, design, review workflow for bigger features. Overlaps with superpowers. |
| `playwright@claude-plugins-official` | Drive the FastAPI web dashboard in a browser for end-to-end checks. |
| `context7@claude-plugins-official` | Up-to-date docs for yfinance, ib_async, alpaca-py, FastAPI. |
| `duckdb-skills@claude-plugins-official` | Query `data/*.db` SQLite files and CSV/Parquet price data with SQL. |
| `financial-analysis@claude-for-financial-services` | Anthropic's DCF, comps and 3-statement modelling skills. Useful for the fundamental agent. |
| `equity-research@claude-for-financial-services` | Earnings analysis and research workflows. |
| `superpowers-lab@superpowers-marketplace` | Experimental extra superpowers skills. |

## Install by hand

```text
/plugin marketplace add anthropics/claude-plugins-official
/plugin marketplace add JuliusBrussee/caveman
/plugin marketplace add DietrichGebert/ponytail
/plugin marketplace add obra/superpowers-marketplace
/plugin marketplace add anthropics/financial-services-plugins

/plugin install superpowers@claude-plugins-official
/plugin install caveman@caveman
/plugin install ponytail@ponytail
/plugin install security-guidance@claude-plugins-official
/plugin install pr-review-toolkit@claude-plugins-official
/plugin install pyright-lsp@claude-plugins-official
/plugin install commit-commands@claude-plugins-official
```

Check what is active with `/plugin` and `/skills`.

## Turning things off

Set a plugin to `false` in `enabledPlugins`, or override it just for yourself in `.claude/settings.local.json` (git-ignored):

```json
{ "enabledPlugins": { "caveman@caveman": false } }
```

## Notes

- Third-party plugins run code (hooks) on your machine. caveman and ponytail are popular open-source projects, but read their hooks before you trust them with a machine that holds broker credentials.
- caveman and ponytail both inject always-on rules at session start. If replies get too terse to follow, use `/ponytail lite` or say "normal mode".
- `.claudeignore` is not read by Claude Code. Its `.claude/` entry does not hide this setup.
