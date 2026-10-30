## Q-python3

The adopted Install line asks that `python3` on the host's PATH select Python 3.11 or newer; every host-facing
instruction in these docs says `python3` to match. Step 1 of `skills/compose/SKILL.md` no longer falls back to a bare
`python`; `README.md`'s reporter argv example uses `"python3"`; its Self-check line runs `python3 test_hunsu.py`. Nothing
else changes: other documents are not swept for the word.
