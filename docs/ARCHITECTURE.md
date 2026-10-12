# OmaNCTalk architecture

OmaNCTalk is a Nextcloud Talk client for the Omarchy shell, based on the OmaWhatsApp fork concept. Plugin ID: `io.github.manueldell.omanctalk`.

Priorities are **instant loading from a local mirror** and **offline full-text search**. Network latency must not enter the chat-list or message read path. The Python backend uses only the standard library; desktop integration uses external tools where needed.

## Layers and ownership

```text
QML UI (plugins/omanctalk/)
    |
    | short-lived process; JSON stdin/stdout
    v
bin/omanctalk -> omanctalk_core.py
    |                              |
    | dto: read-only                | actions/auth: explicit requests
    v                              v
SQLite mirror <--- sync daemon ---> Nextcloud Talk API
    talk.db       omanctalk-sync@<account>.service
                      |
                      +--> room/chat/reaction/poll endpoints and WebDAV
```

The daemon creates and migrates the mirror. DTO readers open it with `mode=ro`, `query_only=ON` and a 1-second busy timeout. WAL allows reads alongside short write transactions. The helper is **not entirely read-only**: actions use a separate `mode=rw` connection (750 ms busy timeout) for outbox reservations, stars, downloaded message `local_path` and confirmed poll refreshes, including poll search indexing. There is no `local_media` table.

`Service.qml` owns helper processes, account-scoped responses and pending bubbles. `App.qml` and `Dropdown.qml` present the full and compact interfaces; shared components handle messages, media, recording, polls, settings and onboarding. Per-store `inotifywait` processes watch `talk.db` and `talk.db-wal` creation, deletion, movement and modification, with a 160 ms refresh debounce. A 12-second timer provides fallback chat/message refreshes; status refreshes every 120 seconds. Pending bubbles are UI state, not rows returned by the message DTO.

| Module (`bin/nctalk/`) | Responsibility |
|---|---|
| `store.py` | SQLite connections, migrations, normalization, upserts, FTS query construction |
| `api.py` | Bounded HTTP/OCS transport, Talk and WebDAV endpoints, retries, write guard |
| `sync.py` | One-account daemon, pagination, event application, backfill, caches, outbox replay |
| `dto.py` | Offline chat/message/member/detail projections and readiness status |
| `prefs.py` | Account identity, registry, locked preferences, acknowledgement state, unit control |
| `notify.py` | Independent notification watermarks, generic desktop popups, sound and open actions |
| `actions.py` | Sending, editing, reactions, attachments, media, rooms, contacts and poll operations |
| `auth.py` | Login Flow v2, verified credential storage, account removal |
| `polls.py` | Poll identifiers, partial upserts, search indexing and UI projection |
| `desktop.py` | Clipboard, private drafts, exports, saving/opening cached files, storage overview |
| `paths.py` | No-symlink directory traversal and regular-file checks |
| `avatars.py` | Cached-path validation and generated emoji-avatar extraction |

`bin/omanctalk_core.py` is the dispatcher; network and desktop modules are loaded lazily. `bin/omanctalk-sync` invokes the daemon and redirects bytecode into the private cache.

## SQLite schema and migrations

Source of truth: `bin/nctalk/store.py`, `SCHEMA_VERSION = 4`. Read-write connections set `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000`. No foreign-key constraints are declared in the schema. Each unapplied migration runs in its own `BEGIN IMMEDIATE` transaction and records its version and application time.

The following is the complete declared schema, in migration order (SQLite-managed FTS shadow tables and the AUTOINCREMENT sequence are implicit).

```sql
CREATE TABLE IF NOT EXISTS schema_migrations(
  version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);

-- v1
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE rooms(
  token TEXT PRIMARY KEY, room_id INTEGER, type INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT '', display_name TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
  object_type TEXT NOT NULL DEFAULT '', object_id TEXT NOT NULL DEFAULT '',
  last_activity INTEGER NOT NULL DEFAULT 0, last_message_id INTEGER NOT NULL DEFAULT 0,
  last_read_message INTEGER NOT NULL DEFAULT 0, last_common_read INTEGER NOT NULL DEFAULT 0,
  unread_messages INTEGER NOT NULL DEFAULT 0, unread_mention INTEGER NOT NULL DEFAULT 0,
  unread_mention_direct INTEGER NOT NULL DEFAULT 0,
  is_favorite INTEGER NOT NULL DEFAULT 0, is_archived INTEGER NOT NULL DEFAULT 0,
  notification_level INTEGER NOT NULL DEFAULT 1,
  read_only INTEGER NOT NULL DEFAULT 0, participant_type INTEGER NOT NULL DEFAULT 0,
  can_leave INTEGER NOT NULL DEFAULT 1, can_delete INTEGER NOT NULL DEFAULT 0,
  has_call INTEGER NOT NULL DEFAULT 0, avatar_version TEXT NOT NULL DEFAULT '',
  is_custom_avatar INTEGER NOT NULL DEFAULT 0, message_expiration INTEGER NOT NULL DEFAULT 0,
  actor_type TEXT NOT NULL DEFAULT '', actor_id TEXT NOT NULL DEFAULT '',
  removed INTEGER NOT NULL DEFAULT 0,
  raw TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL DEFAULT 0);
CREATE INDEX rooms_activity ON rooms(removed, last_activity DESC);

CREATE TABLE messages(
  rowid INTEGER PRIMARY KEY,
  token TEXT NOT NULL, id INTEGER NOT NULL,
  actor_type TEXT NOT NULL DEFAULT '', actor_id TEXT NOT NULL DEFAULT '', actor_name TEXT NOT NULL DEFAULT '',
  timestamp INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
  message_type TEXT NOT NULL DEFAULT 'comment', system_message TEXT NOT NULL DEFAULT '',
  parent_id INTEGER, reference_id TEXT NOT NULL DEFAULT '', thread_id INTEGER,
  is_deleted INTEGER NOT NULL DEFAULT 0, edited_ts INTEGER NOT NULL DEFAULT 0,
  expiration_ts INTEGER NOT NULL DEFAULT 0, markdown INTEGER NOT NULL DEFAULT 0,
  params TEXT NOT NULL DEFAULT '{}',
  reactions_json TEXT NOT NULL DEFAULT '{}', reactions_self TEXT NOT NULL DEFAULT '[]',
  file_id INTEGER, file_name TEXT NOT NULL DEFAULT '', file_mime TEXT NOT NULL DEFAULT '',
  file_size INTEGER NOT NULL DEFAULT 0, file_path TEXT NOT NULL DEFAULT '',
  local_path TEXT NOT NULL DEFAULT '', media_type TEXT NOT NULL DEFAULT '',
  search_text TEXT NOT NULL DEFAULT '', raw TEXT NOT NULL DEFAULT '{}',
  UNIQUE(token, id));
CREATE INDEX messages_room ON messages(token, id DESC);
CREATE INDEX messages_room_ts ON messages(token, timestamp DESC);
CREATE INDEX messages_media ON messages(token, media_type, id DESC) WHERE media_type != '';
CREATE VIRTUAL TABLE messages_fts USING fts5(
  search_text, content='messages', content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, search_text) VALUES ('delete', old.rowid, old.search_text);
END;
CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, search_text) VALUES ('delete', old.rowid, old.search_text);
  INSERT INTO messages_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
END;

CREATE TABLE reactions(
  token TEXT NOT NULL, message_id INTEGER NOT NULL, emoji TEXT NOT NULL,
  actor_type TEXT NOT NULL, actor_id TEXT NOT NULL, actor_name TEXT NOT NULL DEFAULT '',
  ts INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(token, message_id, emoji, actor_type, actor_id));
CREATE TABLE members(
  token TEXT NOT NULL, actor_type TEXT NOT NULL, actor_id TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '', participant_type INTEGER NOT NULL DEFAULT 0,
  last_seen INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(token, actor_type, actor_id));
CREATE TABLE sync_state(
  token TEXT PRIMARY KEY, newest_id INTEGER NOT NULL DEFAULT 0, oldest_id INTEGER NOT NULL DEFAULT 0,
  history_complete INTEGER NOT NULL DEFAULT 0, members_synced_at INTEGER NOT NULL DEFAULT 0,
  last_sync_ts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, local_id TEXT NOT NULL UNIQUE, token TEXT NOT NULL,
  kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
  error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE stars(token TEXT NOT NULL, message_id INTEGER NOT NULL, ts INTEGER NOT NULL,
  PRIMARY KEY(token, message_id));
CREATE TABLE avatars(token TEXT PRIMARY KEY, version TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL DEFAULT '', checked_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE users(actor_type TEXT NOT NULL, actor_id TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(actor_type, actor_id));

-- v2: activity already checked for each room
ALTER TABLE sync_state ADD COLUMN activity_seen INTEGER NOT NULL DEFAULT 0;

-- v3: generated emoji avatars and scan bookkeeping
ALTER TABLE avatars ADD COLUMN emoji TEXT NOT NULL DEFAULT '';
ALTER TABLE avatars ADD COLUMN color TEXT NOT NULL DEFAULT '';
ALTER TABLE avatars ADD COLUMN scanned INTEGER NOT NULL DEFAULT 0;

-- v4: poll cache
CREATE TABLE polls(
  token TEXT NOT NULL, poll_id INTEGER NOT NULL, question TEXT NOT NULL DEFAULT '',
  options_json TEXT NOT NULL DEFAULT '[]', max_votes INTEGER NOT NULL DEFAULT 0,
  result_mode INTEGER NOT NULL DEFAULT 0, status INTEGER NOT NULL DEFAULT 0,
  num_voters INTEGER NOT NULL DEFAULT 0, voted_self_json TEXT NOT NULL DEFAULT '[]',
  votes_json TEXT NOT NULL DEFAULT '{}', details_json TEXT NOT NULL DEFAULT '[]',
  actor_id TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(token, poll_id));
```

`meta` holds account/server identity, capabilities, room-list cursors, sync health, event ordering and retry timestamps. JSON columns preserve raw Talk data alongside normalized fields. Removed rooms retain history, but are excluded from the chat list; message access/search remains room-scoped.

Search is fully offline. `search_text` combines rendered text, filename and sender; poll indexing also adds question and options. FTS queries replace double quotes with spaces, quote up to 12 whitespace-separated tokens, prefix-match each with `*` and join them with `AND`. Message results are ordered by timestamp and ID, **not FTS rank**. Chat-name search uses escaped `LIKE` on `display_name`.

## Talk-to-UI mapping

| UI field | Source / rule |
|---|---|
| `account` | Lowercased `user@host`; optional port becomes `_port`; characters outside `[a-z0-9._@-]` become `_` |
| Chat `jid` | Room token, not a user identifier |
| Message `id`, `reference_id` | Decimal message ID as a string; outgoing correlation reference as a string |
| `sender_jid`, member `jid` | `<actorType>/<actorId>`, e.g. `users/alice` or `guests/guest1` |
| `from_me` | Actor type is `users` and actor ID equals the configured user |
| Chat `kind` | Types 1 (one-to-one), 5 (changelog): `dm`; 2 (group), 3 (public), 4 (former one-to-one), 6 (note-to-self): `group` |
| `room_type` | Original numeric Talk room type |
| `peer_jid` | Type-1 peer from room object ID, unique other member, or room `name` fallback |
| `timestamp` | Unix seconds; room activity for chats, message timestamp for messages |
| `unread`, `mentioned` | Server unread count; mention flag requires unread messages |
| `pinned`, `archived`, `muted` | Favorite, archive flag, notification level 3 |
| `notification_unread` | Locally acknowledged badge projection, separate from server unread state |
| `quoted_*` | Parent message joined by room token and `parent_id` |
| `reactions` | Per-actor `{emoji,from_me,sender,sender_jid}`; counts/self-reactions are the fallback when details are absent |
| `media_type` | MIME maps to image/video/audio/gif/document; voice messages explicitly map to audio |
| `filename`, `file_size`, `mime_type`, `local_path` | File parameter metadata and local cache path (empty before download) |
| `voice`, `edited`, `revoked` | Voice-message type, edit timestamp, deleted flag |
| Member `role`, `phone` | Participant types 1: owner; 2/6: moderator; 4/5: guest; otherwise user. Phone is empty |

Raw placeholder text stays in `messages.message`. `store.render_text()` substitutes parameter `name` values at read time (and for search indexing): mentions such as `{mention-user1}` become `@alice`; `{file}` becomes the filename; actor/user/object placeholders become their names. The UI renders Talk Markdown. Outgoing `mentions:["users/alice"]` resolves cached display names to Talk `@alice` syntax, quoting user IDs when needed; unknown or ambiguous names are refused.

Only these system events appear as timeline rows with `system:true`: `conversation_created`, `room_created`, `user_added`, `guest_added`, `user_removed`, `user_joined`, `user_left`. Other system events are stored but hidden. Deleted message text and quoted deleted text are empty.

Poll messages use empty `text` and an optional `poll` card:

```text
{question, selectable, options:[{text,votes,voters:[name],mine}],
 voters, id, closed, hidden_results}
```

Here `id` is the poll ID, while helper vote/close requests use the **message ID**. `selectable` is `maxVotes`, or the option count when unlimited. Tallies primarily use Talk's `option-<index>` keys, with numeric-string keys accepted as a fallback. Open hidden-result polls suppress per-option counts and voter names while preserving own selections; the aggregate `voters` value remains `numVoters`.

## Synchronization

1. **Startup:** obtain the per-store `sync.lock`, migrate the database, load capabilities into `meta`, recover interrupted outbox claims. One daemon runs per account.
2. **Rooms:** every successful foreground cycle is followed by a 3-second wait. Fetch room v4 with `noStatusUpdate=1`, `includeStatus=0` and, for incremental queries, `modifiedSince`. Persist `X-Nextcloud-Talk-Modified-Before`. Startup/restart and every 300 seconds perform a full listing and mark absent rooms `removed=1`. Invalid responses must not remove cached rooms.
3. **Messages:** rooms with new last-message IDs or unprocessed activity are fetched immediately. Scheduled polling also catches edits/reactions/deletions that do not change room activity: hot rooms (under 24 hours) every 3 seconds, warm rooms (under 7 days) every 15 seconds, cold rooms every 120 seconds. At most six scheduled polls are selected per cycle; new-message work is not limited by that budget.
4. **Cursors:** first fetch is backward history, newest first. Subsequent forward requests start from the last mirrored **non-system comment**, not merely `newest_id`: Talk can return nothing when given a system-event cursor. Discard already mirrored IDs. Pages have up to 200 messages and use `X-Chat-Last-Given`/`X-Chat-Last-Common-Read`; forward work is bounded to 100 pages per room invocation. All history requests disable read markers and notification acknowledgement (`setReadMarker=0`, `includeLastKnown=0`, `markNotificationsAsRead=0`, normally `timeout=0`).
5. **Backfill:** round-robin over incomplete, non-removed rooms ordered by recent activity. Fetch backward from `oldest_id`, globally limited to one request per 0.3 seconds. Empty results, including HTTP 304, mark history complete. Cursor and page writes commit together, so progress survives restart. A newly populated previously empty room reopens backfill.
6. **Events:** apply `message_edited`, `message_deleted*`, `message_expired` and `reaction*` parent snapshots in event-ID order. Merge partial snapshots with known raw data; clear deleted content, search text and reactions. Per-message event watermarks prevent older backfill or reply snapshots from undoing newer changes. Keep the raw system row but hide it from the timeline. When supported, fetch reaction details for reaction events and messages with reaction counts; replace per-actor rows and recompute counts and own reactions.
7. **Polls:** `object`/`talk-poll` comments and `poll_voted`/`poll_closed` events enqueue poll reads. Events take priority, with four poll requests per foreground cycle. A bounded recovery scan finds missed polls after restart. Refresh open polls in non-removed rooms when at least 60 seconds old. Poll 403/404 responses defer retry for 300 seconds; other failures use a 60-second poll retry timestamp, with auth/rate-limit/server failures propagated. Cache partial responses without erasing absent fields and update FTS.
8. **Members and avatars:** refresh participants when a room has new messages, or after six hours; update `members` and the offline `users` directory in batches of 500. Participant 403/412 is tolerated. Fetch avatars when their version changes; missing avatars have a one-hour retry cooldown. Cache images under each store. Generated SVG emoji avatars yield emoji/color metadata: `ChatAvatar.qml` draws the emoji itself instead of relying on SVG text positioning. Existing unscanned cache entries are scanned once.
9. **Media:** on-demand helper downloads are bounded to 100 MiB. Optional `auto_download_media` downloads images/GIFs up to 20 MiB, at most four candidates per cycle. WebDAV SEARCH resolves file IDs within the account namespace; downloads publish atomically.
10. **Outbox:** claim up to 50 pending rows in ID order, preserving per-room ordering on delivery failures. Helper-generated offline entries are send/edit/delete/react; uploads are not queued, and polls/read markers are never queued. The daemon also accepts legacy file/upload kinds. Successful sends use correlation references to avoid resending already mirrored deliveries. Persist `pending`, `sending`, `failed` or `done` plus sanitized error/result state.

The helper reserves `local_id` before mutations; reuse must match the original operation and payload. It queues only eligible network failures **before** a write was sent. Ambiguous delivery is failed for manual verification, not automatically replayed. Dead `sending` owners become failed; live helper claims remain intact. Attachments use private immutable snapshots, and partial file results preserve exactly which inputs remain unsent.

Transport retries 429 and selected 5xx requests, honoring bounded `Retry-After` (up to 900 seconds). A 5xx POST is not automatically repeated. Daemon foreground and backfill failures have independent exponential backoff up to 60 seconds, extended by `Retry-After`; foreground failures suspend networking. HTTP 401 sets `last_error=auth` and exits with code 3. SIGTERM/SIGINT interrupt waits and stop cleanly. Health records use categories, not remote error bodies.

The user unit uses `Restart=on-failure`, `RestartSec=10`, `RestartPreventExitStatus=3`, `TimeoutStopSec=40`, `UMask=0077`, restricted privileges and a read-only home/system sandbox. A non-default `XDG_STATE_HOME` requires an additional writable path in a unit drop-in.

## Helper JSON contract

Invoke `bin/omanctalk <command> [--account <id>] [--limit <n>]`. Supply one JSON object on stdin; receive one JSON object on stdout. Account-scoped payloads can use `account` instead of the option; conflicting scopes fail. Read limits default to 240 for messages and 500 for chats and are clamped to 1–10000. Action-specific limits use payload fields. `status` and `update-check` do not consume stdin.

Success normally has `ok:true` and, for operations, `kind`. Errors use `{ok:false,error}`; unsupported features use `{ok:false,error,supported:false}`. `supported:true` on validation errors means the operation exists, not that it succeeded. Exit codes: 0 for success, 1 for failed/pending operations, 2 for an unknown subcommand. Login polling pending is the special `{ok:false,pending:true}` response. Errors never echo arbitrary exception text.

### Read and local commands

| Commands | Input → principal output |
|---|---|
| `status` | Optional account option → readiness, connection state, settings, accounts, setup metadata |
| `chats` | `{account?,query?}` → `{ok,query,chats,accounts}` (per-account mirror readiness) |
| `messages` | `{account,jid,query?,before?:{ts,id},kind?}` → `{ok,chat,query,messages,has_more}`; newest first; kind is media/links/docs or empty |
| `members`, `chat-details` | `{account,jid,query?}` → members or cached room/person details and counts |
| `acknowledge` | `{account?,jid?}` → `{ok,kind,count}`; local badge snapshots only, never server read markers |
| `settings` | `{account?,settings?:{...}}` → validated global/account settings |
| `media-mode` | `{auto_download_media:boolean}` → setting and `restarted:0` |
| `notify`, `notify-mode`, `notify-open` | Skip visible room / boolean enabled-preview-sound settings / notification target → sent counts, settings or opened state |
| `avatars` | Cache counts only; `supported:false`, no helper refresh |
| `presence`, `chat-presence` | Unsupported presence; typing/recording/paused accepts `{state}` but returns `sent:false` |
| `export-chat` | `{account,jid,destination,replace?}` → saved path and message count |
| `save-media` | `{account,jid,id,destination,replace?}` → saved path; requires a cached, non-deleted file |
| `open-media`, `paste` | `{path}` / empty object → opener / clipboard text, file or staged image |
| `discard-stage`, `voice-draft` | `{path}` / `{action:create|finalize|discard,path?}` → discard state or private draft path/size |
| `sync-mode` | `{account,online:boolean}` → start/stop and enable/disable account unit, online preference |
| `quit`, `launch` | Stop all / start online-eligible units; persist session-local closed state |
| `about`, `update-check` | Local versions/storage / `managed:false,available:false` |
| `setup`, `teardown` | Setup is an idempotent metadata response; teardown requires `{confirm:"remove"}`, stops/disables units and **keeps** state |

`status` distinguishes configured authentication, usable schema (`database_ready`), non-removed rooms (`rail_ready`), active unit/recent sync (`sync_active`) and fresh successful online state. An empty valid mirror is database-ready but not rail-ready. Notification watermarks are independent of acknowledgement; popups contain generic text rather than chat contents, even when preview is enabled.

### Write commands

All room operations use `jid` as the token and normally include `account`.

| Commands | Additional input → principal output |
|---|---|
| `send` | `{text,reply_id?,mentions?,local_id?}` → `{kind,local_id,queued,message_id?}` |
| `send-new` | `{target:{user:"alice"}|{jid:"room1"},text,...}` → send result plus `jid,chat_jid`; may create a type-1 room |
| `edit`, `delete`, `react` | `{id,text}` / `{id}` / `{id,emoji}` plus optional local ID → durable result; empty emoji removes own reactions |
| `files` | `{paths:[...],caption?,reply_id?,local_id?}` → count, items, transient album ID; failure can include `partial` |
| `voice` | `{path,reply_id?,local_id?}` → size, `draft_cleaned`; private OGG/Opus draft, optional conversion with ffmpeg |
| `forward` | `{id,to_jid,local_id?}` → target and delivery result; text send or remote file share |
| `star` | `{id,starred:boolean}` → local star state |
| `media`, `download-pending` | `{id}` / `{limit?:1..100}` → local path / downloaded-expired-failed counts |
| `chat-action` | `{action}` read/unread/pin/unpin/archive/unarchive/mute/unmute/remove-local → action result; remove-local only hides in preferences |
| `create-group`, `join-group` | `{name,participants:["users/bob"]}` / `{invite:tokenOrSameServerTalkLink}` → room token; creation reports failed participant additions |
| `group-info`, `group-action` | Token / `{action,value?}` → online room details / operation result; rename, description, add, remove, promote, demote, leave supported |
| `contacts-search`, `contact-profile` | `{query}` / `{person:"users/bob"}` → people from online autocomplete with offline-directory fallback / empty about/business fields |
| `check-number`, `select`, `sticker`, `fetch-stickers`, `stickers`, `contact-alias`, `contact-tag` | Unsupported (`check-number` also returns `registered:false`) |

### Poll commands

Require cached capabilities containing `talk-polls`, schema v4 and online preference. Polls are not queued. Creation in a type-6 personal notes room is refused.

| Command | Input → output |
|---|---|
| `poll` | `{account,jid,question,options:[text,...],multi?:integer,hidden?:boolean}` → `{ok,kind:"poll",message_id}` |
| `poll-vote` | `{account,jid,id:messageId,options:[optionText,...]}` → `{ok,kind:"poll-vote",options}` |
| `poll-close` | `{account,jid,id:messageId}` → `{ok,kind:"poll-close"}` |

Creation accepts 2–20 distinct nonempty options and `multi` from 1 to the option count (default 1); selecting all maps to `maxVotes=0`. Voting resolves exact option text to zero-based option IDs. An empty list withdraws votes and is transmitted as JSON to preserve `[]`. Closing is authorized by the server for creator/moderator. Confirmed operations immediately refresh the poll cache. A refresh failure after success explicitly says not to retry the mutation.

## Login and account storage

1. `login-start {server:"https://cloud.example.com"}` posts to `/index.php/login/v2` and returns `{ok,login_url,poll_token,poll_endpoint,server}`.
2. `AccountOperations.qml` opens the browser and polls `login-finish {server,poll_token,poll_endpoint}` every two seconds, with a 20-minute deadline and cancellation/generation checks.
3. HTTP 404 from the poll endpoint means pending. Completion validates `server`, `loginName`, `appPassword`, probes capabilities and writes the account registry under a lock.
4. Registry structure is `{version:1,default:accountId,accounts:[...]}`. Records contain `account`, `id`, `server`, `user`, `app_password`, optional guard and default flag. App passwords are private plaintext on disk, not returned in successful account responses.
5. `systemctl --user enable --now` starts the account unit. Success returns `{ok,account,user,sync_started}`; saving credentials remains successful even if unit startup fails.

Login URLs and poll endpoints must share the supplied server origin and lie under its base path. Reauthentication preserves existing write guards and refuses identity collisions. `account-add {server,user,app_password,write_guard_prefix?}` is an explicit alternative. There is **no automatic credentials-file bootstrap** in the helper. `account-remove {account}` disables/stops the unit, removes registry credentials and keeps the archive (`archive_kept:true`).

## Paths and security

Only absolute XDG overrides are accepted; otherwise defaults apply.

| Path | Contents |
|---|---|
| `$XDG_STATE_HOME/omanctalk/` (default `~/.local/state/omanctalk/`) | Private state root |
| `accounts.json`, `accounts.json.lock` | Credentials registry and update lock |
| `preferences.json`, `preferences.json.lock` | UI settings, per-account acknowledgement/notification/hidden-room state |
| `notify.lock`, `notify-ids.json`, `notify-ids.json.lock` | Notification serialization and replacement IDs |
| `stores/<account-id>/talk.db`, `talk.db-wal`, `talk.db-shm` | Mirror and SQLite sidecars |
| `stores/<account-id>/sync.lock` | Single-daemon lock |
| `stores/<account-id>/media/`, `stores/<account-id>/avatars/` | Cached attachments and hashed avatar files |
| `voice-drafts/`, `staging/` | Private recording drafts and clipboard images |
| `$XDG_CACHE_HOME/omanctalk/pycache/` (default `~/.cache/omanctalk/pycache/`) | Daemon bytecode cache |

The unit launches the installed helper from `~/.config/omarchy/plugins/io.github.manueldell.omanctalk/bin/omanctalk-sync`. Avatars used by sync live **inside the account store**, not in a shared root avatar cache.

Security decisions:

- Private directories use 0700; credentials, preferences, locks, drafts, downloaded files and database files use 0600. The unit applies umask 0077. Local history and app passwords are not encrypted; filesystem permissions are the confidentiality boundary.
- Network errors and logs use fixed categories/statuses, never remote bodies, credentials, message contents or room tokens. Notification content is generic; private targets travel over helper stdin rather than public process arguments.
- HTTPS is mandatory. Plain HTTP is allowed only for loopback fixtures with `OMANCTALK_ALLOW_INSECURE_LOCALHOST=1`; never use that exception for a real account. Redirects are refused so Basic credentials cannot follow another host.
- File/cache/export operations reject symlinks and non-regular files; hardened traversal uses directory descriptors and `O_NOFOLLOW`, atomic publication and bounded transfers. Account lifecycle rejects symlinked registry/state paths; registry locks use `O_NOFOLLOW`. These are targeted protections, not a claim that every preference read uses hardened traversal.
- `write_guard_prefix` is a **development/test safeguard**, not a production permission model. With `"write_guard_prefix":"omanctalk-test-"`, room mutations require a freshly fetched room `name` with that prefix, including read markers, poll operations and uploads/shares. Creation and rename validate the requested name too; guarded type-1 creation is refused because its name cannot be supplied. Offline queue authorization also requires a matching non-removed mirrored room; replay checks again online. Login polling and read-only WebDAV requests are not room mutations. The guard constrains room-oriented client methods, not arbitrary low-level HTTP calls; server authorization remains essential.

See [TESTING.md](TESTING.md) for offline checks and strictly opt-in live access.
