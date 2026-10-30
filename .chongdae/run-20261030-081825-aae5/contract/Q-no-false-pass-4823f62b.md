## Q-no-false-pass

On that failure nothing is passed off as done. `check` prints no summary line (`errors N · warnings N · info N`), never says
the environment matches, never treats a role as absent or a judgment as fresh or stale because its text could not be
rendered; no project file is written (`hunsu.json`, `hunsu.lock.json`, `hunsu-judgments.json` keep their bytes). `check
--findings` (dwitbuk's reporter) prints no findings document — a reporter that failed is not a clean one — and exits nonzero
with the diagnostic on stderr. A failed attempt leaves no cached partial state: in the same process, once a temporary
directory is available again, `check` renders the roles and reports exactly as it would have.
