# Claude Code instructions

Project instructions live in AGENTS.md (shared with the other coding agents):

@AGENTS.md

## Skills environment

Plugins and project skills for this repo are set up in `.claude/`. See `docs/claude-skills.md` for what is installed and how to turn things on or off.

- Touching the real-money path (`src/trading/live.py`, `broker.py`, `risk.py`, `fees.py`, sizing, kill switch)? Use the `live-money-safety` skill.
- Building or judging a backtest or shadow experiment? Use the `backtest-discipline` skill.
