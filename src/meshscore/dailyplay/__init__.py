"""The "daily" bots: one small thing a day, answered by DM, with a leaderboard on a shared web page.

* ``base``     - what they all share (a shared web board, DMs, hour-gated daily posts)
* ``words``    - the Mesh Word answer list and the Wordle-style scoring
* ``trivia_bank`` - the Mesh Trivia questions
* ``espn``     - the scoreboard feed behind Pick'em
* ``wmo``      - weather codes in words

They reuse the same recipe as the chess puzzle: the channel hears one short post a day and who solved it, never
how; answers only count by DM so nobody can copy them off the air.
"""
