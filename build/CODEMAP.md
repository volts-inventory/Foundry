# CODEMAP — living index (regenerated each milestone)

Generated 2026-06-14T22:45:49.

## Implemented modules (build-order 1–8 + Phase 0)
```
state/
  __init__.py
  factory_state.py
  global_records.py
  reducers.py
  seq.py
  tree_sha.py
schemas/
  __init__.py
  architecture.py
  base.py
  gates.py
  governance.py
  implementation.py
  operations.py
  product.py
  quality.py
  security.py
durability/
  __init__.py
  checkpointer.py
  outbox.py
  serde.py
proxy/
  __init__.py
  lineage.py
  model_provider_proxy.py
execution/
  __init__.py
  adapter_contract.py
  sandbox.py
  __init__.py
  claude_code.py
  raw_react.py
gates/
  __init__.py
  attestation.py
  audit.py
  build.py
  e2e.py
  runner.py
  security.py
  spec.py
  supply_chain.py
  test.py
monitors/
  __init__.py
  cost_monitor.py
  guardian.py
  loop_detector.py
  oracle_monitor.py
  policy_monitor.py
governance/
  __init__.py
  budget_controller.py
  human_liaison.py
context/
  __init__.py
  determinism.py
  pinned_constraints.py
  retriever.py
graph/
  __init__.py
  skeleton.py
(top-level) canonical.py, __init__.py
```

## Test modules
```
  test_context.py
  test_execution.py
  test_gate_runner.py
  test_graph_skeleton.py
  test_merge_gates_grounding.py
  test_monitors.py
  test_outbox.py
  test_proxy.py
  test_reducers_properties.py
  test_schemas.py
  test_tree_sha.py
```

## Not yet built (target — implementation-plan §1)
- agents/  graph/{subgraphs,supervisor.py,factory_graph.py}  trust_root/  domains/
- evaluation/  operations/  lifecycle/  policy/  observability/  cli.py
