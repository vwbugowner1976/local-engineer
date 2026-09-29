# Local Engineer v0.1

Mac mini local-agent layer for Bonsai 2 27B, Technocore, and keyboard firmware projects in WSL.

## Install

```bash
git clone --branch local-engineer-v0.1 --single-branch https://github.com/vwbugowner1976/technocore-tech-radar.git ~/local-engineer-src
cd ~/local-engineer-src/local-engineer
./install.sh
source ~/.zshrc
```

## LLM switching

```bash
llm status
llm bonsai
llm bonsai-tech
llm qwen
llm stop
llm logs
```

- `llm bonsai`: stop Technocore/Qwen and give Bonsai the machine for Local Engineer.
- `llm bonsai-tech`: Bonsai + Technocore using `TECHNOCORE_LLM_BACKEND=managed_bonsai`. Run this only after the Technocore backend patch succeeds.
- `llm qwen`: stop Bonsai and restore `managed_mlx`.

Bonsai runs from the existing `~/Bonsai-demo` installation with PQ2_0, Metal, context 8192, text-only mode, and a server-wide reasoning budget of 2048.

## Japanese Bonsai Web Chat

The optional `web_chat.py` provides a small browser UI for talking to the local Bonsai server in Japanese.

Start Bonsai first:

```bash
llm bonsai
```

Then start the web chat:

```bash
python3 web_chat.py --host 0.0.0.0 --port 8780
```

Open `http://127.0.0.1:8780` on the Mac. For a phone or another device on the same trusted network/Tailscale, use the Mac's reachable address and port 8780.

The chat server talks only to the local OpenAI-compatible Bonsai endpoint at `http://127.0.0.1:8080/v1` by default. Override it with `BONSAI_API_BASE`.

For a remotely reachable deployment, set an access token before starting the server:

```bash
export LOCAL_ENGINEER_WEB_TOKEN='choose-a-long-random-token'
python3 web_chat.py --host 0.0.0.0 --port 8780
```

The first version is intentionally chat-only. It does not execute Local Engineer tools from chat. Tool/status integration can be added as the next layer after the basic Bonsai conversation is working.

## WSL projects

Default SSH alias: `wsl`.

```bash
local-engineer projects
local-engineer discover
local-engineer build prospector
local-engineer build rmk-pg1kb
```

Override the SSH host with `LOCAL_ENGINEER_SSH_HOST` or edit `~/.config/local-engineer/projects.json`.

## Autonomous repair

```bash
local-engineer fix prospector "Build locally, diagnose the current error, make the smallest safe fix, and rebuild."

local-engineer fix rmk-pg1kb "Investigate the trackball regression, preserve working behavior, fix it, and build."
```

The agent can inspect/search files, make bounded file edits, run allowlisted builds/tests, and inspect diffs. It does not push or commit. Destructive/admin/network commands are blocked. Files touched by a run are backed up under `~/.local/state/local-engineer/backups/`.

## Add Bonsai backend to the current local Technocore

```bash
llm bonsai
local-engineer technocore-bonsai
```

This asks Bonsai to inspect the current Mac-side Technocore tree (not the older GitHub snapshot) and add `managed_bonsai` beside `managed_mlx`, preserving existing behavior and tests.

When the patch and tests succeed:

```bash
llm bonsai-tech
```

The manager checks that no `mlx_worker.py` appears in Bonsai mode; if one does, it stops Technocore again to avoid another 16 GB unified-memory collision.

## Durable Local Engineer (2026-09-19)

The existing project/tool layer now uses `engineer_runtime.py` for durable sessions.
No service is stopped automatically when the API is unavailable. The running model
must identify as Bonsai. Context is measured with the server template/tokenizer,
reserving output space in the 8192-token context. Search remains available during repair.

```sh
local-engineer ask "prospector: investigate the peer display regression"
local-engineer resume ~/.local/state/local-engineer/sessions/RUN/working_state.json
python3 -m unittest -v test_runtime
python3 benchmark.py --level 4
```

Sessions save structured state, tool evidence, token/time metrics and backups after
each tool. Resume rechecks Git and invalidates reads and verification from before
the interruption. Project memory is a hint; current repository instructions win.
Only one run per project is permitted on each orchestration host.

Registry fields: `root`, `transport` (`local` or `ssh`), `ssh_host`,
`preferred_branch`, `protected_branches`, `build`, `clean_build`, `test`,
`artifact_path`, `notes`, `context_length`. Defaults protect main/master.
SSH projects additionally require `runtime_dir` on the destination containing
`engineer_runtime.py`, so command timeout enforcement runs on the execution host.
SSH authentication and host resolution must already work; this program does not
modify credentials or SSH configuration. WSL can also run the same Python CLI
with `BONSAI_API_BASE` pointing at an authenticated SSH local forward.

Commands are exact trusted registry commands or limited read-only Git / Python
test invocations. Builds/tests execute project code: this is a guardrail, not an
OS sandbox. Review registry commands and use isolated checkouts for unfamiliar code.
Existing user files cannot be fully overwritten initially; exact replacements
retain the rest and back up their original content. Branches are never switched,
and commits/pushes are never performed automatically.

Live benchmark levels 1-5 create disposable repos without commits. They test build
discovery, status reporting, compiler-error localization, single-file repair, and
multi-file repair. Level 6 uses an actual project with external acceptance tests;
hardware regression checks must be reported separately from offline compilation.
