# OmaNCTalk

Nextcloud Talk, native to the [Omarchy](https://omarchy.org) shell: a local-first
chat client that lives in your bar. Chats open instantly and stay searchable
offline, because the interface never talks to the network. It reads a private
SQLite mirror that a small background service keeps in sync with your Nextcloud
server.

OmaNCTalk is a fork of the idea and the interface of
[OmaWhatsApp](https://github.com/MoizIbnYousaf/Omarchy-Whatsapp) (MIT). See
[Acknowledgements](#acknowledgements) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

> **Status:** early release (0.1.0), developed against Nextcloud 33 with Talk.
> Other server versions are untested.

## What it does

- **Instant start, offline search.** The window and the bar dropdown read a local
  mirror (about 60–90 ms per request in measurements, also with hundreds of
  thousands of messages). The complete history of every room is mirrored and
  searched with SQLite FTS5, with prefix matching.
- **Conversations.** One-to-one chats, group and public rooms, replies, reactions,
  edit and delete (for everyone), `@` mentions, favorites, archive and mute.
- **Media.** Send and view images, files and voice messages; downloads on demand.
- **Polls.** Create, vote, change or retract your vote, close a poll, with hidden
  results as an option.
- **Rooms.** Create rooms, rename, add or remove people, promote moderators, leave.
- **Notifications and bar.** Unread badge and tooltip in the bar, optional desktop
  notifications, reply from the compact dropdown.
- **Your theme.** Colors and fonts follow the Omarchy theme.
- **Private by design.** No account, server or telemetry of its own: data goes only
  to your Nextcloud server. See [docs/PRIVACY.md](docs/PRIVACY.md).

Not supported (hidden in the interface): calls, stickers and GIF pickers, typing
and presence indicators, drafts and scheduled messages.

## How it works

```
QML window / bar dropdown
        │  short-lived helper process, JSON over stdin/stdout
        ▼
bin/omanctalk ──reads (read-only)──►  talk.db  ◄──writes──  omanctalk-sync@<account>
                                      (SQLite, FTS5)         (systemd --user service)
                                                                    │ Talk API (HTTPS)
                                                                    ▼
                                                             your Nextcloud server
```

Details, the database schema and the helper contract are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Server behavior that shaped the design
is collected in [docs/TALK_API_NOTES.md](docs/TALK_API_NOTES.md).

## Requirements

- [Omarchy](https://omarchy.org) with its shell (Quickshell) and plugin support
- Python 3.11 or newer (standard library only, no pip packages)
- `systemd --user`
- A Nextcloud server with the Talk app (chat, reactions, polls and the other features
  used are checked at runtime)
- Optional: `zenity` (file chooser), `ffmpeg` (voice message conversion),
  `wl-clipboard` (paste), `notify-send` (notifications), the Noto Color Emoji font
  (emoji room avatars)

## Install

```bash
git clone https://github.com/ManuelDell/omarchy-nextcloud-talk \
  ~/.config/omarchy/plugins/io.github.manueldell.omanctalk
~/.config/omarchy/plugins/io.github.manueldell.omanctalk/scripts/install-sync-unit
omarchy plugin enable io.github.manueldell.omanctalk right
```

The script copies the `omanctalk-sync@.service` template into
`~/.config/systemd/user` and reloads systemd. It changes nothing else.

Open the app (click the bar icon) and sign in: enter your server URL and approve the
login in your browser (Nextcloud *Login Flow v2*). OmaNCTalk stores an **app
password**, never your account password, and starts the sync service for the
account. You can revoke it any time under *Settings → Security* in Nextcloud.

## Using it

| Where | Action |
|---|---|
| Bar icon | Click: open the app · Middle click: dismiss the badge · Right click: mute notifications |
| Command line | `omarchy-shell io.github.manueldell.omanctalk toggleApp '{}'` (bind it to a key in your Hyprland config) |
| App | Search chats and messages, reply, react, attach, record a voice message, manage rooms and accounts |

Several accounts can be signed in at once; their rooms appear in one list.

## Data on disk

Everything lives under `~/.local/state/omanctalk/`: the account registry (mode 0600),
one mirror per account, downloaded media, voice drafts and the avatar cache. The mirror
is stored **unencrypted**, like any local chat archive. See
[docs/PRIVACY.md](docs/PRIVACY.md) for what is stored, how to remove it and how logging
is kept free of message content.

## Development

```bash
python3 -m unittest discover -s tests          # offline, against tests/fake_talk.py
node plugins/omanctalk/tests/models.cjs        # UI model tests
```

Live tests against a real server are opt-in and described in
[docs/TESTING.md](docs/TESTING.md). Please read [CONTRIBUTING.md](CONTRIBUTING.md)
before opening a pull request.

Much of the code was written with the help of AI coding assistants; behavior is
covered by the test suites above.

## Acknowledgements

OmaNCTalk stands on other people's work:

- **[OmaWhatsApp](https://github.com/MoizIbnYousaf/Omarchy-Whatsapp)** by
  MoizIbnYousaf (MIT), itself building on *WhatsApp for Omarchy* by Atos Lins: the
  QML interface, the resident service / window / bar-dropdown model, the local-first
  mirror concept and parts of the notification, settings and read-state logic. Their
  copyright notices are kept in [LICENSE](LICENSE).
- **[Omamail](https://github.com/huacnlee/omamail)** by Jason Lee (MIT), which informed
  the ownership model of that design.
- **[Omarchy](https://github.com/basecamp/omarchy)** and
  **[Quickshell](https://quickshell.org)**, the platform this plugin runs on.
- **[Nextcloud Talk](https://github.com/nextcloud/spreed)** and its documented API.

Related projects that were looked at for prior art (no code was taken from them):
[sechat-rs](https://github.com/tofubert/sechat-rs) (terminal client) and
[talk-sync](https://github.com/moodyjmz/talk-sync) (API command line tool).

*Nextcloud and Nextcloud Talk are trademarks of Nextcloud GmbH. This project is not
affiliated with or endorsed by Nextcloud GmbH.*

## License

[MIT](LICENSE). Third-party notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
