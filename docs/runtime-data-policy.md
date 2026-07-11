# Research artifact policy

Research artifacts are diagnostic and reproducibility evidence. They must not
be written into the repository. Dataset snapshots and derived traces are
separate from operator-readable reports; audit streams are append-only JSONL.
Use the `ResearchPathManager` and atomic storage helpers for every output.
