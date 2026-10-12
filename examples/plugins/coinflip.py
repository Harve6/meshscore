"""A deliberately silly league plugin: every poll it "flips a coin" and announces the running tally.

Use it as a template. The real work in a plugin is the part marked `# YOUR DATA SOURCE HERE`: fetch a schedule or
score feed (requests.get ...), compare with what you saw last time, and call self.alert() when something changed.

Enable it in meshscore.toml:

    [plugins]
    dirs = ["examples/plugins"]

    [bots.coin]
    type = "coinflip"
    channel = "#coin"
"""
import random

from meshscore.plugin import LeaguePlugin


class CoinFlip(LeaguePlugin):
    label = "Coin flips"
    kinds = {"max": {"flip", "final"}, "min": {"final"}, "off": set()}
    min_blurb = "every 5th flip"

    def __init__(self, hub, name):
        super().__init__(hub, name)
        self.heads = self.tails = 0
        self.rng = random.Random()

    async def poll(self):
        # YOUR DATA SOURCE HERE
        if self.rng.random() < 0.5:
            self.heads += 1
        else:
            self.tails += 1
        n = self.heads + self.tails
        self.alert("flip", f"🪙 Flip #{n}: heads {self.heads} - tails {self.tails}")
        if n % 5 == 0:
            self.alert("final", f"🏁 {n} flips: {'heads' if self.heads > self.tails else 'tails'} lead")
        self.mark_ok()
        return 60

    def extra_commands(self):
        return {"!score": self.cmd_score}

    async def cmd_score(self, inc, arg):
        return [f"[{self.tag}] heads {self.heads} - tails {self.tails}"]


LEAGUES = {"coinflip": CoinFlip}
