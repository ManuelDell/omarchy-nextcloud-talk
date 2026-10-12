# Testing OmaNCTalk

Run commands from the repository root. Use Python 3.11 or newer, SQLite with FTS5, and Node.js for model checks. The backend has no third-party Python dependencies.

## Offline suite

```sh
python3 -m unittest discover -s tests
node plugins/omanctalk/tests/models.cjs
```

Leave `OMANCTALK_LIVE` and `OMANCTALK_LIVE_WRITE` unset (or set to `0`). Live tests are discovered but skipped before credentials are read. Offline tests use temporary stores, synthetic fixtures, injected subprocess implementations and the loopback HTTP fixture; no external server or real account is required. “Offline” means no external network access, not no local sockets.

| Tests | Main coverage |
|---|---|
| `test_api.py` | OCS validation, headers, pagination, mutation guards, retries, transfer limits/cancellation, WebDAV, login transport |
| `test_sync.py` | Mirror creation, polling schedules, event ordering, resumable backfill, FTS, members, media, outbox, daemon lifecycle |
| `test_dto.py`, `test_core_cli.py` | JSON contracts, readiness, search/pagination, read-only reads, acknowledgements, notifications, desktop operations |
| `test_actions.py` | Reservations/idempotency, mentions, offline queuing, uncertain delivery, uploads, partial results, voice, rooms and contacts |
| `test_auth.py`, `test_prefs.py` | Login/account lifecycle, private modes, locked updates, guard preservation, identity collisions, preference validation |
| `test_polls.py` | Poll cache/projection, hidden results, event/periodic refresh, FTS, voting/withdrawal/closing, capabilities and guard failures |
| `test_paths.py`, `test_avatars.py` | Symlink and publication hardening, emoji-avatar extraction/projection |

The schema/migration behavior is exercised through mirror creation and sync/poll tests; there is no separate `test_store.py` in the current suite.

### Fake Talk server

`tests/fake_talk.py` provides `FakeTalkServer`, a stateful standard-library HTTP fixture bound to loopback on an ephemeral port. It enables `OMANCTALK_ALLOW_INSECURE_LOCALHOST=1` for itself and CLI subprocesses. HTTP logging is disabled. Request records contain method, path, query, parsed data and authentication flags, not the Authorization header. Use only synthetic credentials and payloads: recorded request bodies can contain fixture data.

Fixture controls include `add_room`, `add_message`, `edit_message`, `delete_message`, `react`, `add_file`, `remove_room`, `add_poll`, `vote_poll`, `close_poll`, `fail_next`, `respond_next` and a configurable `page_size`. Failure injection supports HTTP status and `Retry-After`; response injection supports malformed/partial protocol cases.

| Server behavior represented | Why it matters |
|---|---|
| Forward polling from a system-message ID returns no results (`quirk_system_cursor`, enabled by default) | Sync must poll from the last real comment and discard mirrored IDs |
| Backward history is descending; forward history is ascending; empty pages return HTTP 304 | Pagination direction, cursor progress and history completion |
| Pagination headers `X-Chat-Last-Given` and `X-Chat-Last-Common-Read` | Mirror cursor/read-state handling |
| Incremental room filtering and `X-Nextcloud-Talk-Modified-Before` | Room-list cursor persistence and periodic full reconciliation |
| Edit/delete/reaction events include parent snapshots | Event application and stale-history protection |
| Poll vote/close events preserve room `lastMessage` and `lastActivity` | Scheduled chat polling and periodic poll refresh cannot rely on room activity |
| Poll tallies use sparse `option-<index>` keys; option IDs are zero-based | Projection must fill missing counts and resolve UI option text correctly |
| Repeated form fields ending in `[]` stay lists; JSON preserves empty vote selections | Multi-option creation and withdrawing all votes |
| Poll close checks creator/moderator; votes check status, bounds and maximum selections | Permission and validation failures |
| WebDAV SEARCH resolves file IDs; upload/share creates a file-message fixture | Media download and share-response handling |

The fixture is intentionally not a complete Talk implementation. In particular, its edit/delete/reaction helpers call the generic message insertion routine, which advances room activity/last-message metadata; regression tests explicitly freeze those fields to model unchanged metadata. Its poll event helper preserves them directly. Avatar responses are synthetic bytes unless a test replaces them; file shares use simplified metadata. Do not infer full server permissions, formats or UI rendering from fixture success alone.

### UI model and contract checks

```sh
node plugins/omanctalk/tests/models.cjs
```

These checks use synthetic JavaScript models and inert process objects, plus QML source assertions. They cover Markdown/link safety, account isolation and stale responses, login timing/cancellation, pending-send reconciliation, mentions, media/voice playback, polls, navigation and settings contracts. They do not launch the helper, connect to a server or replace an actual Qt rendering test.

## Lint and manifest validation

With Qt tooling and the Omarchy shell imports available:

```sh
qmllint -I /usr/share/omarchy/shell plugins/omanctalk/*.qml plugins/omanctalk/*.js
```

On installations where Qt tools are outside `PATH`, use `/usr/lib/qt6/bin/qmllint`. `make lint` supports `QMLLINT` and `OMARCHY_SHELL_DIR` overrides. Missing Qt/Quickshell/shell imports are environment problems to distinguish from component errors.

If the Omarchy plugin command is installed:

```sh
omarchy plugin validate .
```

This validates plugin metadata/layout, not server compatibility. `make validate` combines the repository's test, manifest and lint targets and requires their tools. Use the direct commands above when only a subset is installed.

## Opt-in live checks

**Use a dedicated disposable test account. Never use an account containing real data.** Even the read-only test fetches that account's rooms, participants, avatars and messages into a temporary mirror.

The live tests explicitly read `~/.config/omanctalk/credentials`. Create a private 0700 parent directory and a 0600 file outside the repository with three `KEY=value` lines:

```text
NCTALK_URL=https://cloud.example.com
NCTALK_USER=alice
NCTALK_APP_PASSWORD=<test-account-app-password>
```

This is a live-test input, not an automatic helper account bootstrap. Never commit it, copy it into fixtures, or put the app password on a command line.

### Read-only

```sh
OMANCTALK_LIVE=1 OMANCTALK_LIVE_WRITE=0 \
  python3 -m unittest discover -s tests/live -p test_live_readonly.py
```

`test_live_readonly.py` runs one sync pass with a temporary state root and mirror. Its client rejects methods other than GET/REPORT/PROPFIND/SEARCH. Chat reads do not advance server read markers. Credentials remain in memory rather than entering the account registry; output is counters only. Missing or incomplete credentials skip this test.

### Write lifecycle

```sh
OMANCTALK_LIVE=1 OMANCTALK_LIVE_WRITE=1 \
  python3 -m unittest discover -s tests/live -p test_live_write.py
```

Both flags must equal `1`. The intended smoke lifecycle creates exactly one private group named `omanctalk-test-<random-suffix>`, without enumerating unrelated rooms, then sends, edits, adds/removes a reaction, uploads/downloads a synthetic file and deletes a message. Cleanup attempts to delete only that newly created room. It uses `write_guard_prefix="omanctalk-test-"`; never extend it to existing rooms, even if they happen to share the prefix.

**Current harness limitation:** `test_live_write.py` supplies an `unused.db` path without creating a mirror, but current send/edit/reaction helpers require an existing database for outbox reservations (`mode=rw`). As checked in, the smoke test can create its test room and then fail at the send step; its `finally` block still attempts room cleanup. Do not treat this harness failure as a Talk-server failure or bypass the guard. A successful write smoke run requires fixing that fixture setup first.

Live failure reporting is deliberately limited to step/status, not response bodies or tokens. Verify cleanup using the dedicated test account if cleanup fails. Uploads also create files in the account's Talk storage; room deletion is not a guarantee that uploaded files are removed. Dispose of the test account/storage after testing.

## Test-data rules

- Only synthetic fixtures: invented users such as `alice` and `bob`, a room such as `Team room`, generated IDs, dummy credentials and artificial messages/files.
- Use `cloud.example.com` for documentation server examples; no real domains, user/room/person names, machine identifiers, addresses or account paths.
- Never import chat exports, real message contents, avatars, participant lists, tokens or credentials into tests, screenshots, logs, assertions or issue attachments.
- Redirect test state to temporary directories. Mock desktop/systemd actions and keep new offline tests independent of personal configuration and persistent stores.
- Every live mutation must be opt-in, guarded and confined to the room created by that test. Never retry an uncertain send automatically; verify delivery first.
