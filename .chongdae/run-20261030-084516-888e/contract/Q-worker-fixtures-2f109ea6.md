## Q-worker-fixtures

Test-fixture maintenance, not a new feature: the three tests above build the environment they mean — a normal session
(`AGENT_WORKER` and `HUNSU_SURVEY` removed from the env they give the hook) for their normal-session assertions, and
`AGENT_WORKER=1` set explicitly for any worker-suppression assertion. No hook, no production caller and no guard changes;
nothing clears a production environment. They pass under a normal parent and under an inherited `AGENT_WORKER=1`. There is
no red-before-build for this: the existing failures under a worker parent are its evidence.
