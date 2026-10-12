# Security

## Reporting a vulnerability

Please report security problems **privately** using GitHub's *Report a vulnerability*
button on the repository's Security tab, not in a public issue. Include what you found,
how to reproduce it with invented data, and the affected commit. Never include real
credentials, server URLs, room tokens or message content.

## Scope and design notes

- The app password is stored in clear text in a mode-0600 file, the mirror is not
  encrypted: see [docs/PRIVACY.md](docs/PRIVACY.md).
- Only HTTPS servers are accepted (HTTP only for loopback addresses when the test
  environment variable `OMANCTALK_ALLOW_INSECURE_LOCALHOST=1` is set).
- The helper and the sync service refuse symlinked state directories and database files,
  never follow redirects with credentials and do not echo server error bodies.
- Only the latest release is supported.
