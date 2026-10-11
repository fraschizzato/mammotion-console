# Publication checklist

This package is a **sanitized working tree**, not a certified production release. It was prepared from a review ZIP, not from a verified source Git commit.

- [x] Excluded local databases, archives, logs, packages, generated frontend builds and caches.
- [x] Replaced known personal mower ID, real-site coordinates and personal default zone labels with synthetic examples.
- [x] Replaced proprietary marketing image with an original simple SVG.
- [x] Added README, LICENSE, SECURITY.md, pyproject.toml and GitHub CI.
- [ ] Review all changes in `git diff --no-index` against your working copy.
- [ ] Run complete backend + frontend tests (PyMammotion and pytest-asyncio were not installed in the review environment), Docker build, and manual device-specific regression tests on a capable host.
- [ ] Confirm license policy and attribution with all code/assets used.
- [ ] Confirm no identifying information remains in screenshots, fixture data or documents.
- [ ] Change README placeholder GitHub URL and set repository security settings.

The original ZIP lacked pyproject.toml/README; pyproject.toml is reconstructed for packaging and might not fully match your original build/developer toolchain. Commit to a **new empty public repository** rather than importing private history.
