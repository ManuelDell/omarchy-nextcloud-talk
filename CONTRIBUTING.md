# Contributing

Thanks for helping. A few ground rules keep the project safe and easy to maintain.

## Privacy first

Do not put real data into issues, pull requests, tests, fixtures or screenshots: no
credentials, no server URLs of private instances, no room tokens, no user or room names,
no message text or media. Use invented names (`alice`, `bob`, "Team room") and a
throw-away test room on a test server.

## Setup and tests

```bash
python3 -m unittest discover -s tests          # offline tests, no network needed
node plugins/omanctalk/tests/models.cjs        # UI model tests
```

New behavior needs a test against the fake server (`tests/fake_talk.py`). If the server
behaves in a way the fake does not, teach the fake (and note it in
[docs/TALK_API_NOTES.md](docs/TALK_API_NOTES.md)) rather than mocking around it. Live
tests are opt-in and described in [docs/TESTING.md](docs/TESTING.md).

## Code style

- Python: standard library only, type annotations, short comments that explain *why*.
- The UI keeps the structure of its OmaWhatsApp origin; change what you must and do not
  rewrite what only needs adjusting.
- Read paths must stay fast: the helper opens the mirror read-only and avoids heavy
  imports (measure `bin/omanctalk chats` before and after a change).

## Commits and pull requests

Small, focused commits with a clear message; describe the user-visible outcome in the
pull request and tick the checklist in the template.

## License

By contributing you agree that your work is released under the [MIT License](LICENSE).
