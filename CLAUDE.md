# Project rules

## Git commits — hard rule
- All commits MUST be authored as `ArifShaon <arif.shaon@gmail.com>`.
  This is a hard rule and applies to every commit, with no exceptions.
- The repo is configured with `git config user.name "ArifShaon"` and
  `git config user.email "arif.shaon@gmail.com"`. If a commit would be
  authored by anyone else (for example `Claude <noreply@anthropic.com>`),
  set the author explicitly, e.g.
  `git commit --author="ArifShaon <arif.shaon@gmail.com>" ...`.
- Trailers added by the harness (Co-Authored-By, Claude-Session) may remain;
  they do not change the commit author.
