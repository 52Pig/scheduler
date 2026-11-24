# Dependency Workflows

## Model

Each workflow has one root node. Any node can own an interval, five-field cron, or one-time schedule, including a node with parents; scheduled descendants fire independently after their upstream jobs have succeeded on the same local calendar day. Nodes without a schedule run when dependencies succeed, or through a manual run. Nodes may be logical and have no command. Jobs may fan out into any number of children and later join at a node with multiple parents. Names and IDs are user-defined.

Example registration using the CLI:

```powershell
scheduler add --id intake --name "Intake" --every 3600 --
scheduler add --id prepare-a --name "Prepare A" --depends-on intake -- python C:\jobs\prepare_a.py
scheduler add --id prepare-b --name "Prepare B" --depends-on intake -- python C:\jobs\prepare_b.py
scheduler add --id publish --name "Publish" --depends-on prepare-a --depends-on prepare-b -- python C:\jobs\publish.py
```

The dependency graph is editable in the Web console. It initially fits all nodes into the enlarged graph area; use the zoom controls to inspect details and "适应画布" to restore the overview. Drag a node to arrange the layout. Drag the connector at a node's bottom edge onto another node, or use a node's “连线” action and click a target, to add an edge. Click the × on an edge to remove it. The form also supports selecting multiple parents.

## Execution semantics

Every scheduled node firing creates a workflow run for that node and its unscheduled descendants. A scheduled descendant is not also run early by its parent's workflow; it checks that all ancestors succeeded earlier on its local calendar day. A node is eligible only after every included direct parent succeeds. If a join requires a parent outside a manually selected downstream scope, that join is recorded as skipped. Independent eligible branches run in the configured worker pool. A commandless node succeeds immediately. A failed, timed-out, or manually cancelled node causes dependent descendants to be recorded as skipped; independent branches continue.

Every task can be run manually. The operator can run only that task or it and its downstream tasks. A manual run starts at the selected task, so its own upstream jobs are not rerun. Removing a node with children is rejected; remove descendants first.

## Validation

Registration and graph edits reject unknown parents, self-dependencies, cycles, and joins whose parents do not share the same scheduled root. This keeps one trigger as the unambiguous start of each workflow. Multiple independent roots are supported as separate workflows.

The graph is stored as task definitions in SQLite. APScheduler stores a trigger for each scheduled node. Run history records a workflow run ID so branches can be correlated. Removing a dependency can turn a node into a root; an unscheduled root becomes manual-only until a schedule is added. This is a single-process, single-host design; distributed leases, exactly-once execution, retry policies, and cross-root joins are not provided.
