# Contributing

Thanks for helping improve `sdr-mcp-unraid`.

## Development workflow

1. Fork and create a feature branch.
2. Make focused, minimal changes.
3. Validate syntax and local container behavior:
   - `docker compose config`
   - `docker build -t sdr-mcp-unraid:dev .`
4. Update docs/template assets if behavior changes.
5. Open a PR with clear testing notes.

## Pull request expectations

- Keep changes scoped to one concern.
- Preserve Unraid-friendly defaults (`/config`, `/recordings`, `/data`).
- Document any new ports, environment variables, or template fields.

## Security notes

- Do not add secrets to repository files.
- Prefer non-root runtime and least privilege hardware access.
