---
name: mathkit
description: 统一符号/数值数学计算工具（SymPy 1.14 + SymEngine 0.15）。当需要化简、展开、因式分解、求导/积分/极限/级数、解方程（代数、方程组、ODE、递推、丢番图、不等式）、线性代数分解、数论、概率统计、单位换算、Wigner/CG 系数、逻辑可满足性、LaTeX 排版或 C/Python 代码生成时使用；也用于对已有推导做数值复核。
agent_created: true
---

# mathkit

## 用途

把「符号推导 + 数值复核 + 格式化输出」收进一个 CLI：默认 SymPy 保证语义正确（假设、分支、特殊函数），
需要极限吞吐时用 `se` 子命令全程留在 SymEngine，输出前才做格式化。

已加固的积分/极限/留数/ODE 等命令**委托**给 `sympy-math` 技能的 `math_tool.py`，不重复实现、不重复踩坑。

## 调用方式（Windows 沙箱注意事项）

python 用托管 venv 的**绝对路径**、脚本用绝对路径、**直接内联，禁止 `PY=...; "$PY"` 这类变量赋值**（会触发
`sandbox-center cmd decisionRecord missing`）：

```bash
"/c/Users/联想/.workbuddy/binaries/python/envs/default/Scripts/python.exe" \
  "/c/Users/联想/.workbuddy/skills/mathkit/scripts/mathkit.py" <命令> [参数...]
```

全局开关：`--json`（机器可读）、`--sym "x:positive,n:integer"`（符号假设）、`--preserve-e`（关闭 e→E）。

## 命令地图

### 代数
| 命令 | 用法 | 示例 |
|---|---|---|
| `simplify` | `EXPR` | `simplify "sin(x)^2+cos(x)^2"` |
| `expand` | `EXPR` | `expand "(x+1)^6"` |
| `factor` | `EXPR [--modulus p]` | `factor "x^4-1"` |
| `apart` / `together` / `cancel` | `EXPR [VAR]` | `apart "1/(x^2-1)" x` |
| `trigsimp` / `logcombine` / `powsimp` / `radsimp` | `EXPR` | `radsimp "1/(sqrt(2)+1)"` |
| `expand_trig` / `ratsimp` / `sqrtdenest` / `powdenest` / `signsimp` / `separatevars` / `besselsimp` | `EXPR` | `sqrtdenest "sqrt(5+2*sqrt(6))"` |
| `collect` | `EXPR VAR` | `collect "x*y+x*z" x` |
| `rewrite` | `EXPR TARGET` | `rewrite "sin(x)" exp` |
| `gcd` / `lcm` | `A B` | `gcd "x^2-1" "x-1"` |
| `resultant` / `discriminant` | `P Q [VAR]` | `discriminant "x^2+1" x` |
| `groebner` | `"P1,P2" "x,y" [--order lex]` | `groebner "x^2+y^2-1,x-y" "x,y"` |
| `roots` / `nroots` / `minpoly` / `degree` | 见 `--help` | `nroots "x^5-x+1" --n 6` |

### 微积分
| 命令 | 说明 |
|---|---|
| `diff EXPR VAR [N]` | 求导（委托，支持 `--at`） |
| `integrate EXPR VAR [A B]` | 积分，带符号/数值双通道对账（委托） |
| `limit EXPR VAR POINT [DIR]` | 极限，多重兜底（委托） |
| `series EXPR VAR POINT [N]` | 级数，`--coeffs` 列系数（委托） |
| `summation VAR LO HI EXPR` / `product ...` | 求和/求积（委托） |
| `residue EXPR VAR POINT` | 留数，双通道（委托） |
| `contour EXPR VAR --domain upper\|lower\|unit\|all` | **留数定理算围道积分**（本文件实现，见专节） |
| `laplace EXPR T` / `ilaplace EXPR S` / `ftransform EXPR X` | 积分变换（新增） |

### 求解
| 命令 | 用法 |
|---|---|
| `solve "EQS" "vars"` | 委托，支持方程组 |
| `solveset EQ VAR [--domain reals\|complex\|integers]` | 解集，返回 `ImageSet`/`FiniteSet` |
| `linsolve "eqs" "x,y"` / `nonlinsolve ...` | 线性/非线性方程组 |
| `dsolve "EQ" "y(x)"` | ODE（委托，自动回代验证） |
| `rsolve "EQ" a` / `diophantine EQ` | 递推 / 丢番图 |
| `ineq EXPR VAR` / `nsolve EQ VAR --x0 1.0` | 不等式 / 数值根 |

### 线性代数
`linalg <op> "[[..],[..]]" [arg]`，op ∈ `det inv rank rref trace transpose nullspace columnspace
rowspace charpoly eigen eigenvects jordan qr cholesky svd pinv expm norm cond solve pow
diagonalize lstsq hstack vstack conjugate nullity gramschmidt adjugate`。
旧接口 `matrix <op> MAT` 仍然可用（委托）。

### 数论 / 统计
- `nt <factorint|isprime|primes|totient|divisors|nextprime|primepi|crt|cf|binomial|partitions> ...`
- `stats <pdf|cdf|quantile|sample> <normal|uniform|exp|poisson|binomial|geom|gamma|chi2|t|beta|f|lognormal> ...`
  例：`stats pdf normal 0 1 x`、`stats cdf normal 0 1 0`、`stats sample normal 0 1 --n 5`
- `dist/expect/var/mgf/mle/ci` 走委托。

### 物理 / 单位 / 逻辑 / 输出
```bash
convert "3*kilometer/hour" "meter/second"     # 单位换算
wigner 1 1 0 0 0 0                            # 3j 符号（--kind 6j/9j）
cg 1/2 1/2 1 1/2 -1/2 0                       # Clebsch-Gordan
hydrogen 1 0                                  # 氢原子径向函数与能级
dirac "gamma^mu gamma^nu gamma_mu"            # Dirac 代数化简（见专节）
dirac "gamma^mu gamma^nu" --trace             # gamma 矩阵求迹
logic sat "p & ~p"                            # SAT
logic simplify "(p|q)&(p|~q)"                 # 布尔化简
latex "x^2/2"                                 # LaTeX
codegen "sin(x)^2" --lang c                   # c/fortran/rust/julia/octave
to-numpy "x^2+1" x                            # 打印 lambdify 代码
```

### 几何 / 置换 / 数值 / 绘图
```bash
geo distance "Point(0,1)" "Line(Point(0,0),Point(1,1))"   # 点到直线
geo intersection "Circle(Point(0,0),1)" "Line(Point(-2,0),Point(2,0))"
geo area "Circle(Point(0,0),1)"                            # 也支持 Polygon/Triangle
perm order "[2,0,1]"          # 置换阶；sign/cycles/inverse/array/power
perm group "[[1,0,2],[0,2,1]]"  # 置换群：order / 交换性 / 循环性
neval "sin(x)*exp(-x)" "x=1.0"   # lambdify 数值求值
plot "sin(x)/x" "x:-10:10" --out fig.png   # 存 PNG（matplotlib Agg）
pretty "x^2+1"                   # 终端二维排版
```

## 留数定理算积分：`contour`

`residues` 只给极点和留数，**不会**替你乘 2πi，也不会替你判断哪个极点在围道内。`contour` 把这一整套串起来：

```bash
mathkit.py contour "1/(1+x^2)" x                    # 默认 --domain upper
mathkit.py contour "1/(2+cos(x))" x --trig          # ∫₀^{2π}，自动 z=e^{iθ}
mathkit.py contour "1/(x^4+1)" x --domain lower     # 下半平面
```

五步：枚举极点 → 按围道筛选 → 逐点 `sp.residue` → 乘 2πi → **三通道对账**。

`--domain` 的语义（决定 2πi 前有没有负号）：

| `--domain` | 围道 | 结果 | 对应的积分 |
|---|---|---|---|
| `upper`（默认） | 上半平面半圆，逆时针 | `2πi·Σ_{Im>0} Res` | `∫_{-∞}^{∞} f dx`（需大圆弧趋于 0） |
| `lower` | 下半平面半圆，顺时针 | `-2πi·Σ_{Im<0} Res` | `∫_{-∞}^{∞} f dx` |
| `unit` | `\|z\|=1` 逆时针 | `2πi·Σ_{\|p\|<1} Res` | `∮_{\|z\|=1} f dz` |
| `all` | — | `2πi·Σ_all Res = -Res_∞` | 无实积分含义，仅核对用 |

**三通道对账**（任一 FAIL 都会打 ⚠️）：

1. 直接 `sp.integrate` 符号积分，与围道结果比对；
2. 围道内每个极点逐一做**小圆数值围道**（半径取到最近邻极点距离的 0.3 倍，避免大围道振荡让 `quad` 失准），数值 ΣRes 与符号 ΣRes 比对；
3. `Σ_all Res + Res_∞ = 0`，检查**极点是否漏枚举**（`exp(I x)` 这类 ∞ 处本质奇点的函数会自动跳过此项）。

### `--trig`：三角积分

`∫₀^{2π} F(θ)dθ` 手做换元极易出错，交给工具：

```bash
mathkit.py contour "1/(2+cos(x))" x --trig
# expand_trig → cos θ=(z+1/z)/2, sin θ=(z-1/z)/(2i) → 乘 dz/(iz) → domain 自动切到 unit
```

代换后会打印实际被积函数，**务必肉眼核一眼**：`∫₀^{2π}dθ/(2+cosθ)` 应变成 `-2i/(z²+4z+1)`。

### 仍然要你自己负责的四件事

`contour` 只算「2πi·ΣRes」，围道本身合不合法它不管：

1. **大圆弧是否趋于 0**（Jordan 引理）——有理函数分母次数比分子高 ≥2 时成立，其余自己证；
2. **支点 / 多值函数**（`√z`、`log z`、`z^a`）——分母解不出极点时会报错，需换键孔或扇形围道手算；
3. **无穷族极点**（`1/sin z`）——会打警告，枚举只覆盖解出的有限个，结果不可引用；
4. **实轴上有极点**——`sin(x)/x` 这类会输出 `结果: 0 ← 不可用`，正解是缩进绕行后取极限，`contour` 不代劳。

最后一条是刻意设计：工具宁可给一个带红字标注的 `0`，也不会静默返回看起来像答案的东西。

## Dirac 代数：`dirac`

SymPy 自带的 `physics.hep.gamma_matrices` 只会算**完整缩并后的迹**——保留自由指标的化简它不做
（`simplify(gamma^mu gamma^nu gamma_mu)` 原样返回），也不支持 γ5。`dirac` 补的就是这一块：

```bash
dirac "gamma^mu gamma^nu gamma_mu"                       # -2*gamma^nu
dirac "gamma^mu gamma^nu gamma^rho gamma_mu"             # 4*g(nu,rho)
dirac "gamma^mu gamma^nu gamma^rho gamma^sigma" --trace  # 4(g g - g g + g g)
dirac "gamma5 gamma^mu gamma^nu gamma^rho gamma^sigma" --trace   # -4i eps
dirac "pslash pslash"                                    # p^2
```

### 记号

| 写法 | 含义 |
|---|---|
| `gamma^mu` / `g^mu` / `γ^mu` | γ^μ |
| `gamma_mu` / `g_mu` | γ_μ |
| `gamma5` / `g5` | γ5 |
| `pslash` / `qslash` | p̸ = γ^μ p_μ |
| `mu nu rho sigma lam tau alpha beta`（或 `μ ν ρ σ λ τ α β`） | 指标名 |

分隔用空格或 `*` 都行。`--trace` 建议放**最后**：`--trace EXPR` 会把表达式当成选项值
（虽然 `c_dirac` 做了兼容，别依赖它）。

### 算法

闭式公式 + 递归，不是规则表：

- 一对缩并指标夹 k 个 γ：**k 奇 → −2·(反转序)**，**k 偶 → 4·Σ(±g·g 全完美匹配)**。
  k=1 给 −2γ^ν，k=2 给 4g^{νρ}，k=4 自动生成与迹同结构的三个 g 项。
- 多层缩并：反复取**最内层**配对（保证中间段是纯 γ 串），替换后继续。
- 迹：`tr(γ^{a1}…γ^{an}) = Σ_{j≥2} (−1)^j g^{a1 aj} tr(rest)`，`tr(1)=4`，奇数个 γ 为 0。
- γ5：先整体推到末尾（每个穿过 n 个 γ 给 (−1)^n），两两相消；
  `tr(γ5 γ^a γ^b γ^c γ^d) = −4i ε^{abcd}`，少于 4 个 γ 为 0。
- slash：把 `p̸` 当"指标 p"，`g^{pq}→p·q`、`g^{pν}→p^ν`、`g^{pp}→p²`。

### 验证方式

不是"看着像对"。用显式 4×4 Dirac 矩阵交叉验证：4 类迹（2γ / 4γ / 6γ / γ5+4γ）
与 4 类缩并（k=1…5），各 12 组随机指标，共 **96 次矩阵级比对全过**。
改了引擎先跑 `python scripts/verify_dirac.py`（退出码非 0 就是退化）。

### 边界

1. **`tr(γ5 × 6 个以上 γ)` 未实现**——直接报错，不给半截答案；
2. **形式不唯一**：k 偶给的是展开的 g 组合，中间指标重合时不会自动合并同类项；
3. **约定**：`tr I = 4`、`ε^{0123}=+1`、`tr(γ5 γ^a γ^b γ^c γ^d) = −4iε^{abcd}`（Peskin）；
   换约定（如 West）符号要自己翻；
4. 只认**上/下配对**；`gamma^mu gamma^mu`（同号重复）不缩并，原样保留。

## SymEngine 怎么用：只有一种情况真的更快

实测（i5-13500H，SymPy 1.14 / SymEngine 0.15）：

| 路径 | `expand((1+x)^2000)` 耗时 |
|---|---:|
| 纯 SymPy | ~1.0 s |
| SymEngine 内部展开 | **2.4 ms** |
| SymEngine 展开后再 `sympify` 回 SymPy | **153 s**（对 2001 项做 O(n²) 展平） |

所以 **不要**把 SymPy 表达式送进 SymEngine 再转回来——转换开销吃掉全部收益，还会爆炸。
正确用法是 `se` 子命令：字符串进、字符串出，全程留在 SymEngine。

```bash
mathkit.py se expand "(x+1)^2000"      # 2 ms 级，输出字符串
mathkit.py se diff "sin(x)*exp(x)" x 2
mathkit.py se subs "x^2+y" "x=2,y=3"
mathkit.py se series "sin(x)" x 10
mathkit.py se eval "exp(-x^2)" "x=0.5"
```

`--engine symengine expand EXPR` 等价于 `se expand EXPR`（方便记忆）。其余命令一律 SymPy：
SymEngine 没有假设系统（`Symbol(..., positive=True)` 的 `is_positive` 返回 `None`），
也没有 `integrate/factor/solve`。

## 三条硬规则

1. **结果里残留 `Integral`/`Sum`/`Integrate` 就是没算完**——不要当答案用（尤其 ginac 的 `integrate`，见
   `references/symengine_notes.md`）。
2. **假设决定答案**：不给 `positive=True`，`sqrt(x**2)` 不会化简；分支切割错误九成源于缺假设。用 `--sym`。
3. **静默错误要用数值兜底**：`limit`/`integrate`/`residue` 都出现过返回 0 或错值；委托命令自带对账，
   自己写的推导用 `check "LHS==RHS"` 或 `evalf` 复核。

## 委托机制

以下命令不在本文件实现，直接转给 `sympy-math/scripts/math_tool.py`（同一进程内调用，输出保持原格式）：
`integrate subs-integrate limit series summation product residue residues dsolve ode-series
multi-integrate converge fourier orthdiag dist expect var mgf mle ci check evalf latex nsimplify
gammasimp combsimp hyperexpand matrix matrix-eqsolve numeric-integrate subs coeff solve diff`。

找不到 `math_tool.py` 时（`MATH_TOOL_PATH` 未设且默认路径不存在），这些命令报错并提示安装/修复路径；
其余命令仍可用。

## 自检与排错

```bash
python "/c/Users/联想/.workbuddy/skills/mathkit/scripts/selfcheck.py"   # 85 条用例，全过才算健康
```

常见报错：
- `未找到 sympy-math 的 math_tool.py` → 检查 `~/.workbuddy/skills/sympy-math/scripts/math_tool.py` 是否存在。
- 中文乱码 → 终端需 UTF-8；脚本已 `reconfigure(encoding="utf-8")`。
- 结果被截断 → 超过 3000 字符只显示前 3000；用 `--json` 或重定向取全文。

## 参考文件

- `references/sympy_api_map.md` — 能力 → SymPy API → mathkit 命令对照，含官方文档章节定位。
- `references/symengine_notes.md` — SymEngine 能力边界、转换开销实测、与 ginac/Symbolica 的对比结论。
- `references/kdense_gap.md` — 与 K-Dense `sympy` skill 逐条对照的差异清单（补了什么、故意不做什么）。
