# Nextcloud Talk behavior that shaped OmaNCTalk

Notes on how the Talk (spreed) server behaves in practice, verified against Nextcloud 33
with Talk's `chat-v2`, `reactions`, `edit-messages`, `delete-messages`, `talk-polls` and
related features. They explain several design decisions in the sync service. The API
reference is <https://nextcloud-talk.readthedocs.io/>.

## Changes to existing messages are system messages

Editing, deleting and reacting do not change the room entry. They create **system
messages** in the chat stream (`message_edited`, `message_deleted`, `reaction`,
`reaction_deleted`, `reaction_revoked`, `poll_voted`, `poll_closed`) that carry the
changed message as `parent`. A client has to apply them to the parent message.

## `lastActivity` only moves for new comments

The room list (`/api/v4/room`) updates `lastActivity` and `lastMessage` for new comments,
but **not** for edits, reactions or deletions, and `lastMessage.id` keeps pointing at the
edited or deleted message. Consequently `modifiedSince` never lists a room whose only
change is such an event. A client that waits for the room list misses them.

OmaNCTalk therefore polls rooms on a schedule that follows recent activity (hot rooms
every 3 s, warm 15 s, cold 120 s, within a per-cycle budget).

## `lookIntoFuture` and system-message cursors

`GET /api/v1/chat/<token>?lookIntoFuture=1&lastKnownMessageId=<id>` returns messages newer
than `<id>`, **except** that when `<id>` is the id of a *system* message (for example an
edit event) later events are not returned, while the same request with the id of the
original *comment* returns all events after it. OmaNCTalk polls from the last real
comment and drops what it already mirrored.

## Polls

- Create: `POST /api/v1/poll/<token>` with `question`, `options`, `resultMode`
  (0 public, 1 hidden), `maxVotes` (0 unlimited). The poll appears as a chat message
  whose `messageParameters.object` has `type: "talk-poll"`.
- Tallies are keyed `option-<index>` (`{"option-0": 1, "option-2": 1}`), and options
  without votes are missing.
- The voter list (`details`) is only returned once a poll is closed.
- Hidden-result polls expose neither tallies nor voters while open; only your own choice
  (`votedSelf`) is known.
- **Polls cannot be created in the personal notes conversation** (room type 6): the
  server answers `400 {"error":"room"}`.
- Voting an empty `optionIds` list retracts the vote; closing is `DELETE` on the poll.

## Emoji avatars

Rooms with an emoji avatar return an SVG with a `<text>` node and a background `<rect>`.
Qt's SVG renderer does not center that text, so OmaNCTalk extracts emoji and color and
draws them itself.

## Other points

- An empty `lookIntoFuture` result is HTTP 304.
- Reaction details (who reacted) come from `GET /api/v1/reaction/<token>/<messageId>`.
- Login uses *Login Flow v2* (`POST /index.php/login/v2`, then polling the returned
  endpoint) and yields an app password.
- Room types: 1 one-to-one, 2 group, 3 public, 4 former one-to-one, 5 changelog, 6 note
  to self.
