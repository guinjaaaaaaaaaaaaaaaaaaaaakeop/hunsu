## Q-unread-stays-false

When `judge consume` re-judges a situation whose findings changed, and its resolution's `reviewed` is exactly `false`, a
grounded still-fits verdict (verbatim quote, no state named) leaves `reviewed: false` as it is: the resolution stays, no
delegation record is written, and consume's output says the judge found it still fits but no person has read the proposal,
so it stays unreviewed. The judge's raw answer stays where it was written (the judge round's response files); no new
manifest field is added for it, and no other key of the resolution changes.
