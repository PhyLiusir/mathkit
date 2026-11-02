# Changelog

All notable changes to **mathkit**.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Counts below (commands / self-check cases) are measured from the code at that tag —
`len(TABLE)`, `len(DELEGATE)`, `len(CASES)` — not copied from prose.

## [Unreleased]

## [0.1.2] - 2026-11-02

### Added

- **Matrix-level verification for the Dirac engine** (`scripts/verify_dirac.py`).
  Explicit 4×4 Dirac matrices (Dirac representation) cross-check the symbolic
  simplifier on 4 trace cases and 4 contraction cases, each over 12 random index
  sets — **96 matrix-level comparisons**. `SKILL.md` had been telling readers to run
  this script, but the file had never been committed, so the instruction was dead.
- **CI runs `verify_dirac.py`** as its own step (`MATHKIT_NO_CORE=1`). The Dirac
  engine is the most regression-prone part of the project: rewriting its recursion
  would not fail a single existing self-check case. It is now covered on all
  9 OS × Python combinations.
- `.gitattributes` pinning `* text=auto eol=lf`, so a Windows checkout no longer
  produces CRLF-vs-LF diff noise against the canonical LF sources.

### Fixed

- **`--engine` with no value crashed with a raw traceback.** A bare `--engine` made
  `opt["engine"]` a boolean, and `opt.get("engine", "auto").lower()` raised
  `AttributeError: 'bool' object has no attribute 'lower'`. Engine selection is now
  validated before dispatch, and every malformed form reports a readable error.
- **`--engine <command>` silently swallowed the command name.** The generic
  `--flag value` rule consumed the next token unconditionally, so
  `--engine expand "x^2"` became "unknown command: x^2". `--engine` now only consumes
  the next token when it is actually an engine name (`auto` / `sympy` / `symengine`);
  otherwise the token is left in place and the error names the real problem.
- **`contour --trig` self-check expectation was wrong.** The case expected
  `2 \sqrt{3} \pi` while the true value is `2√3π/3` — one denominator off. It passed
  only because `selfcheck.py` matches substrings, so the assertion was weaker than it
  looked. Now pinned to the exact `2*sqrt(3)*pi/3`.
- **Documentation counts were stale in 10 places.** README, `SKILL.md` and
  `references/kdense_gap.md` advertised 46 built-in / 35 delegated / 85 self-check
  cases. Measured values are **56 / 34 / 89** (90 commands dispatchable in total).
- **README carried a duplicated CI / release / license badge block** — the same three
  badge lines appeared twice, a merge leftover. One copy removed.

### Changed

- `.gitignore` now ignores `.workbuddy/` (agent-local memory, not project source).
- Repository description corrected to 56 commands / 34 delegated / 89 self-check cases.

## [0.1.1] - 2026-10-31

### Fixed

- **`parse` fallback no longer tries implicit multiplication first.** Without
  `math_tool.py`, `a(n+1)` was parsed as `a*(n+1)`, which broke `rsolve`.
- **`selfcheck` forces UTF-8 stdout.** Windows runners printed the summary in cp1252
  and raised `UnicodeEncodeError` *after* every test had already passed.
- **`MATHKIT_NO_CORE=1` now also disables the core import inside the CLI**, so the CI
  path is reproducible on a machine that does have `math_tool.py` installed.

**CI**: 9/9 jobs green — ubuntu / windows / macos × Python 3.11 / 3.12 / 3.13,
76 cases each (13 delegated cases skipped without `math_tool.py`).

## [0.1.0] - 2026-10-31

First release.

- **56 built-in commands + 34 delegated** to a hardened `math_tool.py`
  (symbolic results cross-checked against high-precision numerics).
- **`se` fast path**: SymEngine string-in / string-out (2.4 ms for
  `expand((1+x)^2000)`, versus ~1 s in pure SymPy) — deliberately never round-tripped
  back into SymPy, which costs O(n²) and took 152 s at n=2000.
- Domains: algebra, polynomials, calculus, ODE, linear algebra, number theory,
  statistics, geometry, permutations, units, physics, logic, LaTeX / codegen.
- **85 self-check cases**; CI on ubuntu / windows / macos × Python 3.11 / 3.12 / 3.13.
- `references/` ships a SymPy API map, SymEngine conversion-cost measurements, and a
  line-by-line gap analysis against the K-Dense `sympy` skill.

> **Note on the published 0.1.0 release notes.** They state "46 built-in commands +
> 35 delegated". Measured against the tagged source, `TABLE` already had **56**
> entries and `DELEGATE` **34** at that commit — the counts were wrong on the day of
> release. Only "85 self-check cases" was accurate (it became 89 in 0.1.1). The
> release notes are left untouched as a historical record; the numbers here are the
> measured ones.

[Unreleased]: https://github.com/PhyLiusir/mathkit/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/PhyLiusir/mathkit/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/PhyLiusir/mathkit/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/PhyLiusir/mathkit/releases/tag/v0.1.0
