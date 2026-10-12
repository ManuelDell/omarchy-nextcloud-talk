# Privacy

OmaNCTalk has no account, server or telemetry of its own. It talks to exactly one
kind of endpoint: **your Nextcloud server** (over HTTPS), plus your browser once
during sign-in.

## What is stored on your computer

Everything lives under `~/.local/state/omanctalk/` (directories mode 0700, files 0600):

| Path | Content |
|---|---|
| `accounts.json` | Registry of signed-in accounts: server URL, user name and the **app password** in clear text (mode 0600), as is usual for local clients |
| `stores/<account>/talk.db` | The mirror: rooms, **all messages**, reactions, members, polls, avatars index, outbox. SQLite, **not encrypted** |
| `stores/<account>/media/` | Files and images you opened or that were auto-downloaded (images up to 20 MiB, if enabled) |
| `stores/<account>/avatars/` | Room and profile pictures |
| `voice-drafts/` | Voice messages you recorded and have not sent yet |
| `preferences.json` | Settings and local read/notification state |

The sync service also keeps a small cache in `~/.cache/omanctalk/`.

Because the mirror holds your whole history in the clear, protect the machine itself
(full-disk encryption, screen lock). Anyone who can read your home directory can read
your chats and use the app password.

## What leaves your computer

- API requests to your Nextcloud server: listing rooms, reading and sending messages,
  reactions, polls, files, avatars.
- During sign-in your browser opens your server's login page (*Login Flow v2*); no
  password passes through OmaNCTalk.
- Nothing else: no analytics, no crash reports, no update checks, no third-party hosts.

Reading a room does **not** send a read marker on its own: the sync service never marks
rooms as read. The app marks the room you open as read, like any chat client.

## Logs and notifications

- Logs (the journal of `omanctalk-sync@<account>.service`) contain status and error
  categories only. They are written to never include message text, user names, room
  names, file names or credentials; error texts from the server are not echoed.
- Desktop notifications are **off by default**. When enabled, a popup can show a short
  preview of the message; turn the preview off in the settings if you do not want
  message text in popups.

## Removing your data

1. In the app: remove the account (this stops the sync service and deletes the stored
   app password). The local mirror is kept so that nothing is lost by accident.
2. Delete the mirror and caches:
   ```bash
   rm -r ~/.local/state/omanctalk/stores/<account>
   rm -r ~/.cache/omanctalk
   ```
3. Revoke the app password on the server: *Nextcloud → Settings → Security → Devices &
   sessions*.

To remove everything: `rm -r ~/.local/state/omanctalk ~/.cache/omanctalk`.

## Reporting problems without leaking data

Bug reports and test fixtures must never contain real credentials, server URLs of
private instances, room tokens, user names, messages or screenshots of real chats. The
repository's tests use invented data only (see [TESTING.md](TESTING.md)).
