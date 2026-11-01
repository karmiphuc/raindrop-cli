# raindrop-cli

A small, dependency-free CLI for [Raindrop.io](https://raindrop.io) bookmarks, built on the official REST API v1.
Python standard library only (no third-party packages), so there's no supply chain to trust.
**Read and add only**: there are deliberately no delete commands.

## Setup (once)

1. Open https://app.raindrop.io/settings/integrations, create an app, open it, and copy its **Test token**
   (it doesn't expire and only reads your own account).
2. Store it, either as a user environment variable or in a file:

   ```powershell
   [Environment]::SetEnvironmentVariable('RAINDROP_TOKEN', '<paste token>', 'User')
   ```

   or put it on the first line of `~/.config/raindrop/token`. The CLI never prints the token.
3. On Windows, `raindrop.cmd` (copied to `~/.local/bin`) puts `raindrop` on PATH.

## Usage

```text
raindrop whoami
raindrop collections                       # tree with counts
raindrop search "query" [-c ID] [-n 20] [--sort -created|created|score|title|-title|domain|-domain]
raindrop get ID
raindrop add URL [--title T] [--tags a,b] [-c ID] [--note N] [--no-parse]
raindrop tags [-c ID]
raindrop export [-c 0] [--format json|md] [-o FILE]   # all bookmarks, stable order (backup-friendly)
```

Add `--json` to any command for machine-readable output (for agents).
Collection ids: `0` = all except Trash, `-1` = Unsorted, `-99` = Trash.
Search accepts Raindrop's search syntax (see https://help.raindrop.io/using-search#operators).

Behavior: up to 50 items per page with automatic paging; waits and retries on HTTP 429 (rate limit:
120 requests/minute) and on 5xx; 4xx errors fail immediately with a clear message.

## Tests

```bash
python -B -m unittest discover -s tests -v
```

Tests use a fake transport: no network, no token.
