# Golden set — the eval harness fixtures

Quality is a measured number here, not a vibe (SRD §10). Two files:

- **`topics.jsonl`** — ~30 diverse CDC topics across grades and subjects (the
  reference Grade-6 science lesson included). Used by the **live** eval:
  `RUN_INTEGRATION=1 python -m lessonforge.eval --generate` runs each topic
  through the real pipeline and scores the output. This is the acceptance gate
  for a prompt/model/provider change.

- **`exemplar_ldds.jsonl`** — hand-authored, structurally-valid exemplar LDDs.
  Used by the **deterministic** eval (`python -m lessonforge.eval`, the default),
  which scores them with the structural critic — no LLM, no services. This runs
  in CI and guards against a *scorer* regression (a change that silently starts
  rating good lessons as bad, or vice-versa).

Regenerate the exemplars from source dicts (they're validated on write) if the
LDD schema changes — see the generator referenced in the M4 notes. As with
`corpus/seed/`, these are **authored** examples, not official CDC/NEB text.
