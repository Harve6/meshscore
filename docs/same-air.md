# Two bots on the same air

On a shared mesh it will happen: you and a neighbour both run MeshScore for the same team, or one person runs a
second bot as a backup. Two bots posting every goal would double the airtime and the noise.

MeshScore handles this **without any coordination traffic** - the bots never send messages *to each other*. Each bot
just **listens** to what is already on the channel. Two small rules do the work:

## Rule 1 - don't repeat what was just said

The bot remembers every channel message it hears for `window_seconds` (default 3 minutes). Right before it transmits an
alert it checks whether somebody else already posted the *equivalent*:

* same kind of event (goal / intermission / final / start ...), and
* the same teams and the same score.

Wording differences don't matter - `🚨 TOR GOAL M. Matthews | TOR 1-0 MTL | 1st 12:00` and
`🚨 TOR goal Matthews (PP) | TOR 1-0 MTL | 1st 12:01` are the same event. A *new* score is a new event, so the next goal is
never suppressed. Replies to `!commands` are compared by their words, so two bots answering the same `!score` post
once.

## Rule 2 - a fixed line-up, so they don't talk over each other

If two bots notice a goal in the same second and both transmit, rule 1 comes too late. So once a bot knows other bots
are around (it heard one, or you listed them in `peers`) the bots form a **line-up**:

| position | waits before sending |
|---|---|
| 0 | nothing (+ up to `jitter_seconds`) |
| 1 | `slot_seconds` (default 5 s) |
| 2 | 2 x `slot_seconds` |

A slot is longer than a message takes to cross the mesh, so by the time position 1's turn comes it has heard position 0
and stays quiet. **If the bot in front is down**, the next one in line simply posts after its slot - an automatic
standby, with no configuration and no extra traffic.

Positions are decided by sorting the bots' **node names** alphabetically, so every bot computes the same order on its
own. Want a particular bot to always lead? Give it `rank = 0` in `[same_air]` (and the others `1`, `2`...), or just name
the nodes accordingly.

## How does a bot know another bot is there?

It recognises a bot by what it posts - alert emoji, or a `[TAG ...]` prefix - and by the sender name that MeshCore puts on
the message. Peers it has heard are remembered across restarts (`same_air` state next to your config). To skip even the
first meeting, list the other bots' node names:

```toml
[same_air]
peers = ["Leafs-Bot-Guelph"]
```

## What a single bot costs

Nothing. With no peers known there is no wait and no check beyond a dictionary lookup.

## Limits - please read

* **The first time two bots meet** (neither has heard the other, no `peers` declared) they may both post the same
  alert once. They learn of each other from that post, and from then on the line-up keeps them to one. (Tested:
  `test_first_meeting_can_double_post_once_then_never_again`.) A bot that stays silent because someone else already
  spoke never reveals itself, so a pair can also meet a few alerts in. Rule of thumb: at most one double post per *pair*
  of bots, then exactly one post per alert (tested with three bots starting at random moments).
* Until a pair has met, the two can compute different line-up positions; they agree as soon as they've both heard each
  other. Declaring `peers` removes even that.
* Dedupe is by *content*. If two bots watch the same game from data feeds that disagree for a moment (one has the goal,
  the other doesn't yet) you may see the second bot's post a few seconds later with a different clock - that's two
  genuinely different pieces of information, not a duplicate.
* Bots that are **not** MeshScore (or an older/different version with different wording) are still deduped when they
  use the same `TEAM 1-0 TEAM` score notation; otherwise they simply aren't recognised, and nothing is suppressed.
* This coordinates *alerts and replies on channels*. It doesn't stop two bots from both being *connected to the same
  radio* - a companion radio takes one client at a time, so each bot needs its own radio.

## Turning it off

```toml
[same_air]
enabled = false
```

## Testing it yourself

`tests/test_sameair.py` runs whole bots on a simulated shared channel - same goal seen by both (one post), a full game
(no duplicates, all from the leader), leader down (follower covers), command replies, and the first-meeting limit above.
