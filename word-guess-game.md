# Word Guess game (offline packs)

Play game in Mini App Home (`index.html` → Play · Word Guess). Not a 12th subject. Not a Subject Pass. Does not use a lesson Start slot, AI Voice, Live Talk, or LLM on each guess.

## Five cards

1. Human Anatomy — `human_anatomy`
2. Health and Vitamins — `health_vitamins`
3. Law and Order — `law_order`
4. Chemistry and Physics — `chemistry_physics`
5. Social and Science — `social_science`

Bank loaded from user `wordbank.json` into `word-guess-packs.json` (393 words, 2026-09-04):

- Human Anatomy — 113
- Health and Vitamins — 61
- Law and Order — 68
- Chemistry and Physics — 74
- Social and Science — 77

easy / medium / hard in the source file map to 1 / 2 / 3. Multi-word answers stay as written (`vitamin D`, `habeas corpus`). More words can be appended later.

## How a round works

- Student taps one card.
- App draws **5 words** from that pack (prefer grade band, then mix difficulty).
- Screen shows blanks `_ _ _ _` + the **definition**.
- Student types a letter or the whole word.
- After 2 misses → show **hint1**. After 4 misses → show **hint2**.
- 6 misses → reveal the word, short meaning line, next word.
- Correct whole word or all letters → next word.
- End of 5 → score only (N of 5). No official grade. No badge spam.

Same word is not reused in the same day if the pack still has unused items.

## Bank fields (CSV or JSON)

| Field | Required | Notes |
|---|---|---|
| pack_id | yes | one of the 5 ids |
| word | yes | answer only, a–z, one word preferred |
| definition | yes | first clue, school-safe, 1–2 sentences |
| hint1 | yes | extra clue, not the word itself |
| hint2 | yes | last clue |
| difficulty | yes | 1 easy · 2 mid · 3 hard |
| grade_min | no | default 4 |
| grade_max | no | default 12 |

CSV header:

```
pack_id,word,definition,hint1,hint2,difficulty,grade_min,grade_max
```

JSON must match `word-guess-packs.json` (packs → words[]).

## Offline + admin

- Game reads the packed file in the app. No chat API for guesses.
- Master Admin later: upload CSV/JSON → validate → replace that pack.
- Reject empty word, missing definition, duplicate word in the same pack (case-insensitive).
- No official exam papers, no trademarked test items, no medical advice as treatment.
- Student never uploads the bank.

## What this is not

- Not Spelling Bee (Ivy hear → spell).
- Not Vocabulary Building (Lexsis context cluster).
- Not Health Science live lesson (separate pilot).
- Words stay in the game file, not in `session_messages`.
