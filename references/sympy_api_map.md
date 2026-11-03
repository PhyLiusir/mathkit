# SymPy 能力 → mathkit 命令对照

文档定位基于本机 `sympy-1.14-docs/src/`（官方 1.14.0 文档源码）。

| 能力 | SymPy API | mathkit 命令 | 文档位置 |
|---|---|---|---|
| 通用化简 | `simplify` | `simplify` | `modules/simplify/simplify.rst` |
| 三角/幂/对数 | `trigsimp`, `expand_trig`, `powsimp`, `logcombine` | 同名 | `modules/simplify/simplify.rst` |
| 有理式 | `apart`, `cancel`, `together`, `factor` | 同名 | 同上 |
| 展开 | `expand` | `expand` | `modules/core.rst` |
| 求导 | `diff` | `diff` | `modules/calculus/index.rst` |
| 积分 | `integrate`（Risch/Heurisch/Meijer-G/manual） | `integrate`（委托） | `modules/integrals/integrals.rst` |
| 极限 | `limit`（Gruntz）+ `Limit` | `limit`（委托，多重兜底） | `modules/series/limits.rst` |
| 级数 | `series`, `fps`, `aseries` | `series`（委托） | `modules/series/series.rst` |
| 求和/求积 | `summation`, `product` | `summation`/`product`（委托） | `modules/concrete.rst` |
| 留数 | `residue`, `residues`, `singularities` | `residue`（委托，双通道）；`contour`（本文件：极点枚举+围道筛选+2πi·ΣRes+三通道对账） | `modules/series/residues.rst` |
| 积分变换 | `laplace_transform`, `fourier_transform`, `mellin_transform` | `laplace`, `ftransform` | `modules/integrals/integrals.rst` |
| Dirac 代数 | `physics.hep.gamma_matrices`（**仅完整缩并的迹**，无自由指标化简、无 γ5） | `dirac`（本文件：Clifford 化简 + 迹 + γ5/ε + slash） | `modules/physics/hep.rst` |
| GR 张量 | `diffgeom.metric_to_{Christoffel,Riemann,Ricci}_*`（**要求坐标符号与 `cs.coord_functions()` 同一对象**，否则静默全零；返回类型 1.x 间由 dict 改为 NDimArray） | `tensor <op>`（本文件：自实现公式 + 恒等式对账，见 SKILL「GR 张量」） | `modules/diffgeom/` |
| 方程（字典解） | `solve` | `solve`（委托） | `modules/solvers/solvers.rst` |
| 解集 | `solveset` | `solveset` | `modules/solvers/solveset.rst` |
| 方程组 | `linsolve`, `nonlinsolve` | 同名 | 同上 |
| ODE | `dsolve`（45 种 hint） | `dsolve`（委托，回代验证） | `modules/solvers/ode.rst` |
| 递推 | `rsolve` | `rsolve` | `modules/solvers/solvers.rst` |
| 丢番图 | `diophantine` | `diophantine` | `modules/solvers/diophantine.rst` |
| 不等式 | `solve_univariate_inequality`, `reduce_inequalities` | `ineq` | `modules/solvers/inequalities.rst` |
| 数值根 | `nsolve`, `nroots` | `nsolve`, `nroots` | `modules/solvers/solvers.rst` |
| 线性代数 | `Matrix.{det,inv,rref,nullspace,eigenvals,jordan_form,QRdecomposition,singular_values,expm,...}` | `linalg <op>` | `modules/matrices/matrices.rst` |
| 多项式 | `factor`, `roots`, `resultant`, `discriminant`, `minimal_polynomial` | 同名 | `modules/polys/` |
| Groebner | `groebner` | `groebner` | `modules/polys/groebner-bases.rst` |
| 数论 | `factorint`, `isprime`, `totient`, `divisors`, `crt`, `continued_fraction` | `nt <op>` | `modules/ntheory.rst` |
| 概率统计 | `stats.{Normal,...,density,cdf,quantile,E,variance}` | `stats <op>`, `dist/expect/var`（委托） | `modules/stats.rst` |
| 单位 | `physics.units.convert_to` | `convert` | `modules/physics/units/` |
| 角动量 | `physics.wigner.{wigner_3j,clebsch_gordan}` | `wigner`, `cg` | `modules/physics/wigner.rst` |
| 氢原子 | `physics.hydrogen.{R_nl,E_nl}` | `hydrogen` | `modules/physics/hydrogen.rst` |
| 布尔 | `logic.{satisfiable,simplify_logic,to_dnf,to_cnf}` | `logic <op>` | `modules/logic.rst` |
| 打印 | `latex`, `ccode`, `fcode`, `rust_code` | `latex`, `codegen` | `modules/printing.rst` |
| 数值化 | `evalf`, `lambdify` | `evalf`（委托）, `to-numpy` | `modules/evalf.rst`, `modules/numeric-computation.rst` |

## 语义要点（写代码时必须知道）

1. **`Sum`/`Integral`/`Product` 是惰性的**：`Sum(...)` 只构造，要 `.doit()`；`summation()` 才是立即求值。
2. **`==` 是结构相等**，不是数学等价：判等用 `simplify(a-b)`、`.equals()`（可能返回 `None`）或数值对账。
3. **`solve` 会丢解**，`solveset` 才是集合语义（`ImageSet`/`ConditionSet`）；周期方程务必用 `solveset(..., S.Reals)`。
4. **`nsolve` 必须给初值**，且只返回一个根；多根用 `nroots` 或 `solveset` 先分区。
5. **`Matrix.svd()`/`.lu()` 在 1.14 不存在**：用 `singular_values()`、`QRdecomposition()`。
6. **`integrate` 的 `conds`**：定积分可能带收敛条件，需要 `conds='none'` 或 `Piecewise` 时显式处理。
7. **`lambdify` 是数值性能的正解**，不要用 `subs` 循环。
8. **`parse_expr` 的 `^` 是异或**：字符串入口必须加 `convert_xor`（mathkit 的 `_parse` 已处理）。
9. **假设缺失导致静默失败**：`sqrt(x**2)`、`log(x*y)` 展开、`Abs` 化简都依赖 `positive/real`。
10. **`physics.units` 的量纲检查是手动的**：`convert_to` 只换单位，不做量纲合法性证明。

## 子包速查（40 个顶层子包中常用者）

`core`（表达式/假设）· `simplify` · `calculus` · `integrals` · `series` · `solvers` · `matrices` ·
`polys` · `ntheory` · `combinatorics` · `geometry` · `logic` · `sets` · `stats` · `tensor` ·
`diffgeom` · `liealgebras` · `physics.*` · `printing` · `codegen` · `parsing` · `plotting` · `discrete` ·
`holonomic` · `crypto` · `algebras` · `categories` · `concrete` · `utilities` · `testing`。

官方文档：`~/.workbuddy/skills/mathkit/../../..` 无关；本机副本在 `C:/Users/联想/WorkBuddy/2026-10-30-13-00-42/sympy-1.14-docs/`。
