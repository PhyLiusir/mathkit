# mathkit

A math **Agent Skill** (SKILL.md + CLI) for exact symbolic computation, built on **SymPy 1.14** and **SymEngine 0.15**.

One entry point for algebra, calculus, equation solving, linear algebra, number theory, statistics, units, physics, logic, LaTeX and code generation — with a self-check suite that actually runs.

```
46 built-in commands · 35 delegated to a hardened engine · 85 self-check cases
```

## Why another SymPy wrapper

- **Executable, not just advice.** Most SymPy "skills" are prompt-engineering documents; the model still has to write code. `mathkit.py` runs the computation and returns a result, LaTeX and timing.
- **Hardened core, reused.** `integrate / limit / residue / dsolve / multi-integrate / converge` are delegated in-process to
  [`sympy-math`](https://github.com/)'s `math_tool.py`, which cross-checks symbolic results against high-precision numerics.
- **SymEngine where it actually helps.** `se` commands keep everything inside SymEngine and return strings.
  Round-tripping a big result back to SymPy is a trap — see [benchmarks](#benchmarks).
- **Regression-tested.** 85 cases in `scripts/selfcheck.py`, run in CI on Linux / Windows / macOS.

## Install

```bash
git clone https://github.com/PhyLiusir/mathkit.git
cp -r mathkit ~/.workbuddy/skills/      # or ~/.claude/skills/
pip install "sympy==1.14.0" symengine numpy
```

Optional: `matplotlib` for `plot`, and the
[`sympy-math`](https://github.com/) skill (provides `math_tool.py`) for the delegated commands.
Without it, mathkit still works for its own 46 commands and skips delegated cases in the self-check.

## Quick start

```bash
MK="python $HOME/.workbuddy/skills/mathkit/scripts/mathkit.py"

$MK expand "(x+1)^6"
$MK integrate "exp(-x^2)" x -oo oo
$MK linalg eigen "[[1,2],[2,1]]"
$MK solveset "sin(x)-1/2" x --domain reals
$MK --sym "x:positive" simplify "sqrt(x^2)"
$MK --json stats pdf normal 0 1 x
```

## Command map

| Area | Commands |
|---|---|
| Algebra | `simplify expand factor apart together cancel trigsimp logcombine powsimp radsimp collect rewrite expand_trig ratsimp sqrtdenest powdenest signsimp separatevars besselsimp` |
| Polynomials | `gcd lcm resultant discriminant groebner roots nroots minpoly degree` |
| Calculus | `diff integrate limit series summation product residue contour laplace ilaplace ftransform` |
| Solving | `solve solveset linsolve nonlinsolve dsolve rsolve diophantine ineq nsolve` |
| Linear algebra | `linalg <det inv rank rref trace transpose nullspace columnspace rowspace charpoly eigen eigenvects jordan qr cholesky svd pinv expm norm cond solve pow diagonalize lstsq hstack vstack conjugate nullity gramschmidt adjugate>` |
| Number theory | `nt <factorint isprime primes totient divisors nextprime primepi crt cf binomial partitions>` |
| Statistics | `stats <pdf cdf quantile sample> <distribution>`, plus `dist expect var mgf mle ci` |
| Geometry / combinatorics | `geo <distance intersection area equation contains centroid>`, `perm <order sign cycles inverse array power group>` |
| Physics / units | `convert`, `wigner`, `cg`, `hydrogen` |
| Logic / output | `logic <sat simplify dnf cnf>`, `latex`, `codegen`, `to-numpy`, `pretty` |
| Numeric | `neval`, `evalf`, `nsolve` |
| SymEngine fast path | `se <expand diff subs series eval>` |

Global flags: `--json`, `--sym "x:positive"`, `--preserve-e`, `--engine sympy|symengine`.

## Design

```
router  →  own handlers (46 commands)
       →  delegate to math_tool.py (35 commands, in-process, output preserved)
       →  se: SymEngine in, string out
```

`mathkit.py` reuses `math_tool.parse` for input handling, so `^ → **`, `ln → log`, `arcsin → asin`,
exact rationals and `--sym` assumptions behave identically across both layers.

## Benchmarks

`expand((1+x)^n)` on an i5-13500H (SymPy 1.14 / SymEngine 0.15):

| Path | n = 200 | n = 1000 | n = 2000 |
|---|---:|---:|---:|
| Pure SymPy | 108 ms | 424 ms | 993 ms |
| SymEngine internal | 0.3 ms | 0.9 ms | 2.4 ms |
| SymEngine → `sympify` back | +84 ms | +1805 ms | **+152 s** |

That last column is why `se` returns strings and why mathkit does **not** auto-switch engines:
converting a large SymEngine result back into SymPy triggers an O(n²) flatten.

## Tests

```bash
python scripts/selfcheck.py     # 85 cases; delegated ones skip if math_tool.py is absent
```

## License

MIT — see [LICENSE](LICENSE).

---

## 中文速览

`mathkit` 是一个「可执行的数学 Agent Skill」：`SKILL.md` 负责告诉模型何时用、怎么用，
`scripts/mathkit.py` 负责真的算。覆盖代数、微积分、方程、线代、数论、统计、几何、单位、物理、
逻辑、LaTeX 与代码生成；积分/极限/留数/ODE 等委托给已加固的 `math_tool.py`（带数值对账）；
SymEngine 只走「字符串进、字符串出」的快路径。85 条自检用例，GitHub Actions 三平台跑。
