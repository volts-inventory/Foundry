# STATUS — resume pointer

- **Session:** 1 (2026-06-15)
- **Phase / milestone:** Phase 1 (M1) in progress. Phase 0 DONE.
- **Done:** Wave A (schemas + test doubles), scaffolding (AgentRuntime), Wave B (4 agent layers).
- **Tasks:** M1 8/17 done (T-M1-01,02,03,04a,04,05,06,07). 
- **Last gate baseline:** ruff ✓, mypy foundry ✓ (78 files), pytest 349 pass / 1 skip, coverage 85%.
- **Last commit:** 20a6189 (Wave B fixes accepted).

## NEXT-ACTION
Dispatch Wave C — the LangGraph subgraphs that compose the agent nodes:
T-M1-08 (product subgraph; creates foundry/graph/subgraphs/__init__.py) FIRST, then
T-M1-09 (architecture), T-M1-10 (implementation), T-M1-11 (quality) in parallel (each owns its
own subgraphs/<layer>.py). They build on the make_<agent>_node(rt) factories + AgentRuntime.
Then Wave D = T-M1-12 gate-wiring, Wave E = supervisor -> factory_graph -> cli -> exit test.
