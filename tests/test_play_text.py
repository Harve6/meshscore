"""play_text: MLB play descriptions cut to a message budget without losing the hit or who scored."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from meshscore.leagues.mlb import play_text  # noqa: E402

CONTRERAS = ("William Contreras singles on a line drive to left fielder Teoscar Hernández. "
             "Dylan Smith scores. Willy Adames to 3rd.")
GUERRERO = "Vladimir Guerrero Jr. homers (30) on a fly ball to left center field. George Springer scores."
LINDOR = ("Francisco Lindor grounds out, shortstop Bobby Witt Jr. to first baseman Vinnie Pasquantino. "
          "Mark Vientos scores. Pete Alonso to 3rd.")
SLAM = ("Shohei Ohtani homers (4) on a fly ball to right center field. Mookie Betts scores. "
        "Freddie Freeman scores. Will Smith scores.")


def size(s):
    return len(s.encode("utf-8"))


def test_text_that_fits_is_left_alone():
    assert play_text(GUERRERO, 200) == GUERRERO
    assert play_text("", 50) == ""
    assert play_text(None, 50) == ""


def test_runner_moves_go_first_and_the_scorer_stays():
    out = play_text(CONTRERAS, 90)
    assert out == "William Contreras singles to left fielder Teoscar Hernández. Dylan Smith scores."


def test_tight_budget_keeps_the_hit_and_who_scored():
    out = play_text(CONTRERAS, 55)
    assert out == "William Contreras singles to LF. Dylan Smith scores."
    out = play_text(GUERRERO, 60)
    assert out == "Guerrero homers (30) to left center field. Springer scores."


def test_jr_is_not_a_sentence_end():
    out = play_text(LINDOR, 62)
    assert out == "Francisco Lindor grounds out, SS to 1B. Mark Vientos scores."


def test_never_exceeds_the_budget_and_never_loses_the_batter():
    for text in (CONTRERAS, GUERRERO, LINDOR, SLAM):
        for n in range(30, 140, 7):
            out = play_text(text, n)
            assert size(out) <= n, (n, out)
            assert out, (n, text)


def test_extra_scorers_are_dropped_before_a_word_is_cut():
    out = play_text(SLAM, 56)
    assert out.startswith("Ohtani homers (4)") and out.endswith("scores.") and "..." not in out


def test_replay_review_preamble_goes_first_so_the_play_survives():
    desc = ("Guardians challenged (tag play), call on the field was overturned: Jose Ramirez singles on a sharp line "
            "drive to left fielder Josh Lowe. Chase DeLauter scores.")
    out = play_text(desc, 80)
    assert out.startswith("Jose Ramirez singles") and out.endswith("DeLauter scores."), out
    assert size(out) <= 80
    assert play_text(desc, 400) == desc                          # the full text still wins when it fits
