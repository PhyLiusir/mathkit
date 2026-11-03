# 与 K-Dense `sympy` skill 的逐条对照

对照对象：`K-Dense-AI/scientific-agent-skills` → `skills/sympy/`
（SKILL.md 13.7 KB + `references/{core-capabilities, matrices-linear-algebra, physics-mechanics,
advanced-topics, code-generation-printing, review}.md`，共约 92 KB，**无脚本**）。
本机拉取方式：`git clone --depth 1 --filter=blob:none --sparse` + `git sparse-checkout set skills/sympy`。

## 怎么对的

1. 从 6 个 md 里抽全部 `foo(...)` 与 `x.bar(...)` 调用，得到 420 个候选标识符；
2. 与 `dir(sympy)` 的 928 个公开名（非下划线项，SymPy 1.14.0 实测）求交，保留 205 个真实 API；
3. 对照 mathkit 的命令表，筛出「文档教了、mathkit 没有」的部分；
4. 只补**CLI 能落地且可自检**的，笔记本专属的（`init_printing` 等）明确不做。

> 可复验性说明：**420 / 205 这两个数依赖 K-Dense 仓库的当时快照，本地没有副本，未复验**；
> 13.7 KB / 92 KB 同理（是外部文件大小）。**928 是可复验的**（`len([n for n in dir(sympy)
> if not n.startswith('_')])`）。三处若日后复核对不上，优先怀疑外部仓库已更新。

## 补进来的（本轮新增，全部有自检用例）

| K-Dense 里的用法 | 对应 API | mathkit 命令 |
|---|---|---|
| 三角展开 | `expand_trig` | `expand_trig` |
| 有理式规范化 | `ratsimp` | `ratsimp` |
| 嵌套根式化简 | `sqrtdenest` | `sqrtdenest` |
| 幂去嵌套 | `powdenest` | `powdenest` |
| 符号/绝对值规范化 | `signsimp` | `signsimp` |
| 分离变量 | `separatevars` | `separatevars` |
| Bessel 化简 | `besselsimp` | `besselsimp` |
| 矩阵对角化 | `M.diagonalize()` | `linalg diagonalize` |
| 最小二乘 | `A.solve_least_squares(b)` | `linalg lstsq` |
| Gram–Schmidt | `Matrix.orthogonalize(*vecs, normalize=True)` | `linalg gramschmidt [--normalize]` |
| 伴随矩阵 | `M.adjugate()` | `linalg adjugate` |
| 横向/纵向拼接 | `row_join` / `col_join` | `linalg hstack` / `vstack` |
| 共轭 | `M.conjugate()` | `linalg conjugate` |
| 零度 | `len(M.nullspace())` | `linalg nullity` |
| 几何点到线距离 | `Point.distance(Line)` | `geo distance` |
| 几何交点 | `geometry.intersection` | `geo intersection` |
| 面积/方程/包含/重心 | `.area/.equation/.contains/.centroid` | `geo area/equation/contains/centroid` |
| 置换对象 | `Permutation(list).order()/signature()/cyclic_form` | `perm order/sign/cycles/inverse/array/power` |
| 置换群 | `PermutationGroup(...).order()` | `perm group` |
| lambdify 数值代入 | `lambdify((x,), expr, 'numpy')` | `neval` |
| 画图 | `matplotlib` + `lambdify` | `plot`（存 PNG） |
| 终端排版 | `pprint/pretty` | `pretty` |

## 故意不做的（以及理由）

| K-Dense 覆盖 | 为什么不做 |
|---|---|
| `physics.mechanics`（`ReferenceFrame`/`RigidBody`/`LagrangesMethod`/`beam.apply_load`…） | 多体建模要交互式试探，CLI 一条命令给不出中间状态；该场景用脚本 + SymPy 更合适 |
| `physics.quantum`（`Ket`/`qapply`/`Operator`） | 同上；mathkit 只保留能一次算完的 `wigner`/`cg`/`hydrogen` |
| `init_printing` / notebook 显示 | 纯笔记本功能，CLI 无意义（`pretty` 已覆盖终端需求） |
| `plot` 的交互式/多子图 | 只做“一条命令出一张 PNG”；复杂排版交给 matplotlib 脚本 |
| `review.md` 的版本核对流程 | 这是给人看的审阅记录，不是能力 |

## mathkit 比它多的部分

1. **可执行**：K-Dense 是纯文档，模型读完还得自己写代码；mathkit 56+ 命令直接出结果。
2. **委托加固**：`integrate/limit/residue/dsolve` 走 `sympy-math`，带符号+数值双通道对账。
3. **SymEngine 快路径**：`se expand "(x+1)^2000"`，2 ms 级（K-Dense 完全没提 SymEngine）。
4. **`contour`**：留数定理算围道积分，自动判极点是否在围道内并乘 2πi（K-Dense 只教 `residue`）。
5. **自检**：117 条用例，`selfcheck.py` 一把跑完；文档型 skill 没有回归保护。
6. **机器可读输出**：`--json`（含 `delegated` 标记），便于流水线。
7. **假设与别名**：`--sym "x:positive"`、`ln→log`、`arcsin→asin`、`^→**` 统一在 `_parse` 里。

## 结论

K-Dense 那份的强项是**覆盖面叙述**（尤其物理与代码生成章节写得细），弱项是**不可执行、无回归**。
本轮补进 22 个命令点，都是“它教了、我们确实没有”的；剩下没补的属于笔记本场景，已在上面列明理由。
