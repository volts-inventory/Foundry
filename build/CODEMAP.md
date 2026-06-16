# CODEMAP — living index (regenerated each milestone)

Regenerated 2026-06-15T20:37:23 — end of Phase 1.5 (M1.5).

## foundry/ package modules
```
  foundry/__init__.py
  foundry/agents/__init__.py
  foundry/agents/_common.py
  foundry/agents/_runtime.py
  foundry/agents/architecture/__init__.py
  foundry/agents/architecture/conditional.py
  foundry/agents/architecture/principal_architect.py
  foundry/agents/implementation/__init__.py
  foundry/agents/implementation/junior_engineer.py
  foundry/agents/implementation/senior_engineer.py
  foundry/agents/product/__init__.py
  foundry/agents/product/_common.py
  foundry/agents/product/criteria_critic.py
  foundry/agents/product/eng_program_mgr.py
  foundry/agents/product/product_manager.py
  foundry/agents/product/stakeholder_proxy.py
  foundry/agents/quality/__init__.py
  foundry/agents/quality/_common.py
  foundry/agents/quality/qa_engineer.py
  foundry/agents/quality/reviewer_critic.py
  foundry/agents/quality/sdet.py
  foundry/canonical.py
  foundry/cli.py
  foundry/context/__init__.py
  foundry/context/determinism.py
  foundry/context/pinned_constraints.py
  foundry/context/retriever.py
  foundry/durability/__init__.py
  foundry/durability/artifact_store.py
  foundry/durability/checkpointer.py
  foundry/durability/outbox.py
  foundry/durability/serde.py
  foundry/evaluation/__init__.py
  foundry/evaluation/ab_runner.py
  foundry/evaluation/ablation.py
  foundry/evaluation/decision_table.py
  foundry/evaluation/harness.py
  foundry/evaluation/hypothesis.py
  foundry/evaluation/pilot.py
  foundry/evaluation/power.py
  foundry/evaluation/preregistration.py
  foundry/evaluation/scorecard.py
  foundry/evaluation/task_fixtures.py
  foundry/execution/__init__.py
  foundry/execution/adapter_contract.py
  foundry/execution/adapters/__init__.py
  foundry/execution/adapters/claude_code.py
  foundry/execution/adapters/raw_react.py
  foundry/execution/adapters/stub.py
  foundry/execution/sandbox.py
  foundry/gates/__init__.py
  foundry/gates/attestation.py
  foundry/gates/audit.py
  foundry/gates/build.py
  foundry/gates/e2e.py
  foundry/gates/runner.py
  foundry/gates/security.py
  foundry/gates/spec.py
  foundry/gates/supply_chain.py
  foundry/gates/test.py
  foundry/governance/__init__.py
  foundry/governance/budget_controller.py
  foundry/governance/human_liaison.py
  foundry/graph/__init__.py
  foundry/graph/factory_graph.py
  foundry/graph/gate_wiring.py
  foundry/graph/skeleton.py
  foundry/graph/subgraphs/__init__.py
  foundry/graph/subgraphs/architecture.py
  foundry/graph/subgraphs/implementation.py
  foundry/graph/subgraphs/product.py
  foundry/graph/subgraphs/quality.py
  foundry/graph/supervisor.py
  foundry/monitors/__init__.py
  foundry/monitors/cost_monitor.py
  foundry/monitors/guardian.py
  foundry/monitors/loop_detector.py
  foundry/monitors/oracle_monitor.py
  foundry/monitors/policy_monitor.py
  foundry/proxy/__init__.py
  foundry/proxy/lineage.py
  foundry/proxy/model_provider_proxy.py
  foundry/proxy/test_doubles.py
  foundry/schemas/__init__.py
  foundry/schemas/architecture.py
  foundry/schemas/base.py
  foundry/schemas/gates.py
  foundry/schemas/governance.py
  foundry/schemas/implementation.py
  foundry/schemas/operations.py
  foundry/schemas/product.py
  foundry/schemas/quality.py
  foundry/schemas/security.py
  foundry/state/__init__.py
  foundry/state/factory_state.py
  foundry/state/global_records.py
  foundry/state/reducers.py
  foundry/state/seq.py
  foundry/state/tree_sha.py
```

## tests
```
  test_agent_runtime.py
  test_agents_architecture.py
  test_agents_implementation.py
  test_agents_product.py
  test_agents_quality.py
  test_artifact_store.py
  test_cli_smoke.py
  test_context.py
  test_eval_ab_runner.py
  test_eval_ablation.py
  test_eval_decision_table.py
  test_eval_harness.py
  test_eval_hypothesis.py
  test_eval_phase1_5_exit.py
  test_eval_pilot.py
  test_eval_power.py
  test_eval_preregistration.py
  test_eval_scorecard.py
  test_eval_task_fixtures.py
  test_execution.py
  test_factory_graph.py
  test_gate_runner.py
  test_gate_wiring.py
  test_graph_skeleton.py
  test_merge_gates_grounding.py
  test_monitors.py
  test_outbox.py
  test_phase1_exit.py
  test_proxy.py
  test_reducers_properties.py
  test_schemas.py
  test_schemas_architecture.py
  test_schemas_phase1.py
  test_subgraph_architecture.py
  test_subgraph_implementation.py
  test_subgraph_product.py
  test_subgraph_quality.py
  test_supervisor.py
  test_test_doubles.py
  test_tree_sha.py
```

## Phase-1 graph spine: cli.run -> build_factory_graph -> supervisor(product->architecture
  ->[spec]->implementation->quality->[build+test]) -> emit(open_pr). ArtifactStore bodies;
  TwoZoneGateRunner attested-only; resume-idempotent.
## Phase-1.5 decision gate (foundry/evaluation/): hypothesis -> task_fixtures (disjoint pilot/
  scored) -> harness (oracle ITT) -> pilot -> power -> preregistration (alpha ledger) -> ab_runner
  (paired cluster bootstrap + Holm) -> decision_table -> ablation -> scorecard. DRY-RUN/synthetic;
  FOUNDRY arm real (cli.run), baseline+ablation simulated+flagged.

## LOC
 15684 total
