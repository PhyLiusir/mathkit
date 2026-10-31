#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mathkit -- SymPy + SymEngine 统一符号计算 CLI（mathkit skill 的主入口）。

设计分三层：
  1) 路由层：命令 -> 处理器；math_tool 里已加固的命令（integrate/limit/residue/
     dsolve/summation/...）直接委托，避免重复实现与重复踩坑。
  2) 能力层：本文件新增代数、线代、数论、统计、单位、物理、逻辑、代码生成等域。
  3) 引擎层：expand/diff/simplify 可选 SymEngine 加速，带数值抽检与自动回退。

用法示例（Windows Git Bash，python 用托管 venv 绝对路径）：
  PY="/c/Users/联想/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
  "$PY" "$HOME/.workbuddy/skills/mathkit/scripts/mathkit.py" expand "(x+1)^100"
  "$PY" ... mathkit.py linalg eigen "[[1,2],[2,1]]"
  "$PY" ... mathkit.py --json factorint 2^61-1
"""

from __future__ import annotations

import io
import contextlib
import json
import os
import sys
import time

import sympy as sp
from sympy import S

# ---------------------------------------------------------------- 输出编码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---------------------------------------------------------------- 复用 math_tool
_CORE = None
_CORE_DIRS = [
    os.environ.get("MATH_TOOL_PATH", ""),
    os.path.expanduser("~/.workbuddy/skills/sympy-math/scripts"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "sympy-math", "scripts"),
]
for _d in _CORE_DIRS:
    if _d and os.path.isfile(os.path.join(_d, "math_tool.py")):
        sys.path.insert(0, os.path.abspath(_d))
        try:
            import math_tool as _CORE  # noqa: N816
            break
        except Exception:
            _CORE = None

# ---------------------------------------------------------------- 可选引擎
try:
    import symengine as _se
    HAS_SE = True
except Exception:
    _se = None
    HAS_SE = False

ENGINE = "auto"


# ---------------------------------------------------------------- 解析工具
def _parse(s: str):
    """优先用 math_tool.parse（含 ^->**、教材别名、有理化、--sym 假设）。"""
    if _CORE is not None:
        return _CORE.parse(s)
    from sympy.parsing.sympy_parser import (
        parse_expr, standard_transformations, convert_xor,
        implicit_multiplication_application,
    )
    tf = standard_transformations + (convert_xor, implicit_multiplication_application)
    return parse_expr(s.replace("^", "**"), transformations=tf)


def _sym(name: str):
    name = name.strip()
    if _CORE is not None:
        return _CORE._sym(name)
    return sp.Symbol(name)


def _matrix(s: str):
    if _CORE is not None:
        return _CORE._sympify_matrix(s)
    return sp.Matrix(sp.sympify(s))


def _split(s: str, sep: str = ","):
    if _CORE is not None:
        return _CORE.split_top(s, sep)
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _vars(spec: str):
    return [_sym(v) for v in _split(spec)] if spec else []


# ---------------------------------------------------------------- 引擎层
def _se_expr(text):
    """字符串直接进 SymEngine（避免先造 SymPy 对象再转换）。"""
    return _se.sympify(text.replace("^", "**"))


def c_se(out, pos, opt):
    """全程留在 SymEngine：结果不转回 SymPy（大表达式转回会被 O(n²) 展平拖死）。"""
    if not HAS_SE:
        return out.finish(ok=False, error="未安装 symengine，先 pip install symengine")
    op = pos[0].lower()
    e = _se_expr(pos[1])
    if op == "expand":
        r = e.expand()
    elif op == "diff":
        v = _se.Symbol(pos[2])
        n = int(pos[3]) if len(pos) > 3 else 1
        r = _se.diff(e, v, n)
    elif op == "subs":
        r = e
        for kv in _split(pos[2]):
            k, v = kv.split("=", 1)
            r = r.subs(_se.Symbol(k.strip()), _se.sympify(v.strip()))
    elif op == "series":
        v = _se.Symbol(pos[2])
        n = int(pos[3]) if len(pos) > 3 else 10
        r = _se.series(e, v, 0, n)
    elif op == "eval":
        r = e
        for kv in _split(pos[2]):
            k, v = kv.split("=", 1)
            r = r.subs(_se.Symbol(k.strip()), _se.sympify(v.strip()))
        r = r.evalf()
    else:
        return out.finish(ok=False, error=f"未知 se 操作: {op}（expand/diff/subs/series/eval）")
    out.engine = "symengine"
    text = str(r)
    if out.as_json:
        return out.finish(text, note="SymEngine 字符串结果（未转回 SymPy）")
    print(f"结果: {text}")
    print("引擎: symengine（字符串结果，未转回 SymPy）")
    return 0


# ---------------------------------------------------------------- 结果输出
class Out:
    def __init__(self, cmd, as_json=False):
        self.cmd = cmd
        self.as_json = as_json
        self.t0 = time.perf_counter()
        self.engine = "sympy"

    def finish(self, result=None, ok=True, error=None, note=None, extra=None):
        ms = (time.perf_counter() - self.t0) * 1000
        text = "" if result is None else str(result)
        shown = text if len(text) <= 3000 else (
            text[:3000] + f"...(已截断，共 {len(text)} 字符；用 --json 取全文)")
        latex = ""
        if ok and text and len(text) < 4000:
            try:
                latex = sp.latex(result)
            except Exception:
                latex = ""
        if self.as_json:
            payload = {
                "ok": ok, "cmd": self.cmd, "result": text, "latex": latex,
                "engine": self.engine, "ms": round(ms, 1),
                "error": error, "note": note,
            }
            if extra:
                payload.update(extra)
            print(json.dumps(payload, ensure_ascii=False))
        elif ok:
            if note:
                print(f"注: {note}")
            print(f"结果: {shown}")
            if latex:
                print(f"LaTeX: ${latex}$")
            print(f"引擎: {self.engine} | {ms:.0f} ms")
        else:
            print(f"错误: {error}")
        return 0 if ok else 1


def _emit(out, value, engine=None):
    if engine:
        out.engine = engine
    return out.finish(value)


# ---------------------------------------------------------------- 通用装饰
def _delegate(out, argv):
    """把命令交给 math_tool 执行，保留其原有输出格式。"""
    if _CORE is None:
        return out.finish(ok=False, error="未找到 sympy-math 的 math_tool.py，无法执行该命令")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = _CORE.main(argv)
    except SystemExit as e:
        rc = int(e.code or 0)
    text = buf.getvalue().rstrip()
    if out.as_json:
        return out.finish(text, ok=(rc == 0), error=None if rc == 0 else text,
                          extra={"delegated": True})
    print(text)
    return rc


# ================================================================ 代数
def c_simplify(out, pos, opt):
    return _emit(out, sp.simplify(_parse(pos[0])))


def c_expand(out, pos, opt):
    if ENGINE == "symengine" and HAS_SE:
        return c_se(out, ["expand", pos[0]], opt)
    return _emit(out, sp.expand(_parse(pos[0])))


def c_factor(out, pos, opt):
    expr = _parse(pos[0])
    mod = opt.get("modulus")
    r = sp.factor(expr, modulus=int(mod)) if mod else sp.factor(expr)
    return _emit(out, r)


def _simple(name, fn):
    def handler(out, pos, opt):
        args = [_parse(p) for p in pos]
        return _emit(out, fn(*args))
    return handler


def c_collect(out, pos, opt):
    return _emit(out, sp.collect(_parse(pos[0]), _sym(pos[1])))


def c_rewrite(out, pos, opt):
    target = pos[1].strip()
    fn = getattr(sp, target, None)
    if fn is None:
        return out.finish(ok=False, error=f"未知重写目标: {target}")
    return _emit(out, _parse(pos[0]).rewrite(fn))


def c_gcd(out, pos, opt):
    a, b = _parse(pos[0]), _parse(pos[1])
    return _emit(out, sp.gcd(a, b))


def c_lcm(out, pos, opt):
    return _emit(out, sp.lcm(_parse(pos[0]), _parse(pos[1])))


def c_resultant(out, pos, opt):
    var = _sym(pos[2]) if len(pos) > 2 else _free_var(pos[0], pos[1])
    return _emit(out, sp.resultant(_parse(pos[0]), _parse(pos[1]), var))


def c_discriminant(out, pos, opt):
    var = _sym(pos[1]) if len(pos) > 1 else _free_var(pos[0])
    return _emit(out, sp.discriminant(_parse(pos[0]), var))


def _free_var(*exprs):
    syms = set()
    for e in exprs:
        syms |= set(_parse(e).free_symbols)
    if len(syms) != 1:
        raise RuntimeError(f"无法自动判定变量（候选: {sorted(map(str, syms))}），请显式给一个")
    return syms.pop()


def c_groebner(out, pos, opt):
    polys = [_parse(p) for p in _split(pos[0])]
    vs = _vars(pos[1])
    order = opt.get("order", "lex")
    G = sp.groebner(polys, *vs, order=order)
    return _emit(out, G)


def c_roots(out, pos, opt):
    poly = _parse(pos[0])
    var = _sym(pos[1]) if len(pos) > 1 else _free_var(pos[0])
    return _emit(out, sp.roots(poly, var))


def c_nroots(out, pos, opt):
    poly = _parse(pos[0])
    n = int(opt.get("n", 15))
    return _emit(out, sp.nroots(poly, n=n, maxsteps=200))


def c_minpoly(out, pos, opt):
    var = _sym(pos[1]) if len(pos) > 1 else sp.Symbol("x")
    return _emit(out, sp.minimal_polynomial(_parse(pos[0]), var))


def c_degree(out, pos, opt):
    poly = _parse(pos[0])
    var = _sym(pos[1]) if len(pos) > 1 else _free_var(pos[0])
    p = sp.Poly(poly, var)
    return _emit(out, f"degree={p.degree()}  lc={p.LC()}  coeffs={p.all_coeffs()}")


# ================================================================ 微积分（新增）
def c_laplace(out, pos, opt):
    from sympy.integrals.transforms import laplace_transform
    expr = _parse(pos[0])
    t = _sym(pos[1]) if len(pos) > 1 else _free_var(pos[0])
    s = sp.Symbol("s")
    return _emit(out, laplace_transform(expr, t, s, noconds=True))


def c_ilaplace(out, pos, opt):
    from sympy.integrals.transforms import inverse_laplace_transform
    expr = _parse(pos[0])
    s = _sym(pos[1]) if len(pos) > 1 else _free_var(pos[0])
    t = sp.Symbol("t", positive=True)
    return _emit(out, inverse_laplace_transform(expr, s, t).rewrite(sp.Heaviside))


def c_fourier_transform(out, pos, opt):
    from sympy.integrals.transforms import fourier_transform
    expr = _parse(pos[0])
    x = _sym(pos[1]) if len(pos) > 1 else _free_var(pos[0])
    k = sp.Symbol("k")
    return _emit(out, fourier_transform(expr, x, k))


# ================================================================ 留数定理（围道积分）
def _cplx(p):
    """复数点 -> python complex；不可数值化返回 None。"""
    try:
        return complex(sp.N(p, 20))
    except Exception:
        return None


def _dedup_poles(cands):
    """按数值距离去重（1e-12 相对），保留先出现的符号形式。"""
    kept = []
    for p, v in cands:
        if all(abs(v - w) > 1e-12 * max(1.0, abs(v)) for _, w in kept):
            kept.append((p, v))
    return kept


def _num_residues_sum(f, var, pts):
    """逐极点小圆围道求数值留数并求和。

    小圆半径取到最近邻极点距离的 0.3 倍，保证圈内只有一个奇点；圆上被积函数光滑，
    mpmath 不会像大围道那样因振荡而失去精度。每点自带 r / (r/2) 双半径自检。
    """
    import mpmath as mp
    try:
        fa = sp.lambdify(var, f, "mpmath")
    except Exception as e:
        return None, "lambdify 失败: %s" % e
    ii = mp.mpc(0, 1)
    tot = mp.mpc(0, 0)
    for pc in pts:
        others = [abs(pc - q) for q in pts if abs(pc - q) > 1e-9]
        d = min(others) if others else max(abs(pc), 1.0)
        r = 0.3 * min(d, max(abs(pc), 1.0))

        def ring(rr, pc=pc):
            def g(t):
                z = mp.mpc(pc.real, pc.imag) + rr * mp.exp(ii * t)
                return fa(z) * ii * rr * mp.exp(ii * t)
            return mp.quad(g, [0, 2 * mp.pi]) / (2 * ii * mp.pi)

        try:
            v1, v2 = ring(r), ring(r / 2)
        except Exception as e:
            return None, "极点 %s 数值围道失败: %.120s" % (pc, e)
        if abs(v1 - v2) > 1e-6 * max(1.0, abs(v1)):
            return None, "极点 %.4g 处双半径不一致（%.3g / %.3g）" % (abs(pc), abs(v1), abs(v2))
        tot += v1
    return complex(tot), None


def _close(a, b, tol=1e-8):
    """相对一致判定：返回 (是否一致, 相对偏差)。任一方含自由符号时先试符号等价，
    不可数值化则返回 (None, None)（由 _verdict 显示为「无法比较」，不冒充 FAIL）。"""
    try:
        if getattr(a, "free_symbols", None) or getattr(b, "free_symbols", None):
            if sp.simplify(a - b) == 0:
                return True, 0.0
    except Exception:
        pass
    try:
        na, nb = complex(sp.N(a, 20)), complex(sp.N(b, 20))
        d = abs(na - nb)
        scale = max(abs(na), abs(nb), 1e-12)
        return d <= tol * scale + 1e-13, d / scale
    except Exception:
        return None, None


def _verdict(ok, rel):
    """把三态判定转成可读文本（ok=None 表示无法比较，绝不冒充 FAIL）。"""
    if ok is None:
        return "无法比较（含自由符号），跳过"
    return "PASS" if ok else "FAIL（相对偏差 %.1e）" % rel


def c_contour(out, pos, opt):
    """contour EXPR VAR --domain upper|lower|unit|all [--trig] [--no-check]

    留数定理算围道积分：枚举极点 -> 按围道筛选 -> 2πi·ΣRes -> 双通道对账。
    --trig 先做 z=e^{iθ} 代换，把 ∫₀^{2π} F(θ)dθ 变成 |z|=1 上的围道积分。
    """
    if len(pos) < 2:
        raise ValueError(
            "用法: contour EXPR VAR --domain upper|lower|unit|all [--trig] [--no-check]")
    f0 = _parse(pos[0])
    var0 = _sym(pos[1])
    domain = str(opt.get("domain", "upper")).lower()
    if domain not in ("upper", "lower", "unit", "all"):
        raise ValueError("--domain 只能是 upper / lower / unit / all")
    do_trig = bool(opt.get("trig"))
    do_check = not bool(opt.get("no-check"))
    lines = []
    warn = []

    f, var = f0, var0
    target = None
    if do_trig:
        z = sp.Symbol("z")
        g = sp.expand_trig(f0)
        g = g.subs({sp.cos(var0): (z + 1 / z) / 2,
                    sp.sin(var0): (z - 1 / z) / (2 * sp.I)})
        f = sp.simplify(g / (sp.I * z))
        var = z
        if domain in ("upper", "all"):
            domain = "unit"
        target = (f0, var0, sp.S.Zero, 2 * sp.pi)
        lines.append("三角代换 z=e^{iθ}: ∫₀^{2π}F(θ)dθ = ∮_{|z|=1} F·dz/(iz)")
        lines.append("  → 被积函数 $%s$" % sp.latex(f))
    elif domain in ("upper", "lower"):
        target = (f0, var0, -sp.oo, sp.oo)

    # ---- 极点：分母的根（含分子可去奇点，由 Res=0 自证）
    fr = sp.cancel(sp.together(f))
    num, den = fr.as_numer_denom()
    periodic = {sp.sin, sp.cos, sp.tan, sp.cot, sp.sec, sp.csc,
                sp.sinh, sp.cosh, sp.tanh, sp.coth}
    if den.has(*periodic):
        warn.append("分母含周期函数 %s：极点为无穷族，枚举只覆盖解出的有限个"
                    % sorted({fn.__name__ for fn in periodic if den.has(fn)}))
    rts = []
    try:
        rts = sp.solve(sp.Eq(den, 0), var)
    except Exception as e:
        warn.append("极点求解失败（%s）" % e)
    cands = [(sp.simplify(p), _cplx(p)) for p in rts]
    poles = _dedup_poles([(p, v) for p, v in cands if v is not None])
    if not poles:
        return out.finish(ok=False, error="未能解出极点（分母可能含超越/复合结构）；"
                                         "本质奇点/支点请用 residue 逐点求")

    def inside(v):
        if domain == "upper":
            return v.imag > 1e-12
        if domain == "lower":
            return v.imag < -1e-12
        if domain == "unit":
            return abs(v) < 1 - 1e-12
        return True

    rows, total = [], S.Zero
    for p, v in poles:
        tag = []
        try:
            r = sp.simplify(sp.residue(f, var, p))
        except Exception:
            r = None
            tag.append("求留数失败")
        if r is not None and r == 0:
            try:
                if sp.limit(f, var, p).is_finite:
                    tag.append("可去奇点")
            except Exception:
                pass
        inn = inside(v)
        if r is not None and inn:
            total += r
        rows.append((p, v, r, inn, tag))

    # ---- 输出极点表
    lines.append("极点（共 %d 个）:" % len(poles))
    for p, v, r, inn, tag in rows:
        rs = "—" if r is None else "$%s$" % sp.latex(r)
        extra = ("，" + "，".join(tag)) if tag else ""
        lines.append("  %s = $%s$  Res = %s  [%s%s]"
                     % (var.name, sp.latex(p), rs, "围道内" if inn else "外", extra))

    total = sp.simplify(total)
    coeff = 2 * sp.pi * sp.I * (-1 if domain == "lower" else 1)
    value = sp.simplify(coeff * total)
    lines.append("Σ_{围道内} Res = $%s$" % sp.latex(total))
    lines.append("留数定理: ∮ = %s·ΣRes = $%s$"
                 % ("-2πi" if domain == "lower" else "2πi", sp.latex(value)))
    if domain == "all":
        warn.append("--domain all 的 2πi·ΣRes = -Res_∞，一般不是任何实积分，仅供核对用")
    on_axis = []
    if domain in ("upper", "lower"):
        on_axis = [p for p, v, _, _, _ in rows if abs(v.imag) <= 1e-12]
        if on_axis:
            warn.append("实轴上有极点 %s：半圆围道须缩进（或取主值），"
                        "2πi·ΣRes 不等于原积分" % ", ".join(str(p) for p in on_axis))
    # cos/sin + 未声明符号的参数：半圆闭合不合法（cos(az) 一侧指数增长）
    if domain in ("upper", "lower") and f.has(sp.cos, sp.sin):
        free = [s for s in f.free_symbols if s != var]
        undeclared = [s for s in free if not s.is_positive]
        if undeclared:
            warn.append("被积函数含 cos/sin 且参数 %s 未给正性假设：沿半圆闭合时 "
                        "cos(a·z) 在 Im z→±∞ 一侧按 e^{|a|R} 指数增长，Jordan 引理不成立，"
                        "结果（如 π·cosh(a)）通常只对 a=0 成立。请改写为 exp(I*a*x) 并加 "
                        "--sym \"%s:positive\"" % (", ".join(map(str, undeclared)),
                                                   undeclared[0]))

    # ---- 对账 1：真实积分的符号封闭形式
    if do_check and target is not None:
        te, tv, lo, hi = target
        try:
            sym_int = sp.integrate(te, (tv, lo, hi))
        except Exception as e:
            sym_int, lines = None, lines + [".. 符号积分对账失败（%s）" % type(e).__name__]
        if sym_int is not None:
            if sym_int.has(sp.Integral):
                lines.append(".. 符号积分对账: 原积分无封闭形式，跳过")
            else:
                sym_int = sp.simplify(sym_int)
                ok, rel = _close(sym_int, value)
                lines.append("对账① 直接符号积分 = $%s$ → %s"
                             % (sp.latex(sym_int), _verdict(ok, rel)))
                if ok is False:
                    warn.append("与直接符号积分不一致，优先信任直接积分并检查围道/极点筛选")

    # ---- 对账 2：围道内每个极点逐一做小圆数值围道，与符号留数比对
    if do_check and domain != "all":
        pts_in = [v for _, v, r, inn, _ in rows if inn and r is not None]
        if pts_in:
            num_sum, err = _num_residues_sum(f, var, pts_in)
            if num_sum is None:
                lines.append(".. 数值留数对账跳过: %s" % err)
                warn.append("留数未经数值验证（%s），符号结果请自查" % err)
            else:
                ok, rel = _close(total, num_sum)
                lines.append("对账② 围道内 %d 点小圆数值留数 Σ = $%s$ → %s"
                             % (len(pts_in), sp.latex(sp.N(num_sum, 12)),
                                _verdict(ok, rel)))

    # ---- 对账 3：全平面留数 + Res_∞ = 0，检查极点是否漏枚举
    if do_check:
        r_inf = None
        try:
            r_inf = _CORE._res_at_oo(f, var) if _CORE is not None else sp.residue(f, var, sp.oo)
        except Exception:
            r_inf = None
        if r_inf is None:
            lines.append(".. 对账③ 跳过：f 在 ∞ 的留数算不出（本质奇点），极点完整性未验证")
        else:
            s_all = S.Zero
            for _, _, r, _, _ in rows:
                if r is not None:
                    s_all += r
            ok, rel = _close(s_all + r_inf, 0)
            lines.append("对账③ Σ_all Res + Res_∞ = $%s$ → %s（极点枚举%s）"
                         % (sp.latex(sp.simplify(s_all + r_inf)), _verdict(ok, rel),
                            "完整" if ok else ("可能有遗漏" if ok is False else "未验证")))

    if warn:
        lines.append("")
        for w in warn:
            lines.append("⚠️ " + w)

    if out.as_json:
        return out.finish(value, note="\n".join(lines), extra={
            "domain": domain,
            "poles": [str(p) for p, _, _, _, _ in rows],
            "residues": [None if r is None else str(r) for _, _, r, _, _ in rows],
            "usable": not bool(on_axis),
        })
    print("\n".join(lines))
    if on_axis:
        print("结果: %s  ← 不可用：实轴有极点，须缩进绕行后再取极限" % value)
    else:
        print("结果: %s" % value)
    print("LaTeX: $%s$" % sp.latex(value))
    return 0


# ================================================================ 求解（新增）
def c_solveset(out, pos, opt):
    eq = _parse(pos[0])
    var = _sym(pos[1])
    dom = {"reals": S.Reals, "complex": S.Complexes, "integers": S.Integers}.get(
        opt.get("domain", "complex").lower(), S.Complexes)
    if isinstance(eq, sp.Eq):
        eq = eq.lhs - eq.rhs
    return _emit(out, sp.solveset(eq, var, domain=dom))


def c_linsolve(out, pos, opt):
    eqs = [_parse(p) for p in _split(pos[0])]
    eqs = [e.lhs - e.rhs if isinstance(e, sp.Eq) else e for e in eqs]
    return _emit(out, sp.linsolve(eqs, *_vars(pos[1])))


def c_nonlinsolve(out, pos, opt):
    eqs = [_parse(p) for p in _split(pos[0])]
    eqs = [e.lhs - e.rhs if isinstance(e, sp.Eq) else e for e in eqs]
    return _emit(out, sp.nonlinsolve(eqs, *_vars(pos[1])))


def c_rsolve(out, pos, opt):
    eq = _parse(pos[0])
    name = pos[1].strip()
    if "(" in name:
        fn = _parse(name)
    else:
        n = sp.Symbol("n")
        free = sorted(eq.free_symbols, key=str)
        if free and n not in eq.free_symbols:
            n = free[0]
        fn = sp.Function(name)(n)
    return _emit(out, sp.rsolve(eq, fn))


def c_diophantine(out, pos, opt):
    eq = _parse(pos[0])
    if isinstance(eq, sp.Eq):
        eq = eq.lhs - eq.rhs
    vs = _vars(pos[1]) if len(pos) > 1 else None
    return _emit(out, sp.diophantine(eq) if not vs else sp.diophantine(eq, *vs))


def c_ineq(out, pos, opt):
    expr = _parse(pos[0])
    var = _sym(pos[1]) if len(pos) > 1 else _free_var(pos[0])
    return _emit(out, sp.solve_univariate_inequality(expr, var))


def c_nsolve(out, pos, opt):
    eq = _parse(pos[0])
    var = _sym(pos[1])
    x0 = float(opt.get("x0", 1.0))
    if isinstance(eq, sp.Eq):
        eq = eq.lhs - eq.rhs
    return _emit(out, sp.nsolve(eq, var, x0))


# ================================================================ 线性代数
def c_linalg(out, pos, opt):
    op = pos[0].lower()
    M = _matrix(pos[1])
    if op == "solve":
        b = _matrix(pos[2]) if len(pos) > 2 else None
        return _emit(out, M.solve(b))
    if op == "det":
        return _emit(out, M.det())
    if op == "inv":
        return _emit(out, M.inv())
    if op == "rank":
        return _emit(out, M.rank())
    if op == "rref":
        return _emit(out, M.rref())
    if op == "trace":
        return _emit(out, M.trace())
    if op == "transpose":
        return _emit(out, M.T)
    if op == "nullspace":
        return _emit(out, M.nullspace())
    if op == "columnspace":
        return _emit(out, M.columnspace())
    if op == "rowspace":
        return _emit(out, M.rowspace())
    if op == "charpoly":
        return _emit(out, M.charpoly().as_expr())
    if op in ("eigen", "eigenvals"):
        return _emit(out, M.eigenvals())
    if op in ("eigenvects",):
        return _emit(out, M.eigenvects())
    if op == "jordan":
        P, J = M.jordan_form()
        return _emit(out, f"P={P}\nJ={J}")
    if op == "qr":
        Q, R = M.QRdecomposition()
        return _emit(out, f"Q={Q}\nR={R}")
    if op == "cholesky":
        return _emit(out, M.cholesky())
    if op == "svd":
        return _emit(out, M.singular_values())
    if op == "pinv":
        return _emit(out, M.pinv())
    if op == "expm":
        return _emit(out, M.exp())
    if op == "norm":
        return _emit(out, M.norm())
    if op in ("cond", "condition_number"):
        return _emit(out, M.condition_number())
    if op == "pow":
        return _emit(out, M ** int(pos[2]))
    if op == "diagonalize":
        P, D = M.diagonalize()
        return _emit(out, f"P={P}\nD={D}")
    if op == "lstsq":
        return _emit(out, M.solve_least_squares(_matrix(pos[2])))
    if op == "hstack":
        return _emit(out, M.row_join(_matrix(pos[2])))
    if op == "vstack":
        return _emit(out, M.col_join(_matrix(pos[2])))
    if op == "conjugate":
        return _emit(out, M.conjugate())
    if op == "nullity":
        return _emit(out, len(M.nullspace()))
    if op == "gramschmidt":
        vecs = [sp.Matrix(r) for r in M.tolist()]
        return _emit(out, sp.Matrix.orthogonalize(*vecs, normalize=bool(opt.get("normalize"))))
    if op == "adjugate":
        return _emit(out, M.adjugate())
    return out.finish(ok=False, error=f"未知 linalg 操作: {op}")


# ================================================================ 数论
def c_nt(out, pos, opt):
    from sympy import ntheory as nt
    op = pos[0].lower()
    if op == "factorint":
        return _emit(out, sp.factorint(int(_parse(pos[1]))))
    if op == "isprime":
        return _emit(out, sp.isprime(int(_parse(pos[1]))))
    if op == "primes":
        return _emit(out, list(sp.primerange(int(pos[1]), int(pos[2]))))
    if op == "totient":
        return _emit(out, sp.totient(int(pos[1])))
    if op == "divisors":
        return _emit(out, sp.divisors(int(pos[1])))
    if op == "nextprime":
        return _emit(out, sp.nextprime(int(pos[1])))
    if op == "primepi":
        return _emit(out, sp.primepi(int(pos[1])))
    if op == "crt":
        r = [int(v) for v in _split(pos[1])]
        m = [int(v) for v in _split(pos[2])]
        from sympy.ntheory.modular import crt
        return _emit(out, crt(m, r))
    if op == "cf":
        from sympy.ntheory.continued_fraction import continued_fraction
        return _emit(out, continued_fraction(_parse(pos[1])))
    if op == "binomial":
        return _emit(out, sp.binomial(int(pos[1]), int(pos[2])))
    if op == "partitions":
        from sympy.functions.combinatorial.numbers import partition
        return _emit(out, partition(int(pos[1])))
    return out.finish(ok=False, error=f"未知数论操作: {op}")


# ================================================================ 统计
_DISTS = {
    "normal": lambda a: sp.stats.Normal("X", sp.sympify(a[0]), sp.sympify(a[1])),
    "uniform": lambda a: sp.stats.Uniform("X", sp.sympify(a[0]), sp.sympify(a[1])),
    "exp": lambda a: sp.stats.Exponential("X", sp.sympify(a[0])),
    "poisson": lambda a: sp.stats.Poisson("X", sp.sympify(a[0])),
    "binomial": lambda a: sp.stats.Binomial("X", int(a[0]), sp.sympify(a[1])),
    "geom": lambda a: sp.stats.Geometric("X", sp.sympify(a[0])),
    "gamma": lambda a: sp.stats.Gamma("X", sp.sympify(a[0]), sp.sympify(a[1])),
    "chi2": lambda a: sp.stats.ChiSquared("X", sp.sympify(a[0])),
    "t": lambda a: sp.stats.StudentT("X", sp.sympify(a[0])),
    "beta": lambda a: sp.stats.Beta("X", sp.sympify(a[0]), sp.sympify(a[1])),
    "f": lambda a: sp.stats.FDistribution("X", sp.sympify(a[0]), sp.sympify(a[1])),
    "lognormal": lambda a: sp.stats.LogNormal("X", sp.sympify(a[0]), sp.sympify(a[1])),
}


def c_stats(out, pos, opt):
    from sympy.stats import density, cdf as scdf, quantile, sample
    op = pos[0].lower()
    name = pos[1].lower()
    if name not in _DISTS:
        return out.finish(ok=False, error=f"未知分布: {name}（可用: {', '.join(_DISTS)}）")
    args = [_parse(p) for p in pos[2:2 + 2]] if name in (
        "normal", "uniform", "gamma", "beta", "f", "lognormal") else [_parse(p) for p in pos[2:3]]
    if name in ("binomial",):
        args = [pos[2], _parse(pos[3])]
    X = _DISTS[name](args)
    if op == "pdf":
        x = _parse(pos[-1])
        return _emit(out, density(X)(x))
    if op == "cdf":
        return _emit(out, scdf(X)(_parse(pos[-1])))
    if op == "quantile":
        return _emit(out, quantile(X)(_parse(pos[-1])))
    if op == "sample":
        n = int(opt.get("n", 5))
        seed = int(opt.get("seed", 0))
        vals = sample(X, size=n, seed=seed)
        return _emit(out, [round(float(v), 6) for v in vals])
    return out.finish(ok=False, error=f"未知统计操作: {op}")


# ================================================================ 单位 / 物理
def c_convert(out, pos, opt):
    import sympy.physics.units as u
    from sympy.physics.units import convert_to
    ns = {k: getattr(u, k) for k in dir(u) if not k.startswith("_")}
    src = sp.sympify(pos[0], locals=ns)
    dst = sp.sympify(pos[1], locals=ns)
    res = convert_to(src, dst)
    return _emit(out, sp.N(res, 6))


def c_wigner(out, pos, opt):
    from sympy.physics.wigner import wigner_3j, wigner_6j, wigner_9j
    vals = [_parse(p) for p in pos]
    fn = {"3j": wigner_3j, "6j": wigner_6j, "9j": wigner_9j}[opt.get("kind", "3j")]
    return _emit(out, fn(*vals))


def c_cg(out, pos, opt):
    from sympy.physics.wigner import clebsch_gordan
    return _emit(out, clebsch_gordan(*[_parse(p) for p in pos]))


def c_hydrogen(out, pos, opt):
    from sympy.physics.hydrogen import R_nl, E_nl
    n, l = int(pos[0]), int(pos[1])
    r = _sym(pos[2]) if len(pos) > 2 else sp.Symbol("r")
    Z = int(opt.get("Z", 1))
    return _emit(out, f"R_{n}{l}={R_nl(n, l, r, Z)}  E={E_nl(n, l)} Hartree")


# ================================================================ 逻辑
def c_logic(out, pos, opt):
    from sympy.logic.boolalg import simplify_logic, to_dnf, to_cnf
    from sympy.logic.inference import satisfiable
    op = pos[0].lower()
    e = sp.sympify(pos[1], locals={v: sp.Symbol(v) for v in _split(opt.get("syms", "p,q,r"))})
    if op == "sat":
        return _emit(out, satisfiable(e))
    if op == "simplify":
        return _emit(out, simplify_logic(e))
    if op == "dnf":
        return _emit(out, to_dnf(e, force=True))
    if op == "cnf":
        return _emit(out, to_cnf(e, force=True))
    return out.finish(ok=False, error=f"未知逻辑操作: {op}")


# ================================================================ 输出 / 代码
def c_codegen(out, pos, opt):
    expr = _parse(pos[0])
    lang = opt.get("lang", "c").lower()
    from sympy.printing.codeprinter import ccode, fcode, rust_code
    from sympy.printing.julia import julia_code
    from sympy.printing.octave import octave_code
    printer = {"c": ccode, "fortran": fcode, "rust": rust_code,
               "julia": julia_code, "octave": octave_code}.get(lang)
    if printer is None:
        return out.finish(ok=False, error=f"不支持的语言: {lang}（c/fortran/rust/julia/octave）")
    return _emit(out, printer(expr, assign_to=opt.get("name", "f_result")))


def c_to_numpy(out, pos, opt):
    expr = _parse(pos[0])
    vs = _vars(pos[1]) if len(pos) > 1 else sorted(expr.free_symbols, key=str)
    arg = ", ".join(map(str, vs))
    code = (f"import numpy as np\nfrom sympy import symbols, lambdify\n"
            f"{arg} = symbols('{ ' '.join(map(str, vs)) }')\n"
            f"f = lambdify(({arg},), {expr!r}, 'numpy')")
    return _emit(out, code)


# ================================================================ 补充：几何 / 置换 / 数值 / 绘图
def c_geo(out, pos, opt):
    from sympy.geometry import (Point, Point3D, Line, Segment, Circle, Ellipse,
                                Polygon, Triangle, Plane, intersection)
    ns = dict(Point=Point, Point3D=Point3D, Line=Line, Segment=Segment, Circle=Circle,
              Ellipse=Ellipse, Polygon=Polygon, Triangle=Triangle, Plane=Plane)
    op = pos[0].lower()
    objs = [sp.sympify(p, locals=ns) for p in pos[1:]]
    if op == "distance":
        return _emit(out, objs[0].distance(objs[1]))
    if op == "intersection":
        return _emit(out, intersection(*objs))
    if op == "area":
        return _emit(out, objs[0].area)
    if op == "equation":
        return _emit(out, objs[0].equation())
    if op == "contains":
        return _emit(out, objs[0].contains(objs[1]))
    if op == "centroid":
        return _emit(out, objs[0].centroid)
    return out.finish(ok=False, error=f"未知几何操作: {op}（distance/intersection/area/equation/contains/centroid）")


def c_perm(out, pos, opt):
    from sympy.combinatorics import Permutation, PermutationGroup
    op = pos[0].lower()
    if op == "group":
        gens = [Permutation(sp.sympify(g)) for g in _split(pos[1])]
        G = PermutationGroup(gens)
        return _emit(out, f"order={G.order()}  is_abelian={G.is_abelian}  is_cyclic={G.is_cyclic}")
    pm = Permutation(sp.sympify(pos[1]))
    if op == "order":
        return _emit(out, pm.order())
    if op == "sign":
        return _emit(out, pm.signature())
    if op == "cycles":
        return _emit(out, pm.cyclic_form)
    if op == "inverse":
        return _emit(out, pm ** -1)
    if op == "array":
        return _emit(out, pm.array_form)
    if op == "power":
        return _emit(out, pm ** int(pos[2]))
    return out.finish(ok=False, error=f"未知置换操作: {op}（order/sign/cycles/inverse/array/power/group）")


def c_neval(out, pos, opt):
    """数值求值：表达式 + 'x=1,y=2'，走 lambdify/numpy。"""
    expr = _parse(pos[0])
    subs = {}
    for kv in (_split(pos[1]) if len(pos) > 1 else []):
        k, v = kv.split("=", 1)
        subs[_sym(k.strip())] = float(v)
    f = sp.lambdify(tuple(subs), expr, "numpy")
    val = f(*subs.values())
    return _emit(out, val)


def c_plot(out, pos, opt):
    """画图并存 PNG：plot EXPR 'x:-5:5' [--out out.png]"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    expr = _parse(pos[0])
    spec = pos[1] if len(pos) > 1 else f"{sorted(expr.free_symbols, key=str)[0]}:-5:5"
    var_s, lo_s, hi_s = spec.split(":")
    var = _sym(var_s)
    f = sp.lambdify(var, expr, "numpy")
    xs = np.linspace(float(_parse(lo_s)), float(_parse(hi_s)), int(opt.get("points", 800)))
    ys = f(xs)
    fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=140)
    ax.plot(xs, ys, lw=1.6)
    ax.axhline(0, lw=0.6, color="0.6")
    ax.axvline(0, lw=0.6, color="0.6")
    ax.set_xlabel(str(var))
    ax.set_title(str(expr)[:80])
    ax.grid(alpha=0.25, lw=0.5)
    path = opt.get("out", "mathkit_plot.png")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return _emit(out, f"saved: {path}")


def c_pretty(out, pos, opt):
    return _emit(out, "\n" + sp.pretty(_parse(pos[0])))


# ================================================================ 主表
TABLE = {
    # 代数
    "simplify": c_simplify,
    "expand": c_expand,
    "factor": c_factor,
    "apart": _simple("apart", lambda e, *a: sp.apart(e, *a)),
    "together": _simple("together", sp.together),
    "cancel": _simple("cancel", sp.cancel),
    "trigsimp": _simple("trigsimp", sp.trigsimp),
    "logcombine": _simple("logcombine", lambda e: sp.logcombine(e, force=True)),
    "powsimp": _simple("powsimp", sp.powsimp),
    "radsimp": _simple("radsimp", sp.radsimp),
    "collect": c_collect,
    "rewrite": c_rewrite,
    "gcd": c_gcd,
    "lcm": c_lcm,
    "resultant": c_resultant,
    "discriminant": c_discriminant,
    "groebner": c_groebner,
    "roots": c_roots,
    "nroots": c_nroots,
    "minpoly": c_minpoly,
    "degree": c_degree,
    # 微积分新增
    "laplace": c_laplace,
    "ilaplace": c_ilaplace,
    "ftransform": c_fourier_transform,
    "contour": c_contour,
    # 求解新增
    "solveset": c_solveset,
    "linsolve": c_linsolve,
    "nonlinsolve": c_nonlinsolve,
    "rsolve": c_rsolve,
    "diophantine": c_diophantine,
    "ineq": c_ineq,
    "nsolve": c_nsolve,
    # 线代 / 数论 / 统计 / 单位 / 物理 / 逻辑
    "linalg": c_linalg,
    "nt": c_nt,
    "stats": c_stats,
    "convert": c_convert,
    "wigner": c_wigner,
    "cg": c_cg,
    "hydrogen": c_hydrogen,
    "logic": c_logic,
    "se": c_se,
    "geo": c_geo,
    "perm": c_perm,
    "neval": c_neval,
    "plot": c_plot,
    "pretty": c_pretty,
    "expand_trig": _simple("expand_trig", sp.expand_trig),
    "ratsimp": _simple("ratsimp", sp.ratsimp),
    "sqrtdenest": _simple("sqrtdenest", sp.sqrtdenest),
    "powdenest": _simple("powdenest", sp.powdenest),
    "signsimp": _simple("signsimp", sp.signsimp),
    "separatevars": _simple("separatevars", sp.separatevars),
    "besselsimp": _simple("besselsimp", sp.besselsimp),
    "codegen": c_codegen,
    "to-numpy": c_to_numpy,
}

# 委托给 math_tool 的命令（已加固，避免重复实现）
DELEGATE = {
    "integrate", "subs-integrate", "limit", "series", "summation", "product",
    "residue", "residues", "dsolve", "ode-series", "multi-integrate", "converge",
    "fourier", "orthdiag", "dist", "expect", "var", "mgf", "mle", "ci",
    "check", "evalf", "latex", "nsimplify", "gammasimp", "combsimp",
    "hyperexpand", "matrix", "matrix-eqsolve", "numeric-integrate", "subs", "coeff",
    "solve", "diff",
}


HELP = """mathkit -- SymPy + SymEngine 统一符号计算

用法: mathkit.py [--json] [--engine auto|sympy|symengine] [--sym "x:positive"] <命令> [参数...]

代数     simplify/expand/factor/apart/together/cancel/trigsimp/logcombine/
         powsimp/radsimp/collect/rewrite/gcd/lcm/resultant/discriminant/
         groebner/roots/nroots/minpoly/degree
微积分   diff/integrate/limit/series/summation/product/residue/
         laplace/ilaplace/ftransform
留数     contour EXPR VAR --domain upper|lower|unit|all [--trig] [--no-check]
         （极点枚举 → 按围道筛选 → 2πi·ΣRes → 双通道对账）
求解     solve/solveset/linsolve/nonlinsolve/dsolve/rsolve/diophantine/ineq/nsolve
线代     linalg <det|inv|rank|rref|trace|transpose|nullspace|columnspace|rowspace|
         charpoly|eigen|eigenvects|jordan|qr|cholesky|svd|pinv|expm|norm|cond|solve> MAT [ARG]
数论     nt <factorint|isprime|primes|totient|divisors|nextprime|primepi|crt|cf|
         binomial|partitions> 参数...
统计     stats <pdf|cdf|quantile|sample> <分布> 参数...；dist/expect/var/mgf 走 math_tool
单位     convert "3*km/hour" "m/s"
物理     wigner j1 j2 j3 m1 m2 m3 [--kind 3j|6j|9j] / cg j1 m1 j2 m2 j m / hydrogen n l [r]
逻辑     logic <sat|simplify|dnf|cnf> "表达式" [--syms p,q,r]
输出     latex / codegen EXPR [--lang c|fortran|rust|julia|octave] / to-numpy EXPR [vars]
其它     evalf / check / matrix / solve / diff ... 自动委托 sympy-math 加固实现
"""


def main(argv):
    global ENGINE
    as_json = False
    pos, opt = [], {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help", "help"):
            print(HELP)
            return 0
        if a == "--json":
            as_json = True
        elif a.startswith("--"):
            key = a[2:]
            if "=" in key:
                k, v = key.split("=", 1)
                opt[k] = v
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opt[key] = argv[i + 1]
                i += 1
            else:
                opt[key] = True
        else:
            pos.append(a)
        i += 1

    if not pos:
        print(HELP)
        return 0

    cmd = pos[0].lower()
    rest = pos[1:]
    ENGINE = opt.get("engine", "auto").lower()

    if _CORE is not None:
        if "sym" in opt:
            _CORE.register_syms(opt["sym"])
        if opt.get("preserve-e"):
            _CORE.E_MAP_ENABLED = False
    for k in ("sym",):
        if k in opt and _CORE is None:
            pass

    out = Out(cmd, as_json)

    if cmd in DELEGATE or cmd not in TABLE:
        if cmd in DELEGATE:
            keep = []
            for k, v in opt.items():
                if k in ("engine", "json"):
                    continue
                if v is True:
                    keep.append("--" + k)
                else:
                    keep.extend(["--" + k, str(v)])
            return _delegate(out, [cmd] + rest + keep)
        return out.finish(ok=False, error=f"未知命令: {cmd}（mathkit.py --help 查看清单）")

    try:
        return TABLE[cmd](out, rest, opt)
    except SystemExit:
        raise
    except Exception as e:
        msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        return out.finish(ok=False, error=msg)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
