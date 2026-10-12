# Troubleshooting

Start with `meshscore check` - it validates the config, probes every data source, connects to the radio and checks that
each bot's channel exists on it.

## "meshscore is not recognized" / "command not found"

The install put the `meshscore` command in a folder that isn't on your PATH. Use `python -m meshscore <command>`
instead (identical), or install into a virtual environment (`windows\install.bat` does this on Windows).

## "Could not connect" / access denied

A companion radio serves **one client at a time**. Close the MeshCore phone/desktop app (and any other tool - meshcore-cli,
another bot, a web page using Web Serial) and try again. On Windows the COM port can stay busy for a few seconds after
closing the app.

## "Channel(s) not on the radio"

The bot finds channels by **name**. Add the channel to the radio in the MeshCore app first (e.g. `#leafs`), or set
`channel_index = N` in the bot's table to use a slot number directly.

## The bot is silent

* `meshscore check` first.
* Is the bot's alert level `off`? Send `!mode` in the channel.
* Is the game's part of the season switched off? `!phase` - preseason games are off by default.
* No game on? Bots only speak about games. `!score` answers "No game today. Next: ...".
* Channel messages from *other* bots are never treated as commands; but a message that contains an emoji alert marker is
  ignored as a command - send plain `!score`.

## Messages are cut off with "..."

MeshCore channel messages are limited to ~130 bytes of UTF-8; emoji cost 4 bytes each. MeshScore trims scorer/play text
first. Use `tag = "..."` to shorten the prefix.

## Duplicated posts

* Two bots on the air: see [same-air.md](same-air.md). The first time they meet they can double post once.
* Your own `meshscore run` started twice? A local lock (`instance_port`) prevents a second copy on one computer.
* A repeater is retransmitting an old copy: that's the mesh, not the bot.

## `!mode` / `!phase` by strangers

By default anyone can change a bot's alert level. Set `admin_names` and/or `admin_keys` in `[bot]`
([configuration](configuration.md#bot)) - names can be spoofed in a channel, keys can't (DMs only).

## A wrong score, a missing scorer, a late final

The feeds are free and imperfect (see [data-sources.md](data-sources.md)). MeshScore tries to correct itself and says
`⚠️ Score update` when it does. If something is *consistently* wrong, open an issue with the log lines around it and, if
you can, the output of `meshscore dump <league> <team>`.

## Timezone

Times are shown in `[bot] timezone` (an IANA name like `America/Toronto`). On Windows install `tzdata`
(`pip install tzdata`; it is a dependency on Windows already). Empty = the computer's local zone.

## Logs

`meshscore.log` next to the config, or the console. `INFO` lines show every transmission.
