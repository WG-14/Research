# Release checklist

- [ ] Confirm the working tree is clean and the version is `0.1.0`.
- [ ] Run compileall and focused boundary, reproduction, distribution, and strategy tests.
- [ ] Run collection, then run the full pytest suite exactly once.
- [ ] Run `uv build` and inspect wheel contents.
- [ ] Install the wheel into an isolated environment outside the repository.
- [ ] Verify console/module CLI help parity and package imports.
- [ ] Run the positive reproduction test and review any drift report.
- [ ] Run the repository residue search.
- [ ] Confirm remote CI is green.
- [ ] A human explicitly creates any tag; this checklist does not create one.
