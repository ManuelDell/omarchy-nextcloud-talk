# Third-party notices

OmaNCTalk is released under the MIT License (see [LICENSE](LICENSE)). It contains work
from, and depends on, the projects below. No third-party code is vendored besides the
MIT-licensed OmaWhatsApp lineage described first.

## OmaWhatsApp (and its lineage) — MIT

OmaNCTalk is a fork of [OmaWhatsApp](https://github.com/MoizIbnYousaf/Omarchy-Whatsapp)
by MoizIbnYousaf (MIT), which carries the copyright of Atos Lins for *WhatsApp for
Omarchy*. From that project come the QML interface (window, bar widget, dropdown,
message and media views, composer, settings), the resident-service design, parts of the
notification, settings, acknowledgement and avatar-cache logic and the demo images in
`plugins/omanctalk/assets/`. The WhatsApp transport (`wacli`) was removed and replaced
by a Nextcloud Talk backend (`bin/nctalk/`) with its own SQLite mirror. The original
copyright and permission notice is kept in `LICENSE`, as the MIT License requires.

## Omamail — MIT

The ownership model of the original resident-service, full-window and bar design was
informed by [Omamail](https://github.com/huacnlee/omamail), Copyright © 2026 Jason Lee
(MIT). OmaNCTalk does not vendor Omamail source or assets.

## Nextcloud and Nextcloud Talk

OmaNCTalk uses the documented Nextcloud Talk (spreed) HTTP API. *Nextcloud* and
*Nextcloud Talk* are trademarks of Nextcloud GmbH; this project is not affiliated with
or endorsed by Nextcloud GmbH. The bar icon is a simplified outline redrawing of the
Nextcloud Talk logo, used only to identify the service the plugin connects to. The
Talk app and its artwork are licensed by Nextcloud under the AGPL-3.0; if you
redistribute this project and want to avoid any doubt, replace the icon in
`plugins/omanctalk/BarWidget.qml` with a neutral one.

## Runtime dependencies (not bundled)

| Component | License | Use |
|---|---|---|
| [Omarchy](https://github.com/basecamp/omarchy) | MIT | Shell, plugin API (`qs.Commons`, `qs.Ui`), emoji list read at runtime |
| [Quickshell](https://quickshell.org) | LGPL-3.0 | QML shell runtime |
| Python standard library | PSF | Backend, sync service, tests (no pip packages) |
| Noto Color Emoji | OFL-1.1 | Looked up by name for emoji room avatars; not shipped |

## Prior art (consulted, no code used)

- [sechat-rs](https://github.com/tofubert/sechat-rs), a terminal client for Nextcloud
  Talk (GPL-3.0)
- [talk-sync](https://github.com/moodyjmz/talk-sync), a command line tool for the Talk
  API

## wacli

The OmaWhatsApp design this project forked builds on the `wacli` WhatsApp command line
tool by Peter Steinberger (MIT). OmaNCTalk does not use or include `wacli`.
