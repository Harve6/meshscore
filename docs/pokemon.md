# Pokemon Red / Blue / Yellow: the whole channel plays one game

Everybody on a channel plays **one** copy of Pokemon together (Red, Blue or Yellow). Send a button; the most popular one is pressed in
a headless Game Boy emulator, the game is saved, and a web page shows the screen. The channel itself only hears
about things that matter: a new area, a battle, a level up, a badge.

```
 Ann:   !up 5
 Bob:   !up 5          <- the first vote started a 30 second clock
 Cy:    !a
 #pokemon:  📍 Route 1
 #pokemon:  ⚔ A wild Pokemon appeared! CHARMANDER L5 20/20
 #pokemon:  ⬆ CHARMANDER is now L6!
```

**You must supply your own legally obtained ROMs** (Pokemon Red / Blue / Yellow, US/Europe). MeshScore does not include or download
one, and the ROM and save states must never be committed to a public repo (`*.gb`, `*.state` are git-ignored).

## Set it up

```bat
pip install pyboy pillow
python -m meshscore bots add --type pokemon --channel "#pokemon" --name pokemon
```

then add the ROM to the bot's table in `meshscore.toml`:

```toml
[bots.pokemon]
type = "pokemon"
channel = "#pokemon"
rom = "C:/path/to/Pokemon Red.gb"
```

Create the channel in the MeshCore app first, then restart MeshScore.

| `[bots.pokemon]` setting | default | |
|---|---|---|
| `rom` | | path to your ROM (one game), **or** a `[bots.pokemon.roms]` table for several (below) |
| `game` | first one | which game to start on when there are several |
| `vote_hour` | `9` | local hour the daily "which game" vote opens (only several ROMs) |
| `ballot_minutes` | `60` | how long that vote stays open |
| `site_api`, `site_token` | | host the page on a Cloudflare Worker instead (contrib/cloudflare-board) |
| `window_seconds` | `30` | the vote clock; it starts at the **first** vote of a round |
| `steps` | `3` | tiles a d-pad vote walks (people can ask for 1-8: `!up 5`) |
| `post_every` | `0` | also post a status line every N rounds (0 = events only) |
| `ack` | `"dm"` | how a vote is answered: `dm`, `chan` or `off` |
| `state_file` | `NAME.state` | where the emulator's save state lives (the game resumes after a restart) |
| `site_dir`, `site_url` | | local clone of the web page's GitHub Pages repo, and its address |
| `publish_min_seconds` | `20` | at most one `git push` per this many seconds |
| `show_contact` | `true` | show the channel / contact QR codes on the page |

## Commands

| | |
|---|---|
| `!up` `!down` `!left` `!right` | the d-pad (`!u !d !l !r` too). Add a number to walk further: `!left 4` |
| `!a` `!b` `!start` `!select` | the buttons. `!a 8` taps A eight times in one vote (clicks through text boxes) |
| `!press <button> [n]` | the long form (`!p` for short) |
| `!where` (or just `!pokemon`) | where you are, the lead Pokemon, badges, Pokedex |
| `!game` / `!game blue` | which game is being played / vote for it while the daily vote is open |
| `!switch <game>` | admins: change game right now (each game keeps its own save) |
| `!reset [game] confirm` | admins: wipe the running game - or the named one - back to the title screen |
| `!party` | the whole party with HP |
| `!votes` | the current tally and time left |
| `!top` | who has had the most presses chosen |
| `!mode max\|min\|off` | `min` = badges, new Pokemon and evolutions only |

Vote by DM with `!pokemon up 3` - a DM never touches the channel.

**Admin commands** (`!switch`, `!reset`, and chess's `!newgame`) destroy progress, so they are *off* until you name an admin in `meshscore.toml`: `[bot] admin_keys = ["ab12cd"]` (the start of your public key - authenticated, so use them by DM: `!pokemon reset confirm`) or `admin_names` (a name on a public channel can be faked).

## Several games and the daily vote

Give the bot more than one ROM:

```toml
[bots.pokemon]
type = "pokemon"
channel = "#pokemon"

[bots.pokemon.roms]
red = "C:/roms/Pokemon Red.gb"
blue = "C:/roms/Pokemon Blue.gb"
yellow = "C:/roms/Pokemon Yellow.gb"
```

Each game keeps **its own save** (`pokemon-red.state`, `pokemon-blue.state` ...), so nothing is lost when the channel changes game. Every day in the `vote_hour` hour the bot posts one message and opens a vote (`!game blue`, a unique start of the name works: `!game y`); an hour later (`ballot_minutes`) the winner is loaded and played until the next vote. No votes, or the same game winning, changes nothing. A bot that was down at vote time doesn't open a late vote, and a result that came due while it was down is applied quietly.

An existing single-ROM save (`pokemon.state`) is picked up as Red. Yellow keeps its variables one byte lower in memory than Red/Blue; the bot chooses the right layout from the cartridge header.

## How a round works

1. The first button vote starts the clock. Everyone's vote counts (voting again replaces yours); ties go to the vote
   that came first.
2. When it ends, the winner is pressed - a d-pad vote walks `steps` tiles (or what was asked), a button is tapped once
   - and the emulator settles for a moment.
3. The game is saved, the screen is published, and any events are posted (at most three per round).
4. Votes that arrive while the game runs start the next round. With no votes nothing moves: the game is paused, and an
   idle channel costs no airtime.

Restarts are silent: the emulator resumes from its save state and the round's votes are kept. The game is only
"announced" through changes - the first look after a start or a reset reports nothing.

## The web page

Like the chess board: the bot writes `state.json` + `screen.png` into `site_dir` (a clone of a **public** GitHub Pages
repo) and pushes. Because a new screenshot every round would otherwise bloat the repo, the bot **amends and
force-pushes its own previous commit** - only ever a commit it made itself (message `pokemon: update board`); if the
repo's latest commit is anyone else's it makes a normal new commit first. The page shows the screen, the vote clock,
the live tally, the party with HP bars, recent events and inputs, the top players, and the join QR codes. GitHub
needs a minute or so to deploy, so the picture trails the radio by that much.

Use a repo of its own (say `meshscore-pokemon`), not the chess one.

## What it reads from the game

Location, party (nicknames, species, level, HP), badges, money, Pokedex counts and "in a battle" come straight from
the game's memory (addresses from the pokered disassembly) and species names from the ROM itself. Map names are
known for the cities, routes and the first dungeons; anywhere else reads as "Area 3F".

## Tests

`tests/test_pokemon.py`: the memory reader, vote rules, the whole bot through the hub with a fake Game Boy, restart
silence, the squashing publisher against a real bare repo - and, if a ROM is available, a smoke test with the real
emulator (set `MESHSCORE_POKEMON_ROM`, otherwise it is skipped).
