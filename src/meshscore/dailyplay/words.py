"""Mesh Word: the answer list and the Wordle-style scoring.

The answers are everyday five-letter English words (no names, nothing rude). Guesses are not checked against a
dictionary - any five letters a-z count - so the list only needs the words the bot may *pick*.
"""
from __future__ import annotations

ANSWERS = """
about above abuse actor acute admit adopt adult after again agent agree ahead alarm album alert alike alive allow alone
along alter among anger angle angry ankle apart apple apply arena argue arise armor aroma array arrow aside asset audio
avoid award aware awful bacon badge baker basic basin basis batch beach beard beast begin being below bench berry birth
black blade blame blank blast blaze bleak blend bless blind block blood bloom blown board boast boost booth bound brain
brake brand brave bread break breed brick bride brief bring broad broke brook brown brush build built bunch burst buyer
cabin cable camel candy canoe carry carve catch cause cedar chain chair chalk charm chart chase cheap check cheek cheer
chess chest chief child chill chime choir chord chunk cider civic claim clash class clean clear clerk click cliff climb
clock close cloth cloud clown coach coast cocoa coral couch cough could count court cover crack craft crane crash crawl
crazy cream creek creep crest crisp cross crowd crown crumb crush curve cycle daily dairy daisy dance dated dealt death
debut decay decor delay dense depth diary dirty ditch dizzy dodge doing donor doubt dough draft drain drama drank drawn
dream dress dried drift drill drink drive drown eager eagle early earth easel eight elbow elder elect elite empty enemy
enjoy enter entry equal error essay event every exact exist extra fable faint fairy faith false fancy fault feast fence
ferry fetch fever fiber field fifth fifty fight final first flame flash fleet flesh float flock flood floor flour fluid
flute focus force forge forth forty forum frame frank fresh front frost fruit fully funny gauge ghost giant given glass
globe gloom glory glove grace grade grain grand grant grape graph grasp grass grave great greed green greet grill grind
groom gross group grove grown guard guess guest guide habit handy happy harsh haste hatch haven heart heavy hedge hello
hence herbs hobby honey honor horse hotel house human humor hurry ideal image imply index inner input issue ivory jelly
jewel joint judge juice karma kayak knife knock known label labor lance large laser later laugh layer learn lease least
leave legal lemon level light limit linen liver local lodge logic loose lover lower loyal lucky lunar lunch magic major
maker maple march marsh match maybe mayor meant medal media melon mercy merge merit metal meter might minor minus mixed
model moist money month moral motor mount mouse mouth movie music naive naval nerve never newly night noble noise north
novel nurse oasis ocean offer often olive onion opera orbit order organ other otter ought outer owner paint panel panic
paper party pasta patch pause peace peach pearl pedal penny phase phone photo piano piece pilot pitch pizza place plain
plane plant plate plaza plead pluck plumb point polar porch pound power press price pride prime print prior prize probe
proof proud prove pulse punch pupil query quest quick quiet quilt quite quote radar radio raise rally ranch range rapid
ratio reach react ready realm rebel refer relax reply rider ridge rifle right rigid risky rival river roast robin robot
rocky rough round route royal rugby ruler rural sadly safer salad sauce scale scene scent scope score scout screw sense
serve seven shade shake shall shape share shark sharp sheep sheet shelf shell shift shine shiny shirt shock shore short
shout shown shrug sight silly since sixth sixty skill skirt slate sleep slice slide slope small smart smell smile smoke
snack snake solar solid solve sorry sound south space spare spark speak speed spend spice spike spill spine spoke spoon
sport spray squad stack staff stage stain stair stake stamp stand stare start state steam steel steep steer stick stiff
still stock stone stood store storm story stove strap straw strip stuck study stuff style sugar suite sunny super surge
swamp swear sweat sweep sweet swift swing sword table taste teach tease teeth tempo tenth thank theme there thick thief
thing think third those three threw throw thumb tiger tight timer tired title toast today token tonic tooth topic torch
total touch tough tower toxic trace track trade trail train trait trash treat trend trial tribe trick tried troop truck
truly trunk trust truth tulip tumor twice twist ultra uncle under union unite unity until upper upset urban usage usual
valid value vapor vault venue verse video vigor villa vinyl viola viral virus visit vital vivid vocal voice voter wagon
waist waste watch water weary weave wedge weigh weird whale wheat wheel where which while white whole whose widen widow
width wield windy witch woman world worry worth would wound woven wrist wrote yacht yield young youth zebra
""".split()

ANSWERS = list(dict.fromkeys(ANSWERS))        # keep the order, drop accidental repeats


def score(guess: str, answer: str) -> str:
    """Wordle scoring: one character per letter - 'g' right place, 'y' in the word elsewhere, '.' not in the word.
    Repeated letters are handled the standard way (a letter is only marked as often as it appears in the answer)."""
    guess, answer = guess.lower(), answer.lower()
    out = ["."] * len(guess)
    left = {}
    for i, (g, a) in enumerate(zip(guess, answer)):
        if g == a:
            out[i] = "g"
        else:
            left[a] = left.get(a, 0) + 1
    for i, g in enumerate(guess):
        if out[i] == "." and left.get(g, 0):
            out[i] = "y"
            left[g] -= 1
    return "".join(out)


SQUARE = {"g": "🟩", "y": "🟨", ".": "⬛"}


def squares(pattern: str) -> str:
    return "".join(SQUARE[c] for c in pattern)


def valid_guess(text: str) -> bool:
    t = (text or "").strip().lower()
    return len(t) == 5 and t.isascii() and t.isalpha()
