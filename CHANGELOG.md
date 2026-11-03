# Changelog

All notable changes to **mathkit**.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Counts below (commands / self-check cases) are measured from the code at that tag —
`len(TABLE)`, `len(DELEGATE)`, `len(CASES)` — not copied from prose.

## [Unreleased]

### Changed

- **The repo is now self-contained — `math_tool.py` is inlined.** The 34 hardened
  commands (`integrate`, `solve`, `dsolve`, `limit`, `series`, `matrix`, `evalf`, …) used to
  be loaded at runtime from a *separate* skill
  (`~/.workbuddy/skills/sympy-math/scripts/math_tool.py`, 2688 lines). `mathkit.py` now
  carries that implementation itself (4976 lines total), so cloning the repo is enough:
  no `MATH_TOOL_PATH`, no `MATHKIT_NO_CORE`, and the
  "`未找到 sympy-math 的 math_tool.py`" failure mode no longer exists.
  The only remaining dependencies are third-party: SymPy 1.14 and optional SymEngine 0.15.
- **Fixed a latent global-state leak introduced by the merge.** `math_tool.main()` did
  `global parse` and rebound it to `rational=False` **without ever restoring it**. Harmless
  in a one-command process; fatal once inlined — a single `--float` delegation would leave
  the global parser in float mode, silently turning every later `1/4` into `0.25`.
  A `_mt_main()` wrapper now saves and restores both `parse` and `E_MAP_ENABLED` around
  each call, and `selfcheck.py` gained a single-process probe that fails if the leak returns.
- **Fixed `--preserve-e` never taking effect.** The handler assigned
  `E_MAP_ENABLED = False` inside `main()` without a `global` declaration, so it only created
  a local variable. The flag now actually disables the `e → E` mapping.
- **`selfcheck.py` no longer skips delegated cases.** It used to drop 13 cases when
  `math_tool.py` was absent (which was always true in CI), reporting a misleading
  `76/76`. All 122 cases now run on every platform, and the `MATHKIT_NO_CORE` environment
  variable is gone from both the script and the workflow.
- **`--json` now separates compute time from import cost.** Alongside the existing
  `ms` (kept for compatibility, now an alias of `compute_ms`), every response carries
  `compute_ms`, `import_ms` and `total_ms`; the text output prints all three as well.
  Reason: measured over 15 commands, wall time totalled 39.2 s of which only **4.1 s
  (11%) was computation** — `import` costs a fixed 1.5–1.9 s per process and the
  interpreter itself another 0.6–1.0 s. Without the split, a 2.6 s `expand x^2` looks
  like a slow algorithm when it is 1 ms of work. `import_ms` deliberately excludes the
  interpreter's own startup, which a script cannot observe from the inside.

### Added

- **Covariant differentiation** — `covdiff`, `divergence`, `metric-compat` fill the
  biggest gap in the tensor module. `covdiff` takes a tensor *field* (components plus
  an index-kind string like `"u,d"`), since ∇ needs to know which slots are raised;
  output rank is +1 with the derivative index first. `divergence` is the trace
  `∇_μV^μ`. `metric-compat` evaluates `∇_α g_μν`, which is identically zero — it is the
  one check that validates the connection without requiring any user-supplied tensor,
  so a sign error in the metric shows up immediately instead of surfacing later as a
  wrong Ricci tensor.
  Verified: `∇_α g_μν = 0` on S²; `∇_μV^μ = 2` for `V=(r,0)` in polar coordinates;
  `∇_φV^φ = cotθ` on S²; and the same components declared `"u,d"` vs `"d,d"` give
  `-1/r` vs `r` respectively — proof that the index positions are actually being used.
- **`tensor` — general-relativity tensor calculus** (`metric`, `christoffel`,
  `riemann`, `ricci`, `scalar`, `einstein`, `kretschmann`, `weyl`, `geodesic`).
  Implemented from the explicit formulas on top of `Matrix.inv` / `diff`, **not** by
  wrapping `sympy.diffgeom` — that module has two problems that make it a trap:
  `metric_to_*` requires the coordinate symbols to be *the same objects* returned by
  `cs.coord_functions()`, and a same-named `Symbol(..., real=True)` is silently treated
  as an unrelated variable, so every derivative returns 0 and the whole tensor comes
  back zero **without an error**; and its return type changed from `dict` to
  `NDimArray` across the 1.x series.
  Each op carries a built-in identity check: Ricci symmetry, the Einstein-trace
  identity `G^μ_μ = −R(n−2)/2`, Weyl tracelessness, and a "vacuum but not flat" hint.
  Verified against closed-form answers: S² gives `R=2`, `R^θ_φθφ=sin²θ`, `K=4`; S³
  gives `R=6` and `Weyl=0`; Schwarzschild gives `R_μν=0`, `G_μν=0`, `K=48M²/r⁶`, and a
  non-zero (Petrov D) Weyl tensor.
- **A GR-specific simplifier.** The generic `simplify` is the wrong tool here:
  it takes 113 ms to zero a Schwarzschild Ricci residual and 990 ms to *fail* on
  `sin(2θ)/(2tanθ) − cos(2θ)`, while `cancel` does the former in 9 ms and
  `trigsimp ∘ rewrite(sin)` gets the latter in 183 ms. The pipeline tries
  cancel → trigsimp → trigsimp∘rewrite and keeps the lowest `count_ops`.

- **Four targeted simplification commands, covering what `simplify` cannot reach.**
  `simplify` is a heuristic that tries sub-simplifiers and keeps the shortest by
  `count_ops`; it has no `assumptions` parameter and often stalls outright
  (`simplify(sin(x)**2/(1-cos(x)))` returns `-sin(x)**2/(cos(x) - 1)`, i.e. it only
  flips the denominator's sign). The additions:
  - `refine EXPR "x:positive,y:real"` → `simplify`'s missing assumption channel:
    `sqrt(x**2) → Abs(x)` for real `x`, `→ x` for positive `x`. Multiple predicates
    are combined with `And` because `refine()` accepts only one assumptions object.
  - `trigsimp EXPR [--method matching|groebner|combined] [--deep]` — the three methods
    are not precision knobs, they return **structurally different** answers; the flag
    was silently hard-wired to `matching` before.
  - `fu EXPR [--rule TR8]` — Hardy's rule set, with single-rule triggering. Note
    `sympy.simplify.fu` is shadowed by the same-named function in the package
    namespace, so it must be reached through `importlib.import_module`.
  - `factor_terms EXPR` — pulls out common factors **without** expanding, which is
    what `factor` does not do: `factor((x+1)**10 - x**10 - 10*x**9)` flattens the
    binomial into a 10-term polynomial, while `factor_terms` leaves it alone.
  - `simp EXPR [--order ...] [--probe N] [--no-check]` — runs
    `cancel → trigsimp → fu → factor_terms` in order, accepting a step only when
    `count_ops` strictly decreases (ties broken by string length) **and** a numeric
    spot-check on random real points agrees. Rejected steps are listed with the reason.
- **`simp` detects domain changes that the numeric check cannot see.**
  `cancel` fills removable singularities — `(x**2-1)/(x-1) → x+1` is not an identity,
  it just happens that random sampling never lands on `x=1`. So `_holes()` separately
  enumerates the original denominator's zeros and reports any point where the input is
  undefined but the output is finite.

### Changed

- **`trigsimp` with `--method groebner|combined` is not reproducible across processes.**
  Measured on `sin(x)**4 - 2*sin(x)**2*cos(x)**2 + cos(x)**4`: five runs of `combined`
  returned `cos(4*x)/2 + 1/2` three times and `cos(2*x)**2` twice. Both are correct, so
  this is a form ambiguity rather than a bug, but it is driven by set iteration order
  and `PYTHONHASHSEED`. `matching` is stable; the command now prints the caveat, and
  the self-check asserts the caveat instead of a shape (asserting a shape made the
  case flaky — it is what surfaced this).
- **Boolean options no longer swallow the next argument.** `_BOOL_FLAGS` (`--trace`,
  `--trig`, `--no-check`, `--coeffs`, `--deep`, `--force`, `--normalize`,
  `--preserve-e`) are now parsed as pure switches, which removes the documented
  "put `--trace` last" constraint: `dirac --trace "gamma^mu gamma^nu"` now works.
- `--method` joined `_VALUE_FLAGS`, so its value is whitelist-checked the same way
  `--engine` is; `--method bubble` reports the stray token instead of misparsing.

- **`scripts/audit_docs.py` — documentation number audit, wired into CI.**
  Hand-written counts have now gone wrong twice in this project (46/35 in v0.1.0's
  release notes, 85→89 in the docs), so the counts are asserted rather than trusted:
  the script imports `mathkit.py` to read `len(TABLE)` / `len(DELEGATE)` as the
  authoritative values, counts `CASES` in `selfcheck.py`, then greps every
  "current-state" doc for a number followed by one of those nouns. Any mismatch fails
  the build. `CHANGELOG.md` is excluded on purpose — its old numbers are history.
- README gained a `python` badge (3.11 / 3.12 / 3.13) next to the version badge.

### Fixed

- **`SKILL.md` claimed the Dirac contraction coverage was `k=1…5`.** It is **`k=1…4`**:
  `verify_dirac.py` has 4 contraction cases containing 3, 4, 5 and 6 gammas, i.e.
  1–4 gammas between the contracted pair. The "4 类" in the same sentence was right,
  so the line contradicted itself.
- **`references/kdense_gap.md` said `dir(sympy)` has 929 public names.** Measured on
  SymPy 1.14.0: **928** non-underscore names (940 total, 12 dunders). "929" matches
  no counting convention and was a measurement slip.
- Added an explicit reproducibility note to `kdense_gap.md`: the 420 / 205 / 13.7 KB /
  92 KB figures depend on a K-Dense snapshot that is not available locally and were
  **not** re-verified; 928 is the only re-checkable one.

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
