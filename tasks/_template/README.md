# Harbor task template

Canonical layout for one webdev-bench Harbor task. The packager
(`recipe/03-package/package.py`) stamps out one of these per design —
this repo ships the `-oneshot` variant only (see `DESIGN.md` §3).

This README itself is **not copied** into packaged tasks — it documents
the template scaffold for repo readers, not the task it produces.

Placeholders in this template are written as `{{ NAME }}`. The packager
substitutes them at copy time:

| Placeholder | Source |
|---|---|
| `{{ TASK_ID }}` | `task_1`, `task_2`, … (sequential) |
| `{{ VARIANT }}` | always `oneshot` |
| `{{ DESIGN_DESCRIPTION }}` | from `recipe/runs/<task_id>/design.json` `description` |
| `{{ PAGES_LIST }}` | bullet list of canonical page filenames |
| `{{ PAGES_JSON }}` | JSON array of canonical page names |
| `{{ ALLOWED_FRAMEWORKS }}` | from design doc — currently `html-css` |
| `{{ ALLOWED_FRAMEWORKS_JSON }}` | JSON array form of the above |
| `{{ BASE_IMAGE }}` | `base-html-css-oneshot` |
| `{{ FONT_DECLARATIONS }}` | rendered `@font-face` block extracted from `solution/source/styles.css`, or empty if the design uses only system fonts |

## Verifier ↔ task layout

At eval time, Harbor's separate-mode verifier mounts the task directory
and the agent's output into the verifier container as:

```
/grading/
  agent_output/          ← copied by Harbor from the agent container's /workspace/output/
  ground_truth/          ← this task's ground_truth/
  task_config.json       ← this task's task_config.json
  tests/                 ← this task's tests/
  grading/               ← the project-wide grading/ package (criteria, gates, aggregator)
```

The per-criterion `tests/<name>/check.py` files use these conventional
paths to find the agent output, ground truth, and the shared grader code.
