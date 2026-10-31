# SymEngine / ginac / Symbolica 实测笔记

本机：Windows / Python 3.13.14 / SymPy 1.14.0 / SymEngine 0.15.0（主环境已装）。
ginac、Symbolica 只做过对比测试（隔离目录 `caslibs/`），**不是 mathkit 的依赖**。

## 1. SymEngine 能做什么、不能做什么

有：`expand`、`diff`、`subs`、`series`、`Matrix`（det/solve/inv）、`Lambdify`、`count_ops`、初等函数。
没有：`integrate`、`solve`、`factor`、`gcd`、`resultant`，以及**整个假设系统**。

```python
import symengine as se
se.Symbol('y', positive=True).is_positive      # None —— 关键字被静默忽略
se.sqrt(se.Symbol('y', positive=True)**2)      # sqrt(y**2) —— 不会化简
```

因此 SymEngine 不能当 SymPy 的替代品，只能当**纯表达式变形**的加速器。

## 2. 关键实测：往返转换会吃掉全部收益

`expand((1+x)^n)`，同一台机器：

| 路径 | n=200 | n=1000 | n=2000 |
|---|---:|---:|---:|
| 纯 SymPy | 108 ms | 424 ms | 993 ms |
| SymEngine 内部展开 | 0.3 ms | 0.9 ms | 2.4 ms |
| **展开后转回 SymPy**（`sympify(str(...))`） | +84 ms | +1805 ms | **+152 s** |

结论：**SymPy 表达式 → SymEngine → SymPy** 这种来回，净收益为负，规模大时还会爆炸
（2001 项的多项式转回 SymPy 时要 O(n²) 展平）。

所以 mathkit 的 `se` 子命令**只返回字符串**，不转回 SymPy：

```bash
mathkit.py se expand "(x+1)^2000"    # 2 ms，输出字符串
mathkit.py se diff "sin(x)*exp(x)" x 2
```

需要继续做符号运算时，小规模（几百项以内）再 `sympify`，大规模就留在 SymEngine 侧或直接导出文本。

## 3. 与 ginacsympy 的同题对比（min of 3，每进程一次）

| 任务 | SymEngine | ginacsympy | 谁快 |
|---|---:|---:|---|
| expand (1+x)^200 | 0.258 ms | 0.973 ms | SymEngine 3.8× |
| expand (1+x)^2000 | 3.00 ms | 15.84 ms | SymEngine 5.3× |
| expand (1+x+y)^25 | 0.377 ms | 2.118 ms | SymEngine 5.6× |
| diff 5 阶 | 0.111 ms | 0.210 ms | SymEngine 1.9× |
| subs（101 项） | 0.128 ms | 0.068 ms | ginac 1.9× |
| 5×5/8×8 符号行列式 | 0.052/0.093 ms | 0.067/0.101 ms | 持平 |
| factor(x^60-1) | 不支持 | 7.9 ms（SymPy 71 ms） | ginac 独有 |

## 4. 两个容易误判的地方

1. **ginac 的 `integrate` 是半成品**：`∫x²dx`、`∫dx/(1+x²)`、`∫sin x dx` 能算，
   但 `∫dx/(1+x⁴)` 原样返回 `Integrate(...)`，`∫dx/(1+x⁵)` 只做出 `1/5·ln(1+x)` 一项、其余仍包在
   `Integrate` 里。**判断标准：`"Integrate" in str(result)` 就说明没算完。**
2. **Symbolica 的 `to_sympy()` 返回字符串**（docstring 明说 "Convert the expression into a
   Sympy-parsable string"），且符号带 `python::` 前缀，`set_namespace('')` 不被允许；
   输入解析器只认 `^` 不认 `**`。它定位是超大表达式/模式重写，不是 SymPy 后端。

## 5. 选型结论

| 需求 | 选择 |
|---|---|
| 给 SymPy 加速、可产品化 | **SymEngine**（MIT；但只用 `se` 路径，别来回转） |
| 大整数/多项式因式分解 | ginac（GPLv2+，商业分发前必须评估） |
| 超大规模表达式、模式重写 | Symbolica（专有，学术单核免费） |
| 需要假设、积分、ODE、物理模块 | 只能用 SymPy |

## 6. mathkit 里的落地方式

- 默认 `sympy`：语义正确优先。
- `se` / `--engine symengine`：字符串进出，用于巨大展开、批量求导、数值代入。
- 不做自动引擎切换：实测收益不稳定，自动切换反而让「为什么这次结果不一样」难以排查。
