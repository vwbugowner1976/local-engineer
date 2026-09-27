# Ollaya observer

Local Engineer can optionally consult Ollaya as a **local decision-model observer**.
It does not replace Bonsai and it does not control the repair loop.

## Enable

Install Ollaya using its official installer, then start a local Laya model.
The default Ollaya API is:

`http://127.0.0.1:11435/v1/systemone`

Enable the observer only when benchmarking it:

```bash
export LOCAL_ENGINEER_OLLAYA=1
```

Optional overrides:

```bash
export LOCAL_ENGINEER_OLLAYA_URL=http://127.0.0.1:11435/v1/systemone
export LOCAL_ENGINEER_OLLAYA_MODEL=laya
```

Each Local Engineer round sends a compact operational snapshot to Ollaya and records
the returned advisory decision in `working_state.json`.

## Safety boundary

- Disabled by default.
- Localhost only by default.
- Ollaya failures are swallowed and do not stop Bonsai.
- Ollaya output does not change tool permissions, hypotheses, edits, build/test flow,
  or completion status.
- No source code, tool arguments, checkpoints, or event logs are sent.
- The observer is intentionally the first integration stage; gating/control can be
  evaluated later after a benchmark.

## Next stage

After a controlled benchmark, the observer can be promoted to a narrow gate for
specific decisions such as failure classification, reflection eligibility, or
duplicate-loop detection. Any such gate should remain fail-open until its behavior
is measured against the existing Level 6 regression suite.
