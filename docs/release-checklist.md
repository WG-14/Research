# Release checklist

- [ ] Confirm the working tree is clean and the version is `0.1.0`.
- [ ] Run compileall and focused boundary, reproduction, distribution, and strategy tests.
- [ ] Run collection, then run the full pytest suite exactly once.
- [ ] Confirm the report `research_classification` directly binds to the manifest.
- [ ] Confirm required result hashes have no receipt-side fallback.
- [ ] Run `uv build` and inspect the exact wheel and sdist contents.
- [ ] Install the wheel and sdist into separate isolated environments outside the repository.
- [ ] Verify console/module CLI help parity, package imports, and supported strategy lists across both installs.
- [ ] Generate and verify `dist/SHA256SUMS`.
- [ ] Download the GitHub Actions distribution artifact and verify its checksums.
- [ ] Run the positive reproduction test and review any drift report.
- [ ] Run the repository residue search.
- [ ] Confirm remote CI is green.
- [ ] A human explicitly creates any tag; this checklist does not create one.
