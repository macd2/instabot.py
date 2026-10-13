# Rules for AI-assisted sessions in this repository

## Git identity and attribution
- Never add the assistant as author, committer or co-author of a commit.
- Never add `Co-Authored-By`, `Claude-Session`, "Generated with" lines or any
  other assistant attribution to commit messages, pull request titles or
  bodies, code comments or files.
- Commit as the repository owner: `macd2 <waypointlabs@gmail.com>`.

## Branch names
- Never push a branch whose name contains "claude" or any other assistant name.
- Cloud sessions are started on an auto-generated `claude/...` branch. Before
  the first push, rename it to a descriptive name such as
  `feature/<topic>` or `fix/<topic>` and push that instead.

## Scope
- The legacy bot in `src/` uses undocumented Instagram endpoints and is not to
  be "fixed" to evade Instagram's blocking. New work goes through the official
  Graph API (see `igtool/`).
