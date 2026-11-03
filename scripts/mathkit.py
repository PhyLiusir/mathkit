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

# time 提前到其它 import 之前，只为拿到「模块导入阶段」的起点。
# 实测 import sympy 要 1.9 s，而真正计算常常只需 0.4 s —— 不拆开报，
# 用户只会看到「这条命令跑了 2.6 秒」并误以为算法慢。
import time

_T_PROC_START = time.perf_counter()

import io
import contextlib
import json
import math
import os
import sys

import mpmath as mp
from sympy.parsing.sympy_parser import (
    parse_expr, standard_transformations, convert_xor, rationalize)

import sympy as sp
from sympy import S

TRANS = tuple(standard_transformations) + (convert_xor, rationalize)
TRANS_FLOAT = tuple(t for t in TRANS if t is not rationalize)

# 局部字典：常见教材写法的别名映射 + 小写 e → E（--preserve-e 可关闭）
E_MAP_ENABLED = True


def _lg(x):
    return sp.log(x, 10)


ALIAS_DICT = {
    "ln": sp.log,
    "arcsin": sp.asin, "arccos": sp.acos, "arctan": sp.atan,
    "arccot": sp.acot, "arcsec": sp.asec, "arccsc": sp.acsc,
    "arcsinh": sp.asinh, "arccosh": sp.acosh, "arctanh": sp.atanh,
    "lg": _lg,
}
LOCAL_DICT = dict(ALIAS_DICT)
LOCAL_DICT["e"] = sp.E

# 全局符号假设表（由 --sym "n:integer,m:positive" 注入）
SYM_ASSUMPTIONS = {}
_ASSUME_MAP = {
    "real": {"real": True},
    "pos": {"positive": True}, "positive": {"positive": True},
    "nonneg": {"nonnegative": True}, "nonnegative": {"nonnegative": True},
    "int": {"integer": True}, "integer": {"integer": True},
    "nat": {"integer": True, "positive": True}, "natural": {"integer": True, "positive": True},
    "neg": {"negative": True}, "negative": {"negative": True},
    "nz": {"nonzero": True}, "nonzero": {"nonzero": True},
}


def register_syms(spec):
    """注册符号假设：spec 形如 'n:integer,m:positive'（省略冒号默认 real）。"""
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, ass = part.split(":", 1)
        else:
            name, ass = part, "real"
        name, ass = name.strip(), ass.strip().lower()
        kw = _ASSUME_MAP.get(ass)
        if kw is None:
            raise ValueError("未知假设 '%s'（可用: %s）" % (ass, "/".join(sorted(_ASSUME_MAP))))
        SYM_ASSUMPTIONS[name] = sp.Symbol(name, **kw)


def _sym(name):
    """取积分/求和变量：若 --sym 已声明过该符号，返回【带假设】的那个，
    否则新建无假设符号。⚠️ 否则 integrand（带假设符号）与积分变量（新符号）不是同一个对象，积分会静默失败。"""
    name = name.strip()
    return SYM_ASSUMPTIONS.get(name, sp.Symbol(name))


# ---------------------------------------------------------------- 基础工具

def parse(s, rational=True):
    """把字符串解析为 sympy 表达式。
    ^ -> **；小数 -> Rational；e -> E；教材别名（ln/arcsin/arctan/...）-> sympy 函数；
    并叠加 --sym 注册的符号假设。"""
    ld = {}
    ld.update(ALIAS_DICT)          # ln/arcsin 等别名始终可用
    if E_MAP_ENABLED:
        ld["e"] = sp.E
    ld.update(SYM_ASSUMPTIONS)
    return parse_expr(s, transformations=(TRANS if rational else TRANS_FLOAT),
                      local_dict=ld)


def split_top(s, sep=","):
    """按顶层分隔符切分（忽略括号内的分隔符）。"""
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
    return [p.strip() for p in parts if p.strip() != ""]


def split_top_kv(s, tok):
    """在第一个顶层 tok 处切成两半；找不到返回 None。"""
    depth = 0
    for i, ch in enumerate(s):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and s.startswith(tok, i):
            return s[:i], s[i + len(tok):]
    return None


def to_mp(x):
    """sympy 数 -> mpmath 数（支持 oo）。"""
    if x == sp.oo:
        return mp.inf
    if x == -sp.oo:
        return -mp.inf
    return mp.mpf(str(sp.N(x, 30)))


def is_zero(expr):
    try:
        return sp.simplify(expr) == 0
    except Exception:
        return False


class _MT_Out:
    """统一输出：人类可读行 + 可选 JSON。
    非 JSON 模式下**逐行流式刷新**——长任务被 timeout 杀掉时仍保留已算出的内容。"""

    def __init__(self, as_json=False):
        self.as_json = as_json
        self.stream = not as_json
        self.lines = []
        self.notes = []
        self.result_str = None
        self.result_latex = None
        self.result_numeric = None

    def line(self, s=""):
        self.lines.append(s)
        if self.stream:
            print(s, flush=True)

    def set_result(self, expr, numeric_digits=20):
        self.result_str = str(expr)
        try:
            self.result_latex = sp.latex(expr)
        except Exception:
            self.result_latex = None
        try:
            self.result_numeric = str(sp.N(expr, numeric_digits))
        except Exception:
            self.result_numeric = None
        return expr

    def note(self, s):
        self.notes.append(s)
        if self.stream:
            print(".. %s" % s, flush=True)

    def finish(self, ok=True, error=None):
        if self.as_json:
            payload = {
                "ok": ok,
                "lines": self.lines,
                "notes": self.notes,
                "result_str": self.result_str,
                "result_latex": self.result_latex,
                "result_numeric": self.result_numeric,
            }
            if error:
                payload["error"] = str(error)
            print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)
        else:
            if self.stream:
                # 行与备注已流式打印过，这里只补错误信息
                if error:
                    print("ERROR: %s" % error, flush=True)
            else:
                if error:
                    print("ERROR: %s" % error)
                for ln in self.lines:
                    print(ln)
                if self.notes:
                    print("备注:")
                    for nt in self.notes:
                        print("  - %s" % nt)
        return 0 if ok else (2 if error is not None and str(error) == "CHECK_FAILED" else 1)


# ---------------------------------------------------------------- 各命令实现

def _wallis_rule(f, var, a, b):
    """Wallis 型积分规则：∫₀^{π/2} sin^p·cos^q dx = (1/2)B((p+1)/2,(q+1)/2)。
    匹配 sin/cos 的纯幂乘积；返回闭式或 None。"""
    try:
        if not (sp.simplify(a) == 0 and sp.simplify(b - sp.pi / 2) == 0):
            return None
        p = sp.S.Zero
        q = sp.S.Zero
        rest = f
        for term in sp.Mul.make_args(sp.expand_power_base(sp.powsimp(f, force=True))):
            if term.is_Pow and term.base == sp.sin(var):
                p += term.exp
            elif term.is_Pow and term.base == sp.cos(var):
                q += term.exp
            elif term == sp.sin(var):
                p += 1
            elif term == sp.cos(var):
                q += 1
            else:
                return None
        if p == 0 and q == 0:
            return None
        beta = sp.beta((p + 1) / 2, (q + 1) / 2)
        g = sp.simplify(sp.Rational(1, 2) * beta).rewrite(sp.gamma)
        g = sp.simplify(g)
        return g
    except Exception:
        return None


def _slow_to_eval(res):
    """判断结果的 evalf 是否会非常慢（Bessel/超几何/椭圆类特殊函数 + 超大表达式）。"""
    try:
        slow = (sp.besselj, sp.bessely, sp.besseli, sp.besselk, sp.hankel1, sp.hankel2,
                sp.hyper, sp.meijerg, sp.elliptic_k, sp.elliptic_f, sp.elliptic_e,
                sp.LambertW, sp.zeta, sp.polylog)
        if res.has(*slow):
            return True
        return sp.count_ops(res) > 150
    except Exception:
        return True


def cmd_integrate(out, args, opts):
    if len(args) not in (2, 4):
        raise ValueError("用法: integrate EXPR VAR [A B]")
    f = parse(args[0])
    var = _sym(args[1])
    out.line("被积函数: $%s$" % sp.latex(f))
    if opts.get("numeric"):
        a, b = (parse(args[2]), parse(args[3])) if len(args) == 4 else (None, None)
        if a is None:
            raise ValueError("--numeric 模式需要定积分边界 A B")
        out.line("积分变量: %s ，区间 [%s, %s]" % (var, a, b))
        val = _quad(f, var, a, b)
        out.set_result(val)
        out.line("数值结果: %s" % val)
        return
    anchor = None
    if len(args) == 2:
        out.line("不定积分  d/d%s:" % var)
        res = sp.integrate(f, var)
    else:
        a, b = parse(args[2]), parse(args[3])
        out.line("定积分  %s : %s -> %s" % (var, a, b))
        # 数值锚点：先给一个数值估计（通常秒级），符号路线挂起/被杀时也拿得到结果
        try:
            v0, e0 = _quad(f, var, a, b)
            anchor = v0
            if abs(e0) <= sp.Float("0.02") * max(abs(v0), sp.Float("1e-6")):
                out.line("数值锚点: %s（误差估计 %s）" % (sp.N(v0, 15), sp.Float(str(e0), 3)))
            else:
                out.line("数值锚点: %s（误差估计 %s —— 偏大，仅供参考）"
                         % (sp.N(v0, 10), sp.Float(str(e0), 3)))
        except Exception as e:
            out.line("数值锚点: 失败（%s）" % (str(e) or repr(e)))
        res = sp.integrate(f, (var, a, b))
        # 兜底：规则式积分器（manualintegrate 常能处理 integrate 失败的三角有理式）
        if isinstance(res, sp.Integral) or res.has(sp.Integral):
            try:
                mr = sp.manualintegrate(f, var)
                if not isinstance(mr, sp.Integral) and not mr.has(sp.Integral):
                    bound = mr.subs(var, b) - mr.subs(var, a)
                    if not bound.has(sp.nan) and not bound.has(sp.zoo):
                        out.note("integrate 未成功，manualintegrate 兜底成功")
                        res = sp.simplify(bound)
            except Exception:
                pass
    # 结果净化：超几何 → 初等/Γ 表达；三角化简
    if not isinstance(res, sp.Integral) and res.has(sp.hyper):
        try:
            h = sp.hyperexpand(res)
            if not h.has(sp.hyper):
                out.note("结果含超几何函数，已 hyperexpand 化简")
                res = h
        except Exception:
            pass
    # 结果净化 2：MeijerG —— 做题场景下不可用，强制换初等/Γ 表达（2026-10-17 新增）
    if not isinstance(res, sp.Integral) and res.has(sp.meijerg):
        try:
            h2 = sp.hyperexpand(res)
            if not h2.has(sp.meijerg):
                h2 = sp.simplify(h2)
                out.note("结果含 MeijerG，已 hyperexpand 化为初等/Γ 表达")
                res = h2
        except Exception:
            pass
    if not isinstance(res, sp.Integral) and res.has(sp.meijerg):
        # 换初等表达失败 → 用【高精度数值锚点】反猜闭式（锚点有 ~20 位，1e-8 紧公差下安全；
        # 与 limit 不同：那里采样只有 6-7 位，反猜会出伪闭式）
        guessed = None
        if anchor is not None:
            try:
                g = sp.nsimplify(sp.Float(str(sp.N(anchor, 22)), 22),
                                 [sp.pi, sp.E, sp.log(2), sp.log(3), sp.sqrt(2), sp.sqrt(3), sp.EulerGamma],
                                 rational=False, tolerance=1e-8)
                if (not g.free_symbols
                        and abs(complex(sp.N(g, 22)) - complex(anchor))
                        < 1e-9 * max(abs(complex(anchor)), 1e-12)):
                    guessed = g
            except Exception:
                guessed = None
        if guessed is not None:
            out.note("结果含 MeijerG 且无法符号化简 → 由高精度数值锚点反猜闭式 $%s$"
                     "（猜测，已按 1e-9 回代校验；非严格证明）" % sp.latex(guessed))
            res = guessed
        else:
            out.note("⚠️ 结果仍含 MeijerG：数学上可能等价，但对做题无意义。"
                     "请换元/分部/级数重算，或以上方数值锚点为准")
    # 兜底：含符号参数未求值 → 先试 Wallis 规则，再给参数加正/整数假设后重试
    if isinstance(res, sp.Integral) or res.has(sp.Integral):
        if len(args) == 4:
            wr = _wallis_rule(f, var, a, b)
            if wr is not None and not wr.has(sp.Integral):
                out.note("命中 Wallis 型积分规则 ∫₀^{π/2} sin^p·cos^q = B((p+1)/2,(q+1)/2)/2")
                res = wr
        if isinstance(res, sp.Integral) or res.has(sp.Integral):
            params = f.free_symbols - {var}
            if params:
                try:
                    subs_map = {p: sp.Symbol(p.name, positive=True, integer=True) for p in params}
                    f2 = f.subs(subs_map)
                    r2 = sp.integrate(f2, var) if len(args) == 2 else sp.integrate(f2, (var, a, b))
                    r2 = r2.subs({v: k for k, v in subs_map.items()})
                    if not isinstance(r2, sp.Integral) and not r2.has(sp.Integral):
                        out.note("给符号参数 %s 加【正整数】假设后求解成功（未加假设时 sympy 失败）"
                                 % sorted(p.name for p in params))
                        res = sp.simplify(r2)
                        if res.has(sp.hyper):
                            try:
                                res = sp.hyperexpand(res)
                            except Exception:
                                pass
                except Exception:
                    pass
    if isinstance(res, sp.Integral) or res.has(sp.Integral):
        if isinstance(res, sp.Integral):
            out.note("sympy 未能求出闭式原函数（返回未求值 Integral）")
        else:
            out.note("⚠️ 结果中仍残留未求值积分（部分求值）：这不是完整答案")
        if len(args) == 4:
            try:
                val, err = _quad(f, var, a, b)
                bad = _scan_singular(f, var, a, b)
                if bad:
                    out.note("⚠️ 区间内被积函数存在奇点/不可采样点（前几个: %s），"
                             "降级数值结果可能不可信" % [sp.N(x, 6) for x in bad])
                if abs(err) > sp.Float("0.05") * max(abs(val), sp.Float("1e-8")):
                    out.note("⚠️ 误差估计 %s 相对值过大（>5%%），降级数值结果不可信"
                             % sp.Float(str(err), 5))
                out.line("降级数值积分: %s （误差估计 %s）" % (val, sp.Float(str(err), 5)))
                if res.has(sp.Integral):
                    out.set_result(val)
                    out.line("结果（残留未求值积分，已改用数值）: %s" % sp.N(val, 15))
                    out.note("建议换路线：换元 / 分部 / 围道 / 级数 / 对称性；"
                             "或在 --sym 里给符号参数加假设后重试")
                    return
            except Exception as e:
                msg = str(e) or repr(e)
                out.note("数值降级也失败: %s" % msg)
                if f.free_symbols - {var}:
                    out.note("被积函数含符号参数 %s：请代入具体数值后分别验证，再归纳通式"
                             % sorted(f.free_symbols - {var}, key=str))
        out.set_result(res)
        out.line("结果（未求值）: $%s$" % sp.latex(res))
        return
    # 符号结果 vs 数值锚点对账（定积分；抓"闭式给但不等于积分值"的静默错答）
    if (anchor is not None and res.is_number and not res.has(sp.zoo)
            and not res.has(sp.meijerg) and not res.has(sp.hyper)):
        try:
            rv = complex(sp.N(res, 20))
            av = complex(anchor)
            scale = max(abs(av), abs(rv), 1.0)
            dev = abs(rv - av) / scale
            if dev > 1e-6:
                out.note("⚠️ 符号结果 %s 与数值锚点 %s 相对偏差 %.2e：请复核（可能漏项/符号错）"
                         % (sp.N(res, 12), sp.N(anchor, 12), dev))
            else:
                out.line("数值对账: 符号结果 ≈ 锚点 -> PASS")
        except Exception:
            pass
    out.set_result(res)
    out.line("结果: $%s$" % sp.latex(res))
    ops = sp.count_ops(res)
    try:
        if not _slow_to_eval(res) and ops <= 150:
            out.line("数值: %s" % sp.N(res, 20))
        else:
            out.line("数值: 跳过 evalf（%s，高精度数值评估极慢；需要数值请用 numeric-integrate）"
                     % ("含 Bessel/超几何/椭圆类特殊函数" if ops <= 150 else "表达式含 %d 个算子" % ops))
    except Exception:
        pass
    # 符号/数值对账（含复数误判纠正）；表达式过于庞大（如 Bessel 型闭式）时跳过以免数值评估极慢
    if len(args) == 4:
        if _slow_to_eval(res):
            out.note("跳过数值对账：结果含慢速特殊函数或算子数 %d 过大，evalf 可能耗时数分钟"
                     % sp.count_ops(res))
            return
        try:
            val, err = _quad(f, var, a, b)
            if abs(err) <= sp.Float("0.02") * max(abs(val), sp.Float("1e-6")):
                resn = complex(sp.N(res, 20))
                valn = complex(sp.N(val, 20))
                # 情形 1：符号结果有显著虚部，而数值积分是实的 → 分支切割误判，改用数值
                if abs(resn.imag) > 1e-6 * max(abs(resn.real), 1e-8) and abs(valn.imag) < 1e-8:
                    out.note("⚠️ 符号结果含伪虚部 %s（分支切割/对数支），数值积分给出实值："
                             "**上面的符号结果作废**，采用数值实部 %s"
                             % (sp.N(sp.im(res), 8), sp.N(sp.re(val), 12)))
                    res = _clean_num(sp.re(val))
                    out.set_result(res)
                    out.line("最终结果: $%s$" % sp.latex(sp.N(res, 15)))
                    return
                d = abs(resn - valn)
                scale = max(abs(resn), abs(valn), 1e-8)
                if d > 1e-6 * scale:
                    out.note("⚠️ 符号结果 evalf=%s 与数值积分 %s（误差 %s）偏差 %s：两侧不一致"
                             "（mpmath 对振荡/缓衰被积函数常不准，符号闭式也可能数值不稳定）。"
                             "请用 check 符号化简或换积分区间判定以哪侧为准"
                             % (sp.N(res, 12), sp.N(val, 12), sp.Float(str(err), 3), "%.3e" % d))
        except Exception:
            pass


def _scan_singular(f, var, a, b, n=80):
    """扫描 [a,b] 内被积函数的奇点（NaN/inf/求值异常）。
    返回不可积采样点列表（截断到前 6 个）。可去奇点（0/0）也会被 mpmath 采成 NaN，
    是数值积分静默出错的常见根因。"""
    if a in (sp.oo, -sp.oo) or b in (sp.oo, -sp.oo):
        return []  # 无穷区间跳过扫描（变换坐标下奇点检测不可靠）
    try:
        fa = sp.lambdify(var, f, "mpmath")
        am, bm = to_mp(a), to_mp(b)
    except Exception:
        return []
    bad = []
    for i in range(n + 1):
        t = am + (bm - am) * mp.mpf(i) / n
        try:
            v = fa(t)
            vc = complex(v)
            if not (math.isfinite(vc.real) and math.isfinite(vc.imag)):
                bad.append(t)
        except Exception:
            bad.append(t)
    return bad[:6]


def _quad(f, var, a, b):
    fa = sp.lambdify(var, f, "mpmath")
    # mpmath>=1.3 默认只返回值；error=True 才返回 (值, 误差估计)
    val, err = mp.quad(fa, [to_mp(a), to_mp(b)], error=True)
    return _mp_to_sympy(val, 20), err


def cmd_subs_integrate(out, args, opts):
    """带代换的积分：subs-integrate EXPR VAR A B SUB，SUB 形如 x=(1-t)/(1+t)。"""
    if len(args) != 5:
        raise ValueError("用法: subs-integrate EXPR VAR A B SUB")
    f = parse(args[0])
    old = _sym(args[1])
    a, b = parse(args[2]), parse(args[3])
    kv = split_top_kv(args[4], "=")
    if kv is None:
        raise ValueError("SUB 需形如 x=(1-t)/(1+t)")
    lhs_name, rhs_str = kv[0].strip(), kv[1].strip()
    if lhs_name != old.name:
        raise ValueError("SUB 左端 %s 应与积分变量 %s 一致" % (lhs_name, old.name))
    rhs = parse(rhs_str)
    cands = [s for s in rhs.free_symbols if s != old]
    if len(cands) != 1:
        raise ValueError("无法从 SUB 确定新变量（右端自由符号应为 1 个）: %s" % rhs.free_symbols)
    t = cands[0]
    if old not in f.free_symbols:
        raise ValueError("被积函数中不出现积分变量 %s" % old.name)

    sol_a = sp.solve(sp.Eq(rhs, a), t)
    sol_b = sp.solve(sp.Eq(rhs, b), t)
    if len(sol_a) != 1 or len(sol_b) != 1:
        raise ValueError("代换在边界处解不唯一，请手动处理: t(%s)=%s, t(%s)=%s" % (a, sol_a, b, sol_b))
    ta, tb = sol_a[0], sol_b[0]
    sign = 1
    lo, hi = ta, tb
    if (ta - tb).is_real is not False and sp.N(ta) > sp.N(tb):
        lo, hi, sign = tb, ta, -1

    g = sp.simplify(f.subs(old, rhs) * sp.diff(rhs, t))
    extra = g.free_symbols - {t}
    if extra:
        out.note("代换后仍含其他符号 %s，按常量处理" % extra)
    out.line("代换: %s = %s" % (old.name, rhs_str))
    out.line("新积分变量: %s ，边界 %s -> %s ，%s -> %s" % (t, a, ta, b, tb))
    out.line("新被积函数（含 Jacobian）: $%s$" % sp.latex(g))
    res = sp.integrate(g, (t, lo, hi))
    if isinstance(res, sp.Integral):
        out.note("代换后 sympy 仍未求出闭式")
        out.set_result(sign * res)
        out.line("结果（未求值）: $%s$" % sp.latex(sign * res))
        return
    res = sp.simplify(sign * res)
    out.set_result(res)
    out.line("结果: $%s$" % sp.latex(res))
    try:
        out.line("数值: %s" % sp.N(res, 20))
    except Exception:
        pass


def cmd_diff(out, args, opts):
    if len(args) not in (2, 3):
        raise ValueError("用法: diff EXPR VAR [N]")
    f = parse(args[0])
    var = _sym(args[1])
    n = int(args[2]) if len(args) == 3 else 1
    out.line("函数: $%s$" % sp.latex(f))
    out.line("求导: d^%s/d%s^%s" % (n, var.name, n))
    res = sp.diff(f, var, n)
    out.set_result(res)
    out.line("结果: $%s$" % sp.latex(res))
    # --at "x=1[,...]"：求导后立即代入（避免 CLI 无法管道、手工转录出错）
    if opts.get("at"):
        mapping = {}
        for part in split_top(opts["at"]):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise ValueError("--at 项缺少 '='：%r（应形如 x=1）" % part)
            name, val = part.split("=", 1)
            mapping[_sym(name.strip())] = parse(val.strip())
        val = res.subs(mapping)
        try:
            val = sp.simplify(val)
        except Exception:
            pass
        out.line("代入 %s 后: $%s$" % (", ".join("%s=%s" % (k, sp.latex(v)) for k, v in mapping.items()),
                                     sp.latex(val)))
        out.set_result(val)
        out.line("结果: $%s$" % sp.latex(val))
        if val.is_number:
            try:
                out.line("数值: %s" % sp.N(val, 20))
            except Exception:
                pass


def cmd_solve(out, args, opts):
    if len(args) != 2:
        raise ValueError("用法: solve EQS VARS（VARS 可用 all 表示全部自由符号）")
    eq_strs = split_top(args[0])
    eqs = []
    for es in eq_strs:
        kv = split_top_kv(es, "==")      # 先试 ==，否则 "=" 会误切 x^2-1==0
        if kv is None:
            kv = split_top_kv(es, "=")
        if kv is None:
            eqs.append(parse(es))
        else:
            eqs.append(sp.Eq(parse(kv[0]), parse(kv[1])))
    if args[1].strip().lower() == "all":
        syms = sorted(set().union(*[e.free_symbols for e in eqs]), key=lambda s: s.name)
    else:
        syms = [_sym(s) for s in split_top(args[1])]
    out.line("方程组: %s" % ", ".join(sp.latex(e) for e in eqs))
    out.line("未知数: %s" % ", ".join(s.name for s in syms))
    sols = sp.solve(eqs, syms, dict=True)
    if not sols:
        out.line("结果: 无解")
        out.set_result(sp.S.EmptySet)
        return
    for i, sd in enumerate(sols, 1):
        out.line("解 %d: %s" % (i, ", ".join("%s = %s" % (k, v) for k, v in sd.items())))
    out.set_result(sols)
    if len(sols) == 1:
        out.line("结果: %s" % ", ".join("%s = %s" % (k, v) for k, v in sols[0].items()))
    else:
        out.line("结果: %s" % sols)


def cmd_simplify(out, args, opts):
    if len(args) != 1:
        raise ValueError("用法: simplify EXPR")
    f = parse(args[0]).doit()
    out.line("输入: $%s$" % sp.latex(f))
    res = sp.simplify(f, deep=True) if opts.get("deep") else sp.simplify(f)
    out.set_result(res)
    out.line("结果: $%s$" % sp.latex(res))
    if sp.simplify(res) == 0:
        out.note("结果化简为 0（恒等式成立）")


def cmd_basic(out, args, opts, func, name):
    if len(args) != 1:
        raise ValueError("用法: %s EXPR" % name)
    f = parse(args[0])
    out.line("输入: $%s$" % sp.latex(f))
    res = func(f)
    out.set_result(res)
    out.line("结果: $%s$" % sp.latex(res))


def _fmt_num(v, prec=10):
    """安全格式化数值（支持复数，避免 '%.10f' % complex 抛 TypeError 被吞）。"""
    try:
        c = complex(v)
        if abs(c.imag) < 1e-12 * max(abs(c.real), 1):
            return "%.*f" % (prec, c.real)
        return "%.*f%+.10fi" % (prec, c.real, c.imag)
    except Exception:
        return str(v)


def _num_limit(f, var, pt, d):
    """数值极限：沿指定方向采样，返回 (极限估计, 稳定性说明)。高精度（dps=25）。"""
    old_dps = mp.mp.dps
    try:
        mp.mp.dps = 25
        fa = sp.lambdify(var, f, "mpmath")
    except Exception:
        mp.mp.dps = old_dps
        return None, "lambdify 失败"
    try:
        if pt == sp.oo:
            xs = [mp.mpf(10)**k for k in (1, 2, 3, 4, 4.5, 5, 5.5, 6)]
        elif pt == -sp.oo:
            xs = [-mp.mpf(10)**k for k in (1, 2, 3, 4, 4.5, 5, 5.5, 6)]
        else:
            p = mp.mpf(str(sp.N(pt, 30)))
            eps = [mp.mpf("0.1")**k for k in range(1, 10)]
            xs = [p + e for e in eps] if d != "-" else [p - e for e in eps]
        vals = []
        for x in xs:
            try:
                vals.append(complex(fa(x)))
            except Exception:
                continue
        if not vals:
            return None, "采样失败"
        last = vals[-1]
        drift = abs(last - vals[-2]) if len(vals) > 1 else float("inf")
        return last, drift
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)
    finally:
        mp.mp.dps = old_dps


def cmd_limit(out, args, opts):
    if len(args) not in (3, 4):
        raise ValueError("用法: limit EXPR VAR POINT [DIR]，DIR in {+,-,+/-}")
    f = parse(args[0])
    var = _sym(args[1])
    pt = parse(args[2])
    d = args[3] if len(args) == 4 else "+-"
    out.line("函数: $%s$" % sp.latex(f))
    arrow = {"+-": "", "+": "+", "-": "-"}[d]
    out.line("极限: %s -> %s%s" % (var.name, pt, arrow))
    try:
        res = sp.limit(f, var, pt, d)
    except ValueError as e:        # sympy 对左右极限不等的情形直接抛 ValueError("... does not exist ...")
        msg = str(e)
        if "does not exist" in msg:
            out.line("sympy 判定极限不存在: %s" % msg)
            out.note("双侧极限不存在（左右极限不等）；结果记为 NaN，exit 0")
            out.set_result(sp.S.NaN)
            return
        raise
    except Exception as e:
        # sympy 的 gruntz 在变限积分/多变量情形常抛 mrv 等异常（2026-10-17 实测：
        # Integral 上限含 x² 时 "Don't know how to calculate the mrv"）→ 不再直接崩，
        # 标记为 NaN，交给下面的数值采样兜底
        out.note("⚠️ sympy 求极限抛异常（%s: %s）；改用数值采样兜底" % (type(e).__name__, e))
        res = sp.S.NaN
    # 双侧极限未求值 → 分别求左右极限，相等则采用（sympy 对 dir='+-' 常比单侧弱）
    if (isinstance(res, sp.Limit) or res.has(sp.Limit)) and d == "+-":
        try:
            rp = sp.limit(f, var, pt, "+")
            rm = sp.limit(f, var, pt, "-")
            okp = not isinstance(rp, sp.Limit) and not rp.has(sp.Limit)
            okm = not isinstance(rm, sp.Limit) and not rm.has(sp.Limit)
            if okp and okm and sp.simplify(rp - rm) == 0:
                out.line("双侧极限未求值，但左右极限相等 -> 采用 $%s$" % sp.latex(rp))
                res = rp
            elif okp and okm:
                out.line("左右极限不等: 左 $%s$ / 右 $%s$ -> 双侧极限不存在" % (sp.latex(rm), sp.latex(rp)))
                res = sp.S.NaN
        except Exception:
            pass
    if isinstance(res, sp.Limit) or res.has(sp.Limit):
        # 兜底 1：幂指函数 f^g —— 取 u=g·ln f 的极限再指数化（1^∞、∞^0 型）
        out.note("直接求极限未求值，尝试幂指改写 / exp-log / 级数兜底")
        alt = None
        if f.is_Pow:
            base, expo = f.base, f.exp
            # 兜底 0：标准 1^∞ 公式 lim f^g = exp(lim g·(f−1))（f→1 时）
            try:
                lb = sp.limit(base, var, pt, d)
                if isinstance(lb, sp.Limit) or lb.has(sp.Limit):
                    lb_p = sp.limit(base, var, pt, "+")
                    lb_m = sp.limit(base, var, pt, "-")
                    if not isinstance(lb_p, sp.Limit) and sp.simplify(lb_p - lb_m) == 0:
                        lb = lb_p
                if sp.simplify(lb - 1) == 0:
                    u0 = sp.limit(expo * (base - 1), var, pt, d)
                    if isinstance(u0, sp.Limit) or u0.has(sp.Limit):
                        u0p = sp.limit(expo * (base - 1), var, pt, "+")
                        u0m = sp.limit(expo * (base - 1), var, pt, "-")
                        if not isinstance(u0p, sp.Limit) and sp.simplify(u0p - u0m) == 0:
                            u0 = u0p
                    if not isinstance(u0, sp.Limit) and not u0.has(sp.Limit) and u0.is_finite is not False:
                        alt = sp.exp(sp.simplify(u0))
                        out.line("1^∞ 公式: lim g·(f−1) = $%s$ -> e^该值" % sp.latex(u0))
            except Exception:
                pass
        if alt is None and f.is_Pow:
            base, expo = f.base, f.exp
            try:
                u = sp.limit(sp.expand_log(expo * sp.log(base), force=True), var, pt, d)
                if not isinstance(u, sp.Limit) and not u.has(sp.Limit):
                    alt = sp.exp(sp.simplify(u))
                    out.line("幂指改写: lim(g·ln f) = $%s$ -> e^该值" % sp.latex(u))
            except Exception:
                alt = None
        if alt is None:
            try:
                alt = sp.limit(sp.expand_log(f.rewrite(sp.exp), force=True), var, pt, d)
            except Exception:
                alt = None
        if alt is None or isinstance(alt, sp.Limit) or alt.has(sp.Limit):
            try:
                s = sp.series(f, var, pt, 4).removeO()
                alt = sp.limit(s, var, pt, d)
            except Exception:
                alt = None
        # 兜底 3：1^∞/∞^0 型 —— 分别洛朗展开 g 与 ln(f) 再相乘取主导项
        if (alt is None or isinstance(alt, sp.Limit) or alt.has(sp.Limit)) and f.is_Pow:
            try:
                base, expo = f.base, f.exp
                lg = sp.series(sp.log(base), var, pt, 5).removeO()
                eg = sp.series(expo, var, pt, 5).removeO()
                u3 = sp.limit(sp.expand(lg * eg), var, pt, d)
                if (not isinstance(u3, sp.Limit) and not u3.has(sp.Limit)
                        and not u3.has(sp.nan) and u3.is_finite is not False):
                    alt = sp.exp(sp.simplify(u3))
                    out.line("洛朗主导项兜底: lim(g·ln f) = $%s$ -> e^该值" % sp.latex(u3))
            except Exception:
                pass
        if alt is not None and (isinstance(alt, sp.Limit) or alt.has(sp.Limit) or alt.has(sp.nan)):
            alt = None
        if alt is not None and not isinstance(alt, sp.Limit) and not alt.has(sp.Limit):
            out.line("兜底结果: $%s$" % sp.latex(alt))
            res = alt
        else:
            out.note("⚠️ 兜底也失败：sympy 未能求出该极限（结果不是答案；可尝试先化简/换元/取对数）")
    # 数值对账：符号极限与采样值比较，抓静默错答案（如 ∞）
    try:
        numval, drift = _num_limit(f, var, pt, d)
    except Exception:
        numval, drift = None, None
    if numval is not None and res is sp.S.NaN:
        out.note("符号路线失败 → 采用数值采样 %s（非闭式，需自行确认）" % _fmt_num(numval))
        res = _clean_num(_mp_to_sympy(numval, 12))
    if numval is not None and not isinstance(res, sp.Limit) and not res.has(sp.Limit):
        try:
            stable = (drift is not None) and (drift < 1e-6 * max(abs(complex(numval)), 1.0))
            out.line("数值采样: %s（相邻差 %s）" % (_fmt_num(numval), _fmt_num(drift, 3)))
            if res in (sp.oo, -sp.oo, sp.zoo):
                if stable and abs(complex(numval)) < 1e6:
                    out.note("⚠️ 符号给出发散值 %s，但数值采样有限且收敛（%s）：符号结果错误，"
                             "已改用采样值（请自行寻求闭式）" % (res, _fmt_num(numval)))
                    res = _clean_num(_mp_to_sympy(numval, 12))
                elif stable:
                    out.note("符号发散且采样也发散 -> 一致")
                else:
                    out.note("符号发散、采样也未收敛：无法对账，请换路线复核")
            elif res.is_number:
                rn = complex(sp.N(res, 20))
                diff = abs(complex(numval) - rn)
                # 相对误差分母用两者模长的较大者；都为 0 时才退化为绝对差
                den = max(abs(complex(numval)), abs(rn))
                rel = diff / den if den > 1e-12 else diff
                if rel <= 1e-4:
                    out.line("数值对账: 采样 %s ≈ 符号结果 -> PASS" % _fmt_num(numval))
                elif stable:
                    out.note("⚠️ 数值采样 %s 与符号结果 %s 偏差 %.3e：符号结果可疑"
                             % (_fmt_num(numval), sp.N(res, 12), diff))
                elif rel > 0.5:
                    # 采样虽未收敛，但两条路线差一个量级 —— 符号结果几乎确定是错的，禁止静默放行。
                    # 此处【刻意不做闭式反猜】：采样通常只有 6-7 位有效数字，
                    # nsimplify 会返回 -13/18+sqrt(385)/18 这类"看着干净"的伪闭式（2026-10-17 实测）。
                    newres = _clean_num(_mp_to_sympy(numval, 12))
                    out.note("⚠️ **符号结果与数值采样相差 %.0f%%**（符号 %s / 采样 %s）："
                             "sympy 的极限结果几乎确定是错的，已改用采样值。"
                             "采样仅约 6-7 位有效数字，求闭式请换路线，或自行用 nsimplify 回代验证"
                             % (rel * 100, sp.N(res, 10), _fmt_num(numval)))
                    res = newres
                else:
                    out.note("采样尚未收敛（相邻差偏大），但采样 %s 与符号结果接近（相对差 %.1e），暂采信符号结果"
                             % (_fmt_num(numval), rel))
        except Exception as e:
            out.note("数值对账异常（不影响符号结果）: %s: %s" % (type(e).__name__, e))
    out.set_result(res)
    out.line("结果: $%s$" % sp.latex(res))


def _term_degree(t, var):
    """单项按 var 的幂次排序用：非整数/符号幂次也能给出可比值。"""
    t = sp.expand(t)
    try:
        return float(sp.Poly(t, var).degree())
    except Exception:
        _, e = t.as_coeff_exponent(var)
        try:
            return float(e)
        except Exception:
            return 0.0


def cmd_series(out, args, opts):
    if len(args) not in (3, 4):
        raise ValueError("用法: series EXPR VAR POINT [N]")
    f = parse(args[0])
    var = _sym(args[1])
    pt = parse(args[2])
    n = int(args[3]) if len(args) == 4 else 6
    out.line("函数: $%s$" % sp.latex(f))
    out.line("展开: %s = %s 附近，阶数 %d" % (var.name, pt, n))
    res = sp.series(f, var, pt, n)
    out.set_result(res.removeO())
    out.line("结果: $%s$" % sp.latex(res))
    # 去掉 O 项的行与主行同序（按幂次降序），便于人工对照 c_{-1}
    no = res.removeO()
    terms = sorted(sp.Add.make_args(no), key=lambda t: _term_degree(t, var), reverse=True)
    out.line("去掉 O 项: $%s$" % sp.latex(sp.Add(*terms)))
    # --coeffs：按幂次从低到高列出系数（泰勒题直接读 a0,a1,a2,a3；选择题可对选项）
    if opts.get("coeffs"):
        try:
            shift = sp.expand(no.subs(var, var + pt))  # 平移到 0 附近再取系数
            p = sp.Poly(shift, var)
            cs = p.all_coeffs()[::-1]
            out.line("系数（低→高，c0 为常数项）: %s"
                     % " | ".join("c%d=%s" % (i, sp.latex(c)) for i, c in enumerate(cs)))
        except Exception as e:
            out.note("系数提取失败（可能含非多项式项）: %s: %s" % (type(e).__name__, e))


def cmd_sumprod(out, args, opts, func, name, sym):
    if len(args) != 4:
        raise ValueError("用法: %s VAR LO HI EXPR（注意顺序：先求和变量，再上下限，最后表达式）" % name)
    # 参数校验：VAR 必须是单个符号名，否则用户多半把 EXPR 写在了前面
    var_raw = args[0].strip()
    if not (var_raw.replace("_", "").isalnum() and not var_raw[0].isdigit()):
        raise ValueError(
            "第一个参数 %r 不是合法变量名。正确顺序为: %s VAR LO HI EXPR\n"
            "例如: %s n 1 oo \"1/n^2\"（若你写成了 %s \"表达式\" lo hi，请调换）"
            % (var_raw, name, name, name))
    k = _sym(var_raw)
    lo, hi = parse(args[1]), parse(args[2])
    f = parse(args[3])
    out.line("表达式: $%s$" % sp.latex(f))
    out.line("%s: %s = %s 到 %s" % (name, k.name, lo, hi))
    res = func(f, (k, lo, hi))
    unresolved = (res.has(sp.Sum) or res.has(sp.Product)
                  or isinstance(res, (sp.Sum, sp.Product)))
    if unresolved:
        out.note("⚠️ sympy 未能求出该%s的闭式（返回未求值形式）——不给出误导性数值" % name)
    out.set_result(res)
    out.line("结果: $%s$" % sp.latex(res))
    if not unresolved:
        try:
            out.line("数值: %s" % sp.N(res, 20))
        except Exception:
            pass
    # 数值对账：对收敛级数抽样部分和，抓符号错答案
    if hi == sp.oo and not unresolved:
        try:
            nv = sp.N(res, 20)
            if nv.is_number and nv.is_finite:
                kk = k
                term = f
                partial = sum(sp.N(term.subs(kk, j), 25) for j in range(int(sp.N(lo, 5)), int(sp.N(lo, 5)) + 4000))
                d = abs(sp.N(nv - partial, 10))
                if d > sp.Float("1e-3") * max(abs(nv), sp.Float(1)):
                    out.note("⚠️ 符号结果 %s 与 4000 项部分和 %s 偏差 %s：符号结果可疑"
                             % (sp.N(nv, 12), sp.N(partial, 12), sp.N(d, 6)))
                else:
                    out.line("部分和对账(4000 项): %s -> PASS" % sp.N(partial, 12))
        except Exception:
            pass


def _matrixify(s):
    """把字符串里顶层的 [[...],[...]] 片段包成 Matrix(...)，避免 sympify 把 list 当作数值序列相乘。"""
    out = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == "[":
            depth, j = 0, i
            while j < n:
                if s[j] == "[":
                    depth += 1
                elif s[j] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            literal = s[i:j + 1]
            out.append("Matrix(%s)" % literal)
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _sympify_matrix(s):
    """把 '[[1,2],[3,4]]' 或含 Matrix(...) 的表达式解析为 Matrix。
    注意：【不】应用 e→E 映射，避免矩阵元素名 e 被当成欧拉常数。"""
    s = s.strip()
    if "[[" in s and "Matrix(" not in s:
        s = _matrixify(s)
    ld = {"Matrix": sp.Matrix}
    ld.update(SYM_ASSUMPTIONS)
    return sp.sympify(s, locals=ld)


def cmd_matrix(out, args, opts):
    """matrix OP MAT... —— OP in det/inv/eigenvals/eigenvects/rref/rank/trace/transpose/
    nullspace/charpoly/pow/mul/add/sub/pow_expr"""
    if len(args) < 2:
        raise ValueError("用法: matrix OP MAT [ARG]；OP 见帮助")
    op = args[0].lower()
    if op in ("mul", "add", "sub"):
        mats = [_sympify_matrix(a) for a in args[1:]]
        if op == "mul":
            res = mats[0]
            for m in mats[1:]:
                res = res * m
        elif op == "add":
            res = mats[0]
            for m in mats[1:]:
                res = res + m
        else:
            res = mats[0]
            for m in mats[1:]:
                res = res - m
        out.set_result(res)
        out.line("%s = $%s$" % (op, sp.latex(res)))
        out.line("（展开形式）%s" % res)
        return
    if op in ("pow", "power"):
        if len(args) != 3:
            raise ValueError("用法: matrix pow MAT N（N 为整数，负整数表示逆的幂）")
        m = _sympify_matrix(args[1])
        n = int(args[2])
        res = m**n
        out.line("矩阵 (%dx%d):" % (m.rows, m.cols))
        out.line("$%s$" % sp.latex(m))
        out.set_result(res)
        out.line("A^%d = $%s$" % (n, sp.latex(res)))
        out.line("（展开形式）%s" % res)
        return
    if len(args) != 2:
        raise ValueError("用法: matrix OP MAT，OP in det/inv/eigenvals/eigenvects/rref/rank/trace/transpose/nullspace/charpoly/pow/mul/add/sub")
    mat_str = args[1]
    m = _sympify_matrix(mat_str)
    out.line("矩阵 (%dx%d):" % (m.rows, m.cols))
    out.line("$%s$" % sp.latex(m))
    if op == "det":
        res = m.det()
    elif op == "inv":
        res = m.inv()
    elif op == "eigenvals":
        res = m.eigenvals()
    elif op == "eigenvects":
        res = m.eigenvects()
    elif op == "rref":
        res = m.rref()[0]
    elif op == "rank":
        res = m.rank()
    elif op == "trace":
        res = m.trace()
    elif op == "transpose":
        res = m.T
    elif op == "nullspace":
        res = m.nullspace()
    elif op == "charpoly":
        lam = sp.Symbol("lambda")
        res = m.charpoly(lam).as_expr()
    else:
        raise ValueError("不支持的矩阵运算: %s" % op)
    out.set_result(res)
    out.line("%s = %s" % (op, res))
    out.line("结果: %s" % res)
    # 多项式结果（det/charpoly/trace）自动给系数表：行列式求某项系数是高频考点
    if op in ("det", "charpoly", "trace") and len(res.free_symbols) == 1:
        _v = list(res.free_symbols)[0]
        try:
            _p = sp.Poly(sp.expand(res), _v)
            _cs = _p.all_coeffs()[::-1]
            out.line("多项式系数（按 %s 次数低→高）: %s"
                     % (_v.name, " | ".join("c%d=%s" % (i, sp.latex(c)) for i, c in enumerate(_cs))))
        except Exception:
            pass
    if op == "eigenvals":
        try:
            ev = list(m.eigenvals().keys())
            pos = sum(1 for e in ev if sp.N(e) > 0)
            neg = sum(1 for e in ev if sp.N(e) < 0)
            zer = sum(1 for e in ev if sp.simplify(e) == 0)
            out.note("惯性指数（按特征值符号）: 正 %d / 负 %d / 零 %d —— 可直接读二次型规范形" % (pos, neg, zer))
            out.line("结果: 正惯性 %d / 负惯性 %d / 零 %d" % (pos, neg, zer))
        except Exception:
            pass


def cmd_matrix_eqsolve(out, args, opts):
    """matrix eqsolve "LHS == RHS" VARS —— 把矩阵方程逐元素展平成标量方程组再解。
    未知量用符号矩阵元素（如 [[a,b],[c,d]]）书写。"""
    if len(args) != 2:
        raise ValueError('用法: matrix eqsolve "[[1,1],[0,1]]*[[a,b],[c,d]] == [[a,b],[c,d]]*[[1,1],[0,1]]" a,b,c,d')
    kv = split_top_kv(args[0], "==")
    if kv is None:
        raise ValueError("需要 == 连接两边")
    lhs = _sympify_matrix(kv[0])
    rhs = _sympify_matrix(kv[1])
    if lhs.shape != rhs.shape:
        raise ValueError("两边矩阵形状不同: %s vs %s" % (lhs.shape, rhs.shape))
    diff = lhs - rhs
    eqs = [sp.expand(diff[i, j]) for i in range(diff.rows) for j in range(diff.cols)]
    eqs = [e for e in eqs if e != 0]
    syms = [sp.Symbol(s.strip()) for s in split_top(args[1])] if args[1].strip().lower() != "all" \
        else sorted(set().union(*[e.free_symbols for e in eqs]), key=lambda s: s.name)
    out.line("矩阵方程: $%s = %s$" % (sp.latex(lhs), sp.latex(rhs)))
    out.line("展平得到 %d 个标量方程，未知量 %s" % (len(eqs), [s.name for s in syms]))
    sols = sp.solve(eqs, syms, dict=True)
    if not sols:
        out.line("结果: 无解（或仅平凡约束）")
        out.set_result(sp.S.EmptySet)
        return
    for i, sd in enumerate(sols, 1):
        out.line("解 %d: %s" % (i, ", ".join("%s = %s" % (k, v) for k, v in sd.items())))
    out.set_result(sols)
    if syms:
        free = [s for s in syms if s not in sols[0]]
        if free:
            out.note("自由参数: %s（通解含这些任意常数）" % ", ".join(s.name for s in free))


def cmd_evalf(out, args, opts):
    if len(args) not in (1, 2):
        raise ValueError("用法: evalf EXPR [N]")
    f = parse(args[0])
    n = int(args[1]) if len(args) == 2 else 20
    out.line("输入: $%s$" % sp.latex(f))
    res = sp.N(f, n)
    out.set_result(res)
    out.line("数值(%d 位): %s" % (n, res))


def cmd_subs(out, args, opts):
    """subs EXPR "k=v,k=v"：代入具体值求值（配合 diff / dsolve / integrate 结果的落地）"""
    if len(args) != 2:
        raise ValueError('用法: subs EXPR "k=v,k=v"（例：subs "t^2+1" "t=0"；'
                         '多元：subs "x*y" "x=2,y=3"）')
    expr = parse(args[0])
    mapping = {}
    for part in split_top(args[1]):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError("代入项缺少 '='：%r（应形如 t=0）" % part)
        name, val = part.split("=", 1)
        name, val = name.strip(), val.strip()
        if not name or not val:
            raise ValueError("代入项不完整：%r" % part)
        mapping[_sym(name)] = parse(val)
    out.line("代入: %s" % ", ".join("%s=%s" % (k, sp.latex(v)) for k, v in mapping.items()))
    res = expr.subs(mapping)
    try:
        res = sp.simplify(res)
    except Exception:
        pass
    if res.has(sp.zoo) or res.has(sp.nan):
        out.note("⚠️ 代入后出现 zoo/nan：可能有定义域问题（如除以 0、对数在非正处）")
    out.set_result(res)
    out.line("结果: $%s$" % sp.latex(res))
    if res.is_number:
        try:
            out.line("数值: %s" % sp.N(res, 20))
        except Exception:
            pass


def cmd_coeff(out, args, opts):
    """coeff EXPR VAR N：取 VAR^N 的系数（行列式/泰勒求某项系数）"""
    if len(args) != 3:
        raise ValueError("用法: coeff EXPR VAR N（如 coeff \"x^4-5*x^3+2\" x 3 → -5）")
    f = parse(args[0])
    var = _sym(args[1])
    try:
        n = int(args[2])
    except ValueError:
        raise ValueError("N 必须是整数，收到 %r" % args[2])
    e = sp.expand(f)
    out.line("展开: $%s$" % sp.latex(e))
    c = e.coeff(var, n)
    out.set_result(c)
    out.line("结果: $%s$" % sp.latex(c))
    out.note("%s^%d 的系数" % (var.name, n))


def cmd_latex(out, args, opts):
    if len(args) != 1:
        raise ValueError("用法: latex EXPR")
    f = parse(args[0])
    out.set_result(f)
    out.line(sp.latex(f))


def cmd_nsimplify(out, args, opts):
    if len(args) < 1:
        raise ValueError("用法: nsimplify NUMBER [CONSTS]，CONSTS 形如 pi,E,sqrt(2)")
    num = parse(args[0], rational=False)
    consts = [parse(c, rational=False) for c in split_top(args[1])] if len(args) > 1 else []
    if consts:
        out.line("常数池: %s" % ", ".join(str(c) for c in consts))
    res = sp.nsimplify(num, constants=consts)
    out.set_result(res)
    out.line("数值输入: %s" % num)
    out.line("闭式猜测: $%s$" % sp.latex(res))
    try:
        out.line("回代数值: %s" % sp.N(res, 20))
        out.line("相对偏差: %s" % sp.N(abs(res - num) / max(abs(num), sp.Float(1e-300)), 10))
    except Exception:
        pass


def cmd_gamma_simp(out, args, opts, func, name):
    if len(args) != 1:
        raise ValueError("用法: %s EXPR" % name)
    f = parse(args[0])
    out.line("输入: $%s$" % sp.latex(f))
    res = func(f)
    out.set_result(res)
    out.line("结果: $%s$" % sp.latex(res))


def cmd_check(out, args, opts):
    """双通道验证：符号化简 + 30 位数值对账。"""
    if len(args) == 3 and args[1] == "==":
        args = [args[0] + " == " + args[2]]
    if len(args) != 1:
        raise ValueError("用法: check LHS == RHS（可写成单个字符串，或 LHS == RHS 三个参数）")
    kv = split_top_kv(args[0], "==")
    if kv is None:
        raise ValueError("需要 == 连接的两边")
    lhs, rhs = parse(kv[0]), parse(kv[1])
    out.line("左边: $%s$" % sp.latex(lhs))
    out.line("右边: $%s$" % sp.latex(rhs))
    diff = sp.simplify(sp.expand(lhs - rhs))
    sym_ok = (diff == 0)
    dnum = None
    num_channel = True
    if (lhs - rhs).free_symbols:
        out.note("表达式含自由符号 %s，跳过数值通道（符号通道 %s）"
                 % (sorted((lhs - rhs).free_symbols, key=str), "PASS" if sym_ok else "未证/不等"))
        num_ok = sym_ok
        num_channel = False
    else:
        try:
            dnum = sp.N(lhs - rhs, 30)
            scale = max(abs(sp.N(rhs, 30)), sp.Float(1))
            num_ok = abs(dnum) < sp.Float("1e-25") * scale
        except Exception:
            dnum, num_ok = None, False
            num_channel = False
    out.line("符号通道: simplify(lhs-rhs) = %s -> %s" % (diff, "PASS" if sym_ok else "未证/不等"))
    if num_channel:
        out.line("数值通道: (lhs-rhs)@30位 = %s -> %s" % (dnum, "PASS" if num_ok else "FAIL"))
    out.set_result(diff)
    if sym_ok or num_ok:
        if sym_ok and num_channel and num_ok:
            how = "符号+数值双通道"
        elif sym_ok and not num_channel:
            how = "符号通道；数值通道因含自由符号跳过"
        elif sym_ok:
            how = "符号通道（数值通道给出 FAIL，注意数值放大误差）"
        else:
            how = "仅数值通道"
        out.line("结论: 两边相等（%s）" % how)
        if not sym_ok:
            out.note("符号通道未能化简到 0（可能是 sympy 缺相应恒等式，如 Gamma 反射公式），仅数值 30 位一致")
        return
    out.line("结论: 两边不相等")
    raise RuntimeError("CHECK_FAILED")


def _mp_to_sympy(v, digits=20):
    """mpmath 数 -> sympy（支持复数；mpmath 的 mpc 不能直接进 Float）。"""
    try:
        if isinstance(v, (mp.mpc, complex)):
            re_v = mp.mpf(v.real)
            im_v = mp.mpf(v.imag)
            return sp.Float(str(re_v), digits) + sp.I * sp.Float(str(im_v), digits)
        return sp.Float(str(mp.mpf(v)), digits)
    except Exception:
        return sp.sympify(str(v))


def _laurent_c_minus1(f, var, pt, n=6):
    """用级数展开取洛朗系数 c_{-1}（sp.residue 失败时的降级路径）。
    含其他自由符号（如符号阶数 m）时立即放弃：符号级数展开可能死循环。"""
    g = f if pt == 0 else f.subs(var, var + pt)
    extra = g.free_symbols - {var}
    if extra:
        raise ValueError("表达式含其他自由符号 %s，符号级数降级不适用（会死循环）" % sorted(extra, key=str))
    s = sp.series(g, var, 0, n).removeO()
    c = s.coeff(var, -1)
    if c == 0 and n < 14:
        # 高阶极点可能展开阶数不够，提高阶数重试
        s = sp.series(g, var, 0, 14).removeO()
        c = s.coeff(var, -1)
    return sp.simplify(c)


def _clean_num(x):
    """剥掉数值噪声虚部（|Im| < 1e-15·|Re| 视为 0）。"""
    try:
        if x.is_number:
            im, re = sp.im(x), sp.re(x)
            if abs(sp.N(im, 20)) < sp.Float("1e-15") * max(abs(sp.N(re, 20)), sp.Float(1)):
                return sp.re(x)
    except Exception:
        pass
    return x


def _num_residue(f, var, pt, r=0.5):
    """数值围道留数：Res = (1/2πi)∮_{|z-pt|=r} f dz（mpmath 高精度）。
    双半径自检：r 与 r/3 不一致说明圆内圈进其他奇点，返回 ok=False。
    这是 residue 的独立第二通道，用来抓 sympy 对本質奇点静默返回 0 的 bug。"""
    try:
        fa = sp.lambdify(var, f, "mpmath")
    except Exception as e:
        return None, None, "lambdify 失败: %s" % e
    if not (pt.is_number and pt.is_finite):
        return None, None, "留数点 %s 不可数值化（符号/无穷）" % pt
    try:
        pc = complex(sp.N(pt, 30))
    except Exception as e:
        return None, None, "留数点数值化失败: %s" % e

    def contour(rr):
        def integrand(t):
            z = mp.mpc(pc.real, pc.imag) + rr * mp.exp(1j * mp.mpf(t))
            dz = 1j * rr * mp.exp(1j * mp.mpf(t))
            return fa(z) * dz
        return mp.quad(integrand, [0, 2 * mp.pi]) / (2j * mp.pi)

    try:
        v1 = _mp_to_sympy(contour(r), 15)
        v2 = _mp_to_sympy(contour(r / 3.0), 15)
        v1 = _clean_num(v1)
        v2 = _clean_num(v2)
        d = abs(sp.N(v1 - v2, 15))
        scale = max(abs(sp.N(v1, 15)), abs(sp.N(v2, 15)), sp.Float(1e-8))
        consistent = bool(d < sp.Float("1e-8") * scale or d < sp.Float("1e-13"))
        if not consistent:
            return v1, False, ("半径 r=%s 与 r/3=%s 的围道结果不一致（%s vs %s）："
                               "圆内可能圈进其他奇点，请显式给更小 RADIUS"
                               % (r, r / 3.0, sp.N(v1, 8), sp.N(v2, 8)))
        return v1, True, None
    except Exception as e:
        return None, None, "%s: %s" % (type(e).__name__, e)


def _num_residue_oo(f, var, R=50.0):
    """∞ 留数的数值通道：Res_oo = -(1/2πi)∮_{|z|=R} f dz（大圆，逆时针）。"""
    try:
        fa = sp.lambdify(var, f, "mpmath")
    except Exception as e:
        return None, "lambdify 失败: %s" % e
    try:
        def integrand(t):
            z = R * mp.exp(1j * mp.mpf(t))
            dz = 1j * R * mp.exp(1j * mp.mpf(t))
            return fa(z) * dz
        val = -mp.quad(integrand, [0, 2 * mp.pi]) / (2j * mp.pi)
        return _mp_to_sympy(val, 15), None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def _res_at_oo(f, var):
    """Res_{var=oo} f = -Res_{w=0} f(1/w)/w^2；sp.residue 失败则降级到级数取 c_{-1}。"""
    w = sp.Dummy("w")
    g = f.subs(var, 1 / w) / w**2
    try:
        return sp.simplify(-sp.residue(g, w, 0))
    except Exception:
        try:
            return -_laurent_c_minus1(g, w, 0)
        except Exception:
            return None


def cmd_residue(out, args, opts):
    """residue EXPR VAR POINT [RADIUS]：单点留数；POINT 可为 oo。
    双通道：符号（sp.residue / 级数 c_{-1}） + 数值围道积分（独立第二通道）。
    ⚠️ sympy 对本質奇点可能静默返回 0，数值通道是唯一能抓到的手段。"""
    if len(args) not in (3, 4):
        raise ValueError("用法: residue EXPR VAR POINT [RADIUS]（POINT 可为数字/符号/oo）")
    f = parse(args[0])
    var = _sym(args[1])
    pt_raw = args[2].strip()
    radius = float(args[3]) if len(args) == 4 else 0.5
    out.line("函数: $%s$" % sp.latex(f))
    is_oo = pt_raw in ("oo", "inf", "Infinity")
    if is_oo:
        out.line("求 %s -> oo 处的留数（变换 z=1/w, Res=-Res[f(1/w)/w^2, w=0]）" % var.name)
        r_sym = _res_at_oo(f, var)
        r_num, err = _num_residue_oo(f, var)
        num_ok = True  # 大圆数值通道无半径自检，视为可信但会在不一致时降权
    else:
        pt = parse(pt_raw)
        out.line("求 %s = %s 处的留数" % (var.name, pt))
        try:
            r_sym = sp.residue(f, var, pt)
        except Exception as e:
            out.note("sp.residue 失败（%s: %s），降级为级数展开取 c_{-1}" % (type(e).__name__, e))
            try:
                r_sym = _laurent_c_minus1(f, var, pt)
            except Exception as e2:
                out.note("级数降级也失败（%s: %s），只能依赖数值通道" % (type(e2).__name__, e2))
                r_sym = None
        r_num, num_ok, err = _num_residue(f, var, pt, radius)
    if err:
        out.note("数值围道通道提示: %s" % err)
    # 判定
    res = None
    if r_sym is not None:
        res = sp.simplify(r_sym)
        out.line("符号通道结果: $%s$" % sp.latex(res))
    if r_num is not None:
        out.line("数值围道结果: $%s$（双半径自检 %s）" % (
            sp.latex(sp.N(r_num, 12)), "通过" if num_ok else "未通过"))
    if r_sym is None and r_num is not None:
        res = r_num
        out.note("仅数值通道获得结果（符号通道不可用），结果为数值形式，需自行闭式化/验证")
    if r_sym is not None and r_num is not None:
        try:
            d = abs(sp.N(sp.simplify(r_sym - r_num), 20))
            scale = max(abs(sp.N(r_num, 20)), abs(sp.N(r_sym, 20)), sp.Float(1))
            # 绝对下限 1e-13：两通道都接近 0 时（真留数为 0）不该报警
            if d < sp.Float("1e-9") * scale and d < sp.Float("1e-13"):
                out.line("两通道: PASS（一致）")
            elif not num_ok:
                out.line("两通道: ⚠️ 数值围道双半径自检未通过，符号结果保留（数值通道不可靠）")
            else:
                out.line("两通道: ⚠️ 不一致！符号 $%s$ vs 数值 $%s$（差 %s）"
                         % (sp.latex(sp.N(r_sym, 12)), sp.latex(sp.N(r_num, 12)), sp.N(d, 6)))
                if sp.simplify(r_sym) == 0 and abs(sp.N(r_num, 20)) > sp.Float("1e-12"):
                    out.note("符号通道为 0 而数值围道非零（双半径自检已通过）：sympy 漏判"
                             "（本質奇点/高阶极点常见）。⚠️ 采用数值围道结果，符号结果不可信")
                    res = r_num
                else:
                    out.note("两通道有差异：检查半径内是否含其他奇点（可显式给 RADIUS 缩小）或分支切割")
        except Exception as e:
            out.note("两通道比较失败: %s" % e)
    if res is None:
        out.line("⚠️ 两通道均未获得结果，无法给出留数（拒绝输出 0，避免静默错答案）")
        out.note("若表达式含符号参数（如阶数 m），请代入具体数值后再求；"
                 "若是本质奇点，可尝试 series 展开手工取 1/z 系数")
        out.set_result(sp.S.NaN)
        return
    out.set_result(res)
    out.line("结果: $%s$" % sp.latex(res))
    try:
        out.line("数值: %s" % sp.N(res, 20))
    except Exception:
        pass


def cmd_residues(out, args, opts):
    """residues EXPR VAR：自动找有理/亚纯函数全部有限极点并求各点留数。
    非有理（含本质奇点/支点）时降级为提示，用 residue 手动指定。"""
    if len(args) != 2:
        raise ValueError("用法: residues EXPR VAR")
    f = parse(args[0])
    var = _sym(args[1])
    out.line("函数: $%s$" % sp.latex(f))
    fr = sp.cancel(sp.together(f))
    num, den = fr.as_numer_denom()
    # 周期/超越分母警示：极点可能是无穷族，枚举不完整
    periodic_fns = {sp.sin, sp.cos, sp.tan, sp.cot, sp.sec, sp.csc, sp.sinh, sp.cosh, sp.tanh, sp.coth}
    if den.has(*periodic_fns):
        out.note("⚠️ 分母含周期函数 %s：奇点为无穷族（如 2kπ），自动枚举只覆盖已解出的有限个，"
                 "「有限极点留数之和」对无穷族无意义（应收敛性问题），请勿直接引用"
                 % sorted({fn.__name__ for fn in periodic_fns if den.has(fn)}))
    poles = []
    try:
        roots = sp.solve(sp.Eq(den, 0), var)
        poles = [sp.simplify(r) for r in roots]
        poles = sorted(set(poles), key=lambda p: (sp.re(sp.N(p, 10)) if p.is_finite else 0))
    except Exception as e:
        out.note("自动找极点失败（%s）；对本质奇点/支点请改用 residue 手动指定" % e)
        poles = []
    if not poles:
        out.note("未能从分母自动解出极点（分母可能含超越/复合结构）。请用 residue 逐点求。")
        out.set_result(sp.S.NaN)
        return
    total = sp.S.Zero
    for p in poles:
        try:
            r = sp.simplify(sp.residue(f, var, p))
        except Exception as e:
            out.line("%s = %s: 求留数失败 (%s)" % (var.name, p, e))
            continue
        total += r
        out.line("极点 %s = %s : Res = $%s$" % (var.name, sp.latex(p), sp.latex(r)))
    total = sp.simplify(total)
    out.set_result(total)
    out.line("有限极点留数之和: $%s$ （数值: %s）" % (sp.latex(total), sp.N(total, 15)))
    try:
        roo = _res_at_oo(f, var)
        out.line("Res_oo = $%s$ （校验: Sum_finite + Res_oo = $%s$，应化简为 0）"
                 % (sp.latex(roo), sp.latex(sp.simplify(total + roo))))
    except Exception:
        out.note("Res_oo 计算失败（函数在 oo 可能不是孤立奇点）")


def cmd_dsolve(out, args, opts):
    """dsolve EQ FUNC：解 ODE 并用 checkodesol 回代验证。EQ 缺 '=' 视为 expr=0。
    注意：sympy 求不出闭式时可能返回截断级数（含 Order 项），会如实标注。"""
    if len(args) != 2:
        raise ValueError("用法: dsolve EQ FUNC（如 dsolve \"(1-z)*Derivative(w(z),(z,2)) + z*Derivative(w(z),z) - w(z) = 0\" w）")
    eq_expr, f, ivar, fname = _parse_ode(args)
    eq = sp.Eq(eq_expr, 0)
    out.line("方程: $%s$" % sp.latex(eq))
    out.line("未知函数: %s(%s)" % (fname, ivar.name))
    if eq_expr.has(sp.Integral):
        out.note("⚠️ 方程含积分项（积分方程），sympy dsolve 无法直接求解——**拒绝输出可疑解**。"
                 "正确路线：对自变量求导化为 ODE。例：f' + a f = ∫₀ˣf(t)dt 两边求导得 "
                 "f'' + a f' = f；初值由原式取 x=0 得 f'(0) = −a f(0)。"
                 "再对化得的 ODE 用 dsolve，最后回代原方程验证")
        out.set_result(sp.S.NaN)
        return
    try:
        hints = sp.classify_ode(eq, f)
        out.line("sympy 提示(前5): %s" % ", ".join(list(hints)[:5]))
    except Exception:
        hints = []
    sols = sp.dsolve(eq, f)
    sol_list = sols if isinstance(sols, list) else [sols]
    for i, s in enumerate(sol_list, 1):
        rhs = s.rhs if hasattr(s, "rhs") else s
        if rhs.has(sp.Order):
            out.line("解 %d: $%s$" % (i, sp.latex(rhs)))
            out.line("  ⚠️ 这只是截断级数近似（sympy 无闭式解），不作为精确答案；请改用 ode-series")
            continue
        try:
            ok, expr = sp.checkodesol(eq, s, f)
            out.line("解 %d: $%s$" % (i, sp.latex(rhs)))
            out.line("  回代验证(checkodesol): %s（残差 $%s$）" % ("PASS" if ok else "FAIL", sp.latex(sp.simplify(expr))))
        except Exception as e:
            out.line("解 %d: $%s$  回代验证异常: %s" % (i, sp.latex(rhs), e))
    out.set_result(sols)
    if sol_list:
        _rhs = sol_list[0].rhs if hasattr(sol_list[0], "rhs") else sol_list[0]
        out.line("结果: $%s$" % sp.latex(_rhs))
        if len(sol_list) > 1:
            out.note("共 %d 组解，上面只把首组写成「结果」行；其余见「解 2/3…」" % len(sol_list))


# ---------------------------------------------------------------- ODE 级数求解辅助（自研，绕开 sympy hint bug）

def _parse_ode(args):
    """解析 'EQ FUNC' -> (eq_expr, f, ivar, fname)。EQ 缺 '=' 视为 expr=0。
    '==' 与 '=' 等价（2026-10-17 修：此前 dsolve 只按 '=' 切分，写 '==' 会抛裸 SyntaxError）。"""
    eq_s = args[0].replace("==", "=")
    kv = split_top_kv(eq_s, "=")
    try:
        if kv is None:
            eq_expr = parse(eq_s)
        else:
            eq_expr = parse(kv[0]) - parse(kv[1])
    except Exception as e:
        raise ValueError("方程表达式解析失败：收到 %r（%s: %s）。"
                         "提示：导数写 Derivative(y(x),x)；等号可写 '=' 或 '=='；"
                         "也可只写表达式表示 '=0'" % (args[0], type(e).__name__, e))
    fname = args[1].strip()
    # 兼容 y(x) / y 两种写法：只取函数名，自变量从导数中反推
    if "(" in fname:
        fname = fname.split("(", 1)[0].strip()
    ivars = set()
    for der in eq_expr.atoms(sp.Derivative):
        ivars.update(der.variables)
    if not ivars:
        raise ValueError("方程中未出现对自变量的导数，无法确定自变量")
    ivar = sorted(ivars, key=str)[0]
    f = sp.Function(fname)(ivar)
    return eq_expr, f, ivar, fname


def _ode_order(eq_expr):
    return max(d.derivative_count for d in eq_expr.atoms(sp.Derivative))


def _coeff_degs(expr, f, ivar, dorder):
    """返回 [(j, lmin_j)]：c_j(z)=coef of w^{(j)} 中 ivar 的【最低】幂次。
    完整性规则用最低次：幂次 P 上第 j 项的最大级数指标 k=P+j-lmin_j（常数项贡献最大 k），
    只有 k<=n 时方程才未被截断污染。"""
    out = []
    for j in range(dorder + 1):
        c = expr.coeff(sp.Derivative(f, ivar, j)) if j > 0 else expr.coeff(f)
        if c != 0:
            c = sp.expand(c)
            lmin = None
            for term in sp.Add.make_args(c):
                _, e = term.as_coeff_exponent(ivar)
                if lmin is None or e < lmin:
                    lmin = e
            if lmin is None:
                lmin = sp.S.Zero
            out.append((j, int(lmin) if lmin.is_number else 0))
    return out


def _complete_powers(cd, n, lo, hi):
    """幂次 P 完整（所有非零项的级数指标 k=P+j-lmin_j<=n）的列表。"""
    keep = []
    for P in range(lo, hi + 1):
        if all(P + j - lmin <= n for j, lmin in cd):
            keep.append(P)
    return keep


def _resid_check(eq_expr, f, ivar, ws, cd, n, divide=None):
    """回代验证：残差只允许出现在'不完整幂次'上（截断伪影）。返回 (ok, bad_powers)。"""
    resid = sp.expand((eq_expr.doit()).subs(f, ws).doit())
    if divide is not None:
        resid = sp.expand(resid / divide)
    if resid == 0:
        return True, []
    lo = _min_exponent(resid, ivar)
    lo = int(lo) if (lo is not None and lo.is_number and lo != sp.S.NegativeInfinity) else 0
    try:
        poly = sp.Poly(resid, ivar)
    except sp.PolynomialError:
        return False, None
    bad = []
    for k in range(0, poly.degree() + 1):
        c = sp.simplify(poly.coeff_monomial(ivar**k))
        if c == 0:
            continue
        P = k + lo
        # P 在预平移坐标下是否完整幂次（完整幂次上残差必须为 0）
        if all(P + j - dj <= n for j, dj in cd):
            bad.append(P)
    return (not bad), bad


def _min_exponent(expr, ivar):
    """expr 展开后 ivar 的最小幂次（可为符号表达式如 rho-1）；恒为 0 的表达式返回 0。"""
    expr = sp.expand(expr)
    if expr == 0:
        return sp.S.Zero
    mn = None
    for term in sp.Add.make_args(expr):
        _, e = term.as_coeff_exponent(ivar)
        if e is sp.S.NegativeInfinity or e is sp.S.Infinity:
            continue
        if mn is None or e < mn:
            mn = e
    return mn if mn is not None else sp.S.Zero


def _power_series_ordinary(eq_expr, f, ivar, n, out):
    """常点幂级数：w = Σ_{k=0}^{n} a_k z^k，逐次幂收集系数解线性递推。
    只保留"完整"幂次方程（所有项的级数指标 k=P+j-d_j<=n），截断伪影幂次丢弃。"""
    dorder = _ode_order(eq_expr)
    a = sp.symbols("a0:%d" % (n + 1))
    w = sum(a[k] * ivar**k for k in range(n + 1))
    resid = sp.expand((eq_expr.doit()).subs(f, w).doit())
    try:
        poly = sp.Poly(resid, ivar)
    except sp.PolynomialError:
        out.note("残差不是 z 的多项式（系数可能非常函数），常点法失效")
        return None
    expr0 = sp.expand(eq_expr.doit())
    cd = _coeff_degs(expr0, f, ivar, dorder)
    deg = poly.degree()
    keep = _complete_powers(cd, n, 0, deg)
    eqs = [sp.expand(poly.coeff_monomial(ivar**P)) for P in keep]
    unknowns = list(a[dorder:])
    eqs = [e for e in eqs if e != 0]
    solset = sp.linsolve(eqs, unknowns)
    if not solset:
        out.note("线性递推无解：截断阶数 n=%d 可能不够，或方程非常点/非线性" % n)
        return None
    tup = list(solset)[0]
    subst = dict(zip(unknowns, tup))
    w_sol = w.subs(subst)
    free = [a[k] for k in range(dorder)]
    leftover = {s for s in w_sol.free_symbols if s not in free and s != ivar}
    if leftover:
        out.note("截断阶数 n=%d 下仍有未定系数 %s：级数未闭合，建议增大 n 重试" % (n, leftover))
    w_sol = sp.collect(sp.expand(w_sol), free)
    basis = []
    for k in range(dorder):
        vals = {a[j]: (1 if j == k else 0) for j in range(dorder)}
        basis.append(sp.expand(w_sol.subs(vals)))
    return basis


def _frobenius_regular(eq_expr, f, ivar, n, out):
    """正则奇点 Frobenius：w = z^ρ Σ_{k=0}^{n} a_k z^k。返回 [(rho, w)] 列表。
    负幂次先整体平移，再丢最高 dorder 个幂次方程。"""
    dorder = _ode_order(eq_expr)
    rho = sp.Symbol("rho")
    a = sp.symbols("b0:%d" % (n + 1))
    series = sum(a[k] * ivar**k for k in range(n + 1))
    w = ivar**rho * series
    resid = sp.expand((eq_expr.doit()).subs(f, w).doit() / ivar**rho)
    if resid == 0:
        out.note("符号残差恒为 0（方程被级数恒等满足？检查输入）")
        return None
    mn = _min_exponent(resid, ivar)
    shift = -mn if (mn.is_number and mn != sp.S.NegativeInfinity and mn < 0) else 0
    if shift:
        resid = sp.expand(resid * ivar**shift)
    try:
        poly = sp.Poly(resid, ivar)
    except sp.PolynomialError:
        out.note("残差含 z 的非整数/符号幂次，Frobenius 收集失败")
        return None
    deg = poly.degree()
    lo = int(mn) if (mn.is_number and mn != sp.S.NegativeInfinity) else 0
    expr0 = sp.expand(eq_expr.doit())
    cd = _coeff_degs(expr0, f, ivar, dorder)
    # 预平移坐标下幂次 P 对应平移后 P+shift；完整幂次按预平移坐标算
    keep = _complete_powers(cd, n, lo, deg - shift)
    eqs = {P + shift: sp.expand(poly.coeff_monomial(ivar**(P + shift)))
           for P in keep}
    eqs = {k: v for k, v in eqs.items() if v != 0}
    if not eqs:
        out.note("残差恒为零？检查 n")
        return None
    kmin = min(eqs.keys())
    indicial = sp.expand(eqs[kmin])
    if not indicial.has(a[0]):
        out.note("最低次系数不含 a0，结构异常: %s" % indicial)
        return None
    indicial_expr = sp.expand(sp.cancel(indicial / a[0]))
    roots = sp.solve(sp.Eq(indicial_expr, 0), rho)
    out.line("指标方程: $%s = 0$ ，根 ρ = %s" % (sp.latex(indicial_expr), roots))
    results = []
    unknowns = list(a)  # 全部 b 作未知量：共振根可能强制 b0=0，由 linsolve 自行参数化
    for r in roots:
        eqs_r = [sp.expand(e.subs(rho, r)) for e in eqs.values()]
        solset = sp.linsolve(eqs_r, unknowns)
        if not solset:
            out.note("ρ = %s: 递推无解（n 太小或非正则奇点）" % r)
            continue
        tup = list(solset)[0]
        subst = dict(zip(unknowns, tup))
        ws = (ivar**rho * series).subs(rho, r).subs(subst)
        free_syms = sorted([s for s in ws.free_symbols if s in a], key=str)
        if not free_syms:
            out.note("ρ = %s: 递推强制所有系数为 0，该根无 Frobenius 解（对数解情形）" % r)
            continue
        if len(free_syms) > dorder:
            out.note("ρ = %s: 自由参数 %s 多于方程阶数，n=%d 递推未闭合，建议增大 n" % (r, free_syms, n))
        ws = sp.collect(sp.expand(ws), free_syms)
        results.append((r, ws))
    if len(roots) == 2:
        d = sp.simplify(roots[0] - roots[1])
        if d.is_integer and d != 0:
            out.note("两指标根差为整数 %s：第二解一般含对数项，无法自动给出；"
                     "可用降阶法 w2 = w1·∫exp(−∫P dz)/w1² dz 手工求" % d)
    return results


def cmd_ode_series(out, args, opts):
    """ode-series EQ FUNC [N]：z=0 邻域幂级数解。
    自动判常点/正则奇点：先试常点待定系数法，失败再试 Frobenius。
    自研实现，不依赖 sympy 的 2nd_power_series_* hint（有 bug）。"""
    if len(args) not in (2, 3):
        raise ValueError("用法: ode-series EQ FUNC [N]")
    eq_expr, f, ivar, fname = _parse_ode(args[:2])
    n = int(args[2]) if len(args) == 3 else 10
    out.line("方程: $%s = 0$" % sp.latex(eq_expr))
    out.line("在 %s = 0 附近求幂级数解，截断阶数 n=%d" % (ivar.name, n))
    # 判点型：p(z)=-coef_w'/coef_w'', q(z)=-coef_w/coef_w'' 在 0 处解析则常点
    expr = sp.expand(eq_expr.doit())
    cw2 = expr.coeff(sp.Derivative(f, (ivar, 2)))
    cw1 = expr.coeff(sp.Derivative(f, ivar))
    cw0 = (expr - cw2 * sp.Derivative(f, (ivar, 2)) - cw1 * sp.Derivative(f, ivar))
    if cw0 != 0 and not cw0.has(f):
        raise ValueError("无法把方程整理为线性形式 w'' + p w' + q w（cw0=%s 不含 %s）" % (cw0, f))
    p = sp.simplify(-cw1 / cw2)
    q = sp.simplify(-cw0 / (cw2 * f))
    dorder = _ode_order(eq_expr)
    cd = _coeff_degs(expr, f, ivar, dorder)
    ordinary = True
    try:
        if sp.limit(p, ivar, 0) not in (sp.S.NaN,) and not p.has(ivar) or sp.limit(p, ivar, 0).is_finite:
            pass
    except Exception:
        pass
    try:
        p0 = sp.limit(p, ivar, 0)
        q0 = sp.limit(q, ivar, 0)
        ordinary = p0.is_finite and q0.is_finite
    except Exception:
        ordinary = False
    if ordinary:
        out.line("点型判定: z=0 为常点 (p=%s, q=%s 在 0 解析)" % (sp.latex(p), sp.latex(q)))
        res = _power_series_ordinary(eq_expr, f, ivar, n, out)
        if res is None:
            out.note("常点法失败，转 Frobenius 再试")
            res_f = _frobenius_regular(eq_expr, f, ivar, n, out)
            if res_f:
                for r, ws in res_f:
                    out.line("Frobenius 解 (ρ=%s): $%s$" % (sp.latex(r), sp.latex(ws)))
            out.set_result(sp.S.Zero)
            return
        basis = res
        for i, ws in enumerate(basis, 1):
            out.line("独立解 %d (第 %d 个自由系数=1，其余=0): $%s$" % (i, i - 1, sp.latex(ws)))
        # 回代验证（只惩罚完整幂次上的残差）
        for i, ws in enumerate(basis, 1):
            ok, bad = _resid_check(eq_expr, f, ivar, ws, cd, n)
            out.line("解 %d 回代: %s" % (i, "PASS（残差仅为截断伪影）" if ok else
                                         "FAIL，完整幂次 %s 上有残差" % bad))
        out.set_result(sp.Add(*basis))
        return
    # 非常点 -> 正则奇点判定
    zp = sp.simplify(ivar * p)
    zq = sp.simplify(ivar**2 * q)
    reg = False
    try:
        reg = sp.limit(zp, ivar, 0).is_finite and sp.limit(zq, ivar, 0).is_finite
    except Exception:
        reg = False
    if not reg:
        out.note("z=0 不是正则奇点（zp=%s, zq=%s），Frobenius 不适用" % (sp.latex(zp), sp.latex(zq)))
        out.set_result(sp.S.NaN)
        return
    out.line("点型判定: z=0 为正则奇点 (zp=%s, zq=%s)" % (sp.latex(zp), sp.latex(zq)))
    res_f = _frobenius_regular(eq_expr, f, ivar, n, out)
    if res_f:
        for r, ws in res_f:
            ok, bad = _resid_check(eq_expr, f, ivar, ws, cd, n, divide=ivar**r)
            out.line("Frobenius 解 (ρ=%s): $%s$  回代: %s" % (
                sp.latex(r), sp.latex(ws),
                "PASS（残差仅为截断伪影）" if ok else "FAIL，完整幂次 %s 上有残差" % bad))
    out.set_result(sp.S.Zero)


def cmd_numeric_integrate(out, args, opts):
    if len(args) != 4:
        raise ValueError("用法: numeric-integrate EXPR VAR A B")
    f = parse(args[0])
    var = _sym(args[1])
    a, b = parse(args[2]), parse(args[3])
    out.line("被积函数: $%s$" % sp.latex(f))
    out.line("区间: [%s, %s]" % (a, b))
    bad = _scan_singular(f, var, a, b)
    if bad:
        out.note("⚠️ 区间内被积函数存在奇点/不可采样点（前几个: %s）："
                 "含可去奇点（0/0）时 mpmath 可能静默给出错误值。"
                 "请分段积分（绕开奇点）或先用代数化简消去奇点" % [sp.N(x, 6) for x in bad])
    val, err = _quad(f, var, a, b)
    if abs(err) > sp.Float("0.05") * max(abs(val), sp.Float("1e-8")):
        out.note("⚠️ 误差估计 %s 相对值 %s 过大（>5%%）：结果不可信（被积函数振荡/衰减慢/"
                 "含未处理奇点）。建议分段积分或改用围道/级数方法" % (sp.Float(str(err), 5), sp.N(err, 3)))
    out.set_result(val)
    out.line("数值积分: %s" % val)
    out.line("误差估计: %s" % sp.Float(str(err), 5))


# ---------------------------------------------------------------- 分发

# ---------------------------------------------------------------- 多重积分

def _parse_domain(spec):
    """'y:0:x' -> (y, 0, x)。"""
    parts = split_top(spec, ":")
    if len(parts) != 3:
        raise ValueError('积分域应写成 "变量:下限:上限"（如 "y:0:x"），收到 %r' % spec)
    return _sym(parts[0]), parse(parts[1]), parse(parts[2])


def _quad_multi(f, doms):
    """嵌套 mpmath 数值积分（doms 内层在前，从外向内递归）。"""
    fe = sp.lambdify([d[0] for d in doms], f, "mpmath")

    def rec(idx, env):
        if idx < 0:
            return fe(*[env[d[0]] for d in doms])
        v, lo, hi = doms[idx]
        outer = [d[0] for d in doms[idx + 1:]]
        env_outer = [env[u] for u in outer]
        a = sp.lambdify(outer, lo, "mpmath")(*env_outer)
        b = sp.lambdify(outer, hi, "mpmath")(*env_outer)
        if a == b:
            return mp.mpf(0)

        def inner(u, _env=env, _v=v, _idx=idx):
            e = dict(_env)
            e[_v] = u
            return rec(_idx - 1, e)

        return mp.quad(inner, [a, b])

    return rec(len(doms) - 1, {})


def cmd_multi_integrate(out, args, opts):
    """multi-integrate EXPR "v1:LO:HI" "v2:LO2:HI2" ... —— 多重（累次）积分，**内层在前**。
    --subs "x=r*cos(t),y=r*sin(t)" 自动代换并乘 |Jacobian|；--jac EXPR 手动给 Jacobian。"""
    if len(args) < 3:
        raise ValueError(
            '用法: multi-integrate EXPR "内层变量:LO:HI" "外层变量:LO2:HI2" ...\n'
            '  例: multi-integrate "x*y" "y:0:x" "x:0:1"        （先对 y 积，再对 x 积）\n'
            '  极坐标: multi-integrate "exp(-(x^2+y^2))" "r:0:oo" "t:0:2*pi" '
            '--subs "x=r*cos(t),y=r*sin(t)"')
    expr = parse(args[0])
    doms = [_parse_domain(a) for a in args[1:]]
    f = expr
    if opts.get("subs"):
        rules = []
        for part in split_top(opts["subs"]):
            kv = split_top_kv(part, "=")
            if kv is None:
                raise ValueError('--subs 每项应形如 x=r*cos(t)，收到 %r' % part)
            rules.append((parse(kv[0]), parse(kv[1])))
        old = [l for l, _ in rules]
        f = f.subs(rules)
        out.line("变量代换: %s" % "，".join("$%s=%s$" % (sp.latex(a), sp.latex(b)) for a, b in rules))
        news = []
        for _, r in rules:
            news.extend([s for s in r.free_symbols if s not in old])
        news = sorted(set(news), key=str)
        if len(news) == len(old) >= 1:
            J = sp.simplify(sp.Matrix([[sp.diff(r, u) for u in news] for _, r in rules]).det())
            Jabs = sp.simplify(sp.Abs(J))
            out.line("Jacobian: $\\left|\\frac{\\partial(x_1,\\dots)}{\\partial(u_1,\\dots)}\\right| = |%s| = %s$"
                     % (sp.latex(J), sp.latex(Jabs)))
            if Jabs.has(sp.Abs):
                out.note("⚠️ Jacobian 符号无法判定，已保留 |J| 原样；建议用 --sym 声明新变量为正"
                         "（如 --sym \"r:positive\"），或改用 --jac 手动给因子的正形式")
            f = f * Jabs
        else:
            out.note("旧/新变量个数不匹配（或新变量与旧变量同名），未自动计算 Jacobian；可用 --jac 手动给")
    if opts.get("jac"):
        jv = parse(opts["jac"])
        f = f * jv
        out.line("手动乘入 Jacobian 因子: $%s$" % sp.latex(jv))
    try:
        f = sp.simplify(f)
    except Exception:
        pass
    out.line("被积函数（代换后）: $%s$" % sp.latex(f))

    # 累次积分写法（内层先包）
    tex = sp.latex(f)
    for v, lo, hi in doms:
        tex = r"\int_{%s}^{%s} %s\,d%s" % (sp.latex(lo), sp.latex(hi), tex, sp.latex(v))
    out.line("累次积分: $%s$" % tex)

    res = f
    for i, (v, lo, hi) in enumerate(doms, 1):
        try:
            res = sp.trigsimp(sp.simplify(res))
        except Exception:
            pass
        res = sp.integrate(res, (v, lo, hi))
        out.line("第 %d 步（对 %s 积分，%s → %s）: $%s$"
                 % (i, sp.latex(v), sp.latex(lo), sp.latex(hi), sp.latex(res)))
        if isinstance(res, sp.Integral) or res.has(sp.Integral):
            out.note("⚠️ 第 %d 步 sympy 未求出闭式" % i)
            break
    if isinstance(res, sp.Integral) or res.has(sp.Integral):
        try:
            val = _quad_multi(f, doms)
            out.line("降级数值积分: %s" % mp.nstr(val, 15))
            out.set_result(sp.Float(str(val), 15))
        except Exception as e:
            out.note("数值降级失败: %s" % (str(e) or repr(e)))
            out.note("建议：换积分次序 / 换元 / 加 --sym 假设后重试，或拆成多次单变量 integrate")
            out.set_result(res)
        return
    res = sp.simplify(res)
    out.set_result(res)
    out.line("结果: $%s$" % sp.latex(res))
    if not res.free_symbols:
        try:
            out.line("数值: %s" % sp.N(res, 20))
        except Exception:
            pass


# ---------------------------------------------------------------- 判敛

def _safe_conv(obj):
    """sympy 的 is_convergent() 返回 S.true / S.false / None，统一成 Python 布尔。"""
    try:
        v = obj.is_convergent()
    except Exception:
        return None
    if v == True:      # noqa: E712  (S.true == True 成立)
        return True
    if v == False:     # noqa: E712
        return False
    return None


def _partial_trend(f, k, lo, Ns=(100, 200, 400, 800)):
    """算部分和快照，用于给收敛/发散一个数值迹象（不是证明）。"""
    snaps = {}
    try:
        acc = sp.Float(0)
        lo_i = int(sp.N(lo, 5))
        Nmax = max(Ns)
        for j in range(lo_i, lo_i + Nmax):
            acc = acc + sp.N(f.subs(k, j), 30)
            if (j - lo_i + 1) in Ns:
                snaps[j - lo_i + 1] = acc
    except Exception:
        return None
    return snaps


def _has_neg_one_pow(expr):
    for p in expr.atoms(sp.Pow):
        if p.base == sp.S.NegativeOne:
            return True
    return False


def _alt_part(f, k):
    """若 f 是交错项 (-1)^k·a_k（或 (-1)^{k+1}·a_k），返回 a_k；否则 None。
    ⚠️ 必须确认商里【没有】(-1) 的幂，否则正项级数会被误判成交错级数。"""
    for sgn in ((-1) ** k, (-1) ** (k + 1)):
        try:
            q = sp.simplify(f / sgn)
        except Exception:
            continue
        if _has_neg_one_pow(q):
            continue
        try:
            if sp.limit(q, k, sp.oo) == 0:
                return q
        except Exception:
            continue
    return None


def cmd_converge(out, args, opts):
    """converge EXPR VAR [LO] —— 判级数；converge EXPR VAR A B —— 判反常积分。"""
    if len(args) not in (2, 3, 4):
        raise ValueError('用法: converge EXPR VAR [LO]（判 Σ_{n=LO}^{∞}）'
                         ' 或 converge EXPR VAR A B（判 ∫_A^B，B 可为 oo）')
    f = parse(args[0])
    k = _sym(args[1])
    if len(args) <= 3:
        lo = parse(args[2]) if len(args) == 3 else sp.Integer(1)
        out.line("级数: $\\sum_{%s=%s}^{\\infty} %s$" % (sp.latex(k), sp.latex(lo), sp.latex(f)))
        c = _safe_conv(sp.Sum(f, (k, lo, sp.oo)))
        ca = _safe_conv(sp.Sum(sp.Abs(f), (k, lo, sp.oo)))
        out.line("sympy is_convergent: Σa_n=%s ，Σ|a_n|=%s" % (c, ca))
        if c is True:
            if ca is True:
                label, why = "绝对收敛", "Σa_n 与 Σ|a_n| 均收敛（sympy 判定）"
            elif ca is False:
                label, why = "条件收敛", "Σa_n 收敛但 Σ|a_n| 发散"
            else:
                label, why = "收敛", "sympy 判定 Σa_n 收敛；绝对收敛性未判定"
        elif c is False:
            label, why = "发散", "sympy 判定 Σa_n 发散（通项不趋于 0 或部分和无界）"
        else:
            label, why = None, None
        if label is None:
            try:
                sv = sp.summation(f, (k, lo, sp.oo))
                if not sv.has(sp.Sum) and sv.is_finite:
                    label, why = "收敛", "可直接求和得到有限值 $%s$" % sp.latex(sv)
            except Exception:
                pass
        if label is None:
            a = _alt_part(f, k)
            if a is not None:
                out.line("识别为交错级数，取 $a_n=%s$" % sp.latex(a))
                try:
                    dec = sp.limit(a - a.subs(k, k + 1), k, sp.oo)
                except Exception:
                    dec = None
                mono = dec is not None and getattr(dec, "is_nonnegative", None) is not False
                out.line("Leibniz 检查: $a_n\\to0$ 成立；单调性 $a_n-a_{n+1}\\to%s$%s"
                         % (sp.latex(dec) if dec is not None else "（未能求出）",
                            "（非负，递减）" if mono else "（未能确证递减，需人工看）"))
                label = "条件收敛（Leibniz 判别法）" if mono else "可能收敛（Leibniz 前提未完全确证）"
                why = "交错级数 + 通项趋于 0" + (" + 单调递减" if mono else "")
            else:
                try:
                    ratio = sp.Abs(f.subs(k, k + 1) / f)
                    Lr = sp.limit(ratio, k, sp.oo)
                except Exception:
                    Lr = None
                try:
                    Lrt = sp.limit(sp.Abs(f) ** (1 / k), k, sp.oo)
                except Exception:
                    Lrt = None
                if Lr is not None and getattr(Lr, "is_number", False):
                    out.line("比值判别: $\\lim|a_{n+1}/a_n|=%s$" % sp.latex(Lr))
                    if sp.N(Lr) < 1:
                        label, why = "绝对收敛", "比值判别 L<1"
                    elif sp.N(Lr) > 1:
                        label, why = "发散", "比值判别 L>1"
                if label is None and Lrt is not None and getattr(Lrt, "is_number", False):
                    out.line("根值判别: $\\lim|a_n|^{1/n}=%s$" % sp.latex(Lrt))
                    if sp.N(Lrt) < 1:
                        label, why = "绝对收敛", "根值判别 L<1"
                    elif sp.N(Lrt) > 1:
                        label, why = "发散", "根值判别 L>1"
                if label is None:
                    label, why = "无法判定", "sympy 与比式/根式判别均未给出结论"
        snaps = _partial_trend(f, k, lo)
        if snaps:
            out.line("部分和迹象: %s" % "，".join("S_%d=%s" % (n, sp.N(v, 10)) for n, v in sorted(snaps.items())))
            keys = sorted(snaps)
            if len(keys) >= 2:
                d = abs(snaps[keys[-1]] - snaps[keys[-2]])
                out.line("相邻快照差 |S_%d−S_%d| = %s" % (keys[-1], keys[-2], sp.N(d, 6)))
        out.set_result(label)
        out.line("判定: **%s**%s" % (label, "（依据：%s）" % why if why else ""))
        if label == "无法判定":
            out.note("建议：手写比较判别/积分判别，或换用 --sym 加假设后重试；"
                     "部分和迹象只作参考，不是证明")
        return
    # 反常积分
    a, b = parse(args[2]), parse(args[3])
    out.line("反常积分: $\\int_{%s}^{%s} %s\\,d%s$" % (sp.latex(a), sp.latex(b), sp.latex(f), sp.latex(k)))
    c = _safe_conv(sp.Integral(f, (k, a, b)))
    ca = _safe_conv(sp.Integral(sp.Abs(f), (k, a, b)))
    out.line("sympy is_convergent: ∫f=%s ，∫|f|=%s" % (c, ca))
    if c is True:
        label = "绝对收敛" if ca is True else ("条件收敛" if ca is False else "收敛")
        why = "sympy 判定 ∫f 收敛" + ("，且 ∫|f| 收敛" if ca is True else
                                     ("，但 ∫|f| 发散" if ca is False else ""))
    elif c is False:
        label, why = "发散", "sympy 判定积分发散"
    else:
        label, why = None, "sympy 未判定"
    if a != -sp.oo and b != sp.oo:
        bad = _scan_singular(f, k, a, b)
        if bad:
            out.note("区间内出现奇点/不可采样点: %s（端点或内部瑕点，按反常积分处理）"
                     % [sp.N(x, 6) for x in bad])
    val = err = None
    try:
        val, err = _quad(f, k, a, b)
        out.line("数值: %s（误差估计 %s）" % (sp.N(val, 12), sp.Float(str(err), 3)))
    except Exception as e:
        out.note("数值积分失败: %s" % (str(e) or repr(e)))
    if label is None:
        try:
            iv = sp.integrate(f, (k, a, b))
            if not iv.has(sp.Integral) and iv.is_finite is True:
                label, why = "收敛", "可直接算出有限值 $%s$" % sp.latex(iv)
        except Exception:
            pass
    if label is None:
        if b == sp.oo:
            for p in (1, 2, 3, 4):
                try:
                    L = sp.limit(f * k ** p, k, sp.oo)
                except Exception:
                    L = None
                if (L is not None and getattr(L, "is_number", False)
                        and L != 0 and getattr(L, "is_finite", False)):
                    out.line("量级判别: $x^{%s}f(x)\\to%s$（非零有限），故 f 与 $1/x^{%s}$ 同阶"
                             % (p, sp.latex(L), p))
                    label = "收敛" if p > 1 else "发散"
                    why = "比较判别（与 p=%s 的 p 积分同阶）" % p
                    break
        if label is None:
            try:
                vn, en = sp.N(val), sp.N(err)
                if vn.is_finite and abs(vn) < sp.Float("1e8"):
                    if abs(en) <= sp.Float("1e-4") * max(abs(vn), sp.Float(1)):
                        label, why = "收敛（数值迹象）", "数值积分给出有限值 %s（误差 %s）" % (vn, en)
                    else:
                        label, why = "无法判定", "数值误差过大，不可作为判据"
                else:
                    label, why = "发散（数值迹象）", "数值积分发散/无界"
            except Exception:
                label, why = "无法判定", "符号与数值通道均未给出结论"
    out.set_result(label)
    out.line("判定: **%s**%s" % (label, "（依据：%s）" % why if why else ""))
    if label.startswith("无法判定"):
        out.note("建议：手写比较判别/等价量分析；瑕点处可先做 x−a 的等价无穷小替换")


# ---------------------------------------------------------------- 傅里叶级数

def cmd_fourier(out, args, opts):
    """fourier EXPR VAR L [N] —— 在 [-L, L] 上展开，N 为截断项数（默认 5）。"""
    if len(args) not in (3, 4):
        raise ValueError('用法: fourier EXPR VAR L [N]（区间 [-L, L]，N 默认 5）')
    f = parse(args[0])
    x = _sym(args[1])
    L = parse(args[2])
    N = int(args[3]) if len(args) == 4 else 5
    out.line("函数: $f(x)=%s$ ，区间 $[%s, %s]$" % (sp.latex(f), sp.latex(-L), sp.latex(L)))
    n = sp.Symbol("n", integer=True, positive=True)
    a0 = an = bn = None
    try:
        a0 = sp.simplify(sp.integrate(f, (x, -L, L)) / L)
        an = sp.simplify(sp.integrate(f * sp.cos(n * sp.pi * x / L), (x, -L, L)) / L)
        bn = sp.simplify(sp.integrate(f * sp.sin(n * sp.pi * x / L), (x, -L, L)) / L)
        if any(v is not None and v.has(sp.Integral) for v in (a0, an, bn)):
            out.note("系数通式含未求值积分，改用数值/截断写法")
            a0 = an = bn = None
    except Exception as e:
        out.note("系数通式求解失败: %s" % (str(e) or repr(e)))
    if a0 is not None:
        out.line("$a_0=\\frac{1}{L}\\int_{-L}^{L}f\\,dx=%s$" % sp.latex(a0))
        out.line("$a_n=\\frac{1}{L}\\int_{-L}^{L}f\\cos\\frac{n\\pi x}{L}dx=%s$" % sp.latex(an))
        out.line("$b_n=\\frac{1}{L}\\int_{-L}^{L}f\\sin\\frac{n\\pi x}{L}dx=%s$" % sp.latex(bn))
    try:
        S = sp.fourier_series(f, (x, -L, L)).truncate(N)
        out.line("前 %d 项（sympy fourier_series）: $%s$" % (N, sp.latex(sp.simplify(S))))
    except Exception as e:
        if an is not None and bn is not None:
            S = a0 / 2 + sum((an * sp.cos(k * sp.pi * x / L) + bn * sp.sin(k * sp.pi * x / L)).subs(n, k)
                             for k in range(1, N + 1))
            S = sp.simplify(S)
            out.line("前 %d 项（由 a_n/b_n 手工拼装）: $%s$" % (N, sp.latex(S)))
        else:
            S = None
            out.note("傅里叶和构造失败: %s" % (str(e) or repr(e)))
    try:
        fL = sp.limit(f, x, L, "-")
        fmL = sp.limit(f, x, -L, "+")
        endv = sp.simplify((fL + fmL) / 2)
        out.line("端点 $x=\\pm %s$ 处，和函数收敛于 $\\frac{f(-L^+)+f(L^-)}{2}=%s$"
                 "（连续时即 $f(\\pm L)$；间断时取左右极限平均）"
                 % (sp.latex(L), sp.latex(endv)))
        if sp.simplify(fL - fmL) != 0:
            out.note("端点左右极限不等 → 该点是和函数的间断点，级数在端点收敛于平均值（Dirichlet 定理）")
    except Exception:
        pass
    out.line("提示：连续点处 $S(x)=f(x)$；间断点 $x_0$ 处 $S(x_0)=\\frac{f(x_0^-)+f(x_0^+)}{2}$")
    out.set_result(S)


# ---------------------------------------------------------------- 正交对角化

def cmd_orthdiag(out, args, opts):
    """orthdiag MAT —— 实对称矩阵正交对角化（QᵀAQ=Λ），附双自检与二次型标准化。"""
    if len(args) != 1:
        raise ValueError('用法: orthdiag "[[2,1],[1,2]]"（实对称矩阵）')
    m = _sympify_matrix(args[0])
    if m.rows != m.cols:
        raise ValueError("需要方阵，收到 %dx%d" % (m.rows, m.cols))
    out.line("A = $%s$" % sp.latex(m))
    if m.is_symmetric() is False:
        raise ValueError("矩阵不对称 —— 正交对角化只对实对称矩阵成立（考研二次型必为对称阵）")
    if m.is_symmetric() is None:
        out.note("无法确证对称性（含未定符号），仍按对称阵处理，请自行确认")
    try:
        P, D = m.diagonalize(normalize=True)
    except Exception as e:
        raise ValueError("对角化失败（矩阵可能不可对角化）: %s" % (str(e) or repr(e)))
    out.line("特征值对角阵 $\\Lambda=%s$" % sp.latex(D))
    out.line("正交矩阵 $Q=%s$（列为标准正交特征向量）" % sp.latex(P))
    out.line("自检 $Q^TQ=%s$" % sp.latex(sp.simplify(P.T * P)))
    out.line("自检 $Q^TAQ=%s$" % sp.latex(sp.simplify(P.T * m * P)))
    ev = [sp.simplify(D[i, i]) for i in range(m.rows)]
    ys = sp.symbols("y1:%d" % (m.rows + 1))
    qform = sum(ev[i] * ys[i] ** 2 for i in range(m.rows))
    out.line("令 $x=Qy$，则 $x^TAx=%s$" % sp.latex(qform))
    try:
        nums = [sp.N(e) for e in ev]
        if all(v > 0 for v in nums):
            kind = "正定"
        elif all(v < 0 for v in nums):
            kind = "负定"
        elif all(v >= 0 for v in nums) or all(v <= 0 for v in nums):
            kind = "半正定/半负定（有零特征值）"
        else:
            kind = "不定"
        out.line("定性: **%s**（特征值 %s）" % (kind, [sp.N(e, 8) for e in ev]))
    except Exception:
        pass
    out.set_result(D)


# ---------------------------------------------------------------- 概率统计

def _stats():
    import sympy.stats as _st
    return _st


_DIST_ARGS = {
    "normal": ("Normal", 2, "N(μ, σ²)，σ 为标准差"),
    "uniform": ("Uniform", 2, "U(a, b)"),
    "exp": ("Exponential", 1, "Exp(λ)，λ 为速率"),
    "poisson": ("Poisson", 1, "P(λ)"),
    "binomial": ("Binomial", 2, "B(n, p)"),
    "geom": ("Geometric", 1, "G(p)"),
    "gamma": ("Gamma", 2, "Gamma(k, θ)"),
    "chi2": ("ChiSquared", 1, "χ²(k)"),
    "t": ("StudentT", 1, "t(ν)"),
    "beta": ("Beta", 2, "Beta(a, b)"),
    "f": ("F", 2, "F(d1, d2)"),
    "lognormal": ("LogNormal", 2, "lnN(μ, σ)"),
}
_DISCRETE = {"poisson", "binomial", "geom"}


def _make_rv(args):
    """args = [分布名, 参数...] -> (name, RandomSymbol X)。"""
    if not args:
        raise ValueError("缺少分布名，可用: %s" % " / ".join(sorted(_DIST_ARGS)))
    key = args[0].strip().lower()
    if key not in _DIST_ARGS:
        raise ValueError("未知分布 %r，可用: %s" % (key, " / ".join(sorted(_DIST_ARGS))))
    fname, npar, desc = _DIST_ARGS[key]
    if len(args) != 1 + npar:
        raise ValueError("%s 需要 %d 个参数（%s），收到 %d 个"
                         % (key, npar, desc, len(args) - 1))
    params = [parse(a) for a in args[1:]]
    st = _stats()
    X = getattr(st, fname)("X", *params)
    return key, X, desc


def _mgf_calc(X, key, t):
    """M(t)=E(e^{tX})；离散型 sympy 常给未求值的 Sum，这里用 pmf 显式求和兜底。"""
    st = _stats()
    M = sp.simplify(st.E(sp.exp(t * X)))
    if isinstance(M, sp.Piecewise):
        M = M.args[0][0]
    if M.has(sp.Sum) or M.has(sp.Integral):
        # 用 z=exp(t) 把 e^{tk} 换成 z^k（sympy 才能识别几何级数），求和后回代
        z = sp.Symbol("z")
        k = sp.Symbol("k", integer=True, nonnegative=True)
        for lo in (0, 1):
            try:
                pmf = sp.expand(X.pspace.distribution.pdf(k), force=True)
                S = sp.summation(pmf * z ** k, (k, lo, sp.oo))
                if S.has(sp.Piecewise):
                    # 分段式通常形如 Piecewise(主支, (Sum, True))/2，取主支
                    S = S.replace(lambda e: isinstance(e, sp.Piecewise),
                                  lambda e: e.args[0][0])
                if S.has(sp.Sum) or S.has(sp.Integral):
                    continue
                M2 = sp.simplify(S.subs(z, sp.exp(t)))
                if sp.simplify(M2.subs(t, 0)) == 1:
                    return M2
            except Exception:
                continue
    return M


def cmd_dist(out, args, opts):
    if len(args) < 2:
        raise ValueError("用法: dist NAME 参数...（NAME: %s）" % " / ".join(sorted(_DIST_ARGS)))
    key, X, desc = _make_rv(args)
    st = _stats()
    out.line("分布: **%s** —— %s" % (key, desc))
    try:
        out.line("$E(X)=%s$" % sp.latex(sp.simplify(st.E(X))))
    except Exception as e:
        out.note("期望失败: %s" % (str(e) or repr(e)))
    try:
        out.line("$Var(X)=%s$" % sp.latex(sp.simplify(st.variance(X))))
    except Exception as e:
        out.note("方差失败: %s" % (str(e) or repr(e)))
    x = sp.Symbol("x") if key not in _DISCRETE else sp.Symbol("k", integer=True, nonnegative=True)
    res = None
    try:
        res = st.density(X)(x)
        if res.has(sp.DiracDelta):
            try:
                res = X.pspace.distribution.pdf(x)
            except Exception:
                pass
        out.line("%s $=%s$" % ("分布律 $P(X=k)$" if key in _DISCRETE else "密度 $f(x)$", sp.latex(res)))
    except Exception as e:
        out.note("密度/分布律失败: %s" % (str(e) or repr(e)))
    t = sp.Symbol("t")
    try:
        M = _mgf_calc(X, key, t)
        if isinstance(M, sp.Piecewise):
            M = M.args[0][0]
            out.note("mgf 原为分段式（含收敛域条件），已取收敛域内的主支")
        if not M.has(sp.Integral) and not M.has(sp.Sum):
            out.line("矩母函数 $M(t)=E(e^{tX})=%s$" % sp.latex(M))
    except Exception:
        out.note("矩母函数未能求出（部分分布不存在 mgf，如 t 分布）")
    out.set_result(res)


def cmd_expect(out, args, opts):
    if len(args) < 2:
        raise ValueError("用法: expect EXPR NAME 参数...（随机变量统一写 X 或 x）")
    key, X, _ = _make_rv(args[1:])
    e0 = parse(args[0])
    expr = e0.subs(sp.Symbol("X"), X).subs(sp.Symbol("x"), X)
    st = _stats()
    out.line("X ~ %s，求 $E[%s]$" % (key, sp.latex(e0)))
    val = sp.simplify(st.E(expr))
    out.set_result(val)
    out.line("结果: $%s$" % sp.latex(val))
    try:
        if not val.free_symbols:
            out.line("数值: %s" % sp.N(val, 15))
    except Exception:
        pass


def cmd_var(out, args, opts):
    """var NAME 参数... [EXPR] —— 不给 EXPR 就给 Var(X)；给了就按 E(g²)−E(g)² 算。"""
    if len(args) < 2:
        raise ValueError("用法: var NAME 参数... [EXPR]（如 var normal 0 1 ；var normal 0 1 \"x^2\"）")
    key = args[0].strip().lower()
    if key not in _DIST_ARGS:
        raise ValueError("未知分布 %r" % key)
    npar = _DIST_ARGS[key][1]
    has_expr = len(args) == 2 + npar
    if len(args) not in (1 + npar, 2 + npar):
        raise ValueError("%s 需要 %d 个参数，可再跟一个表达式" % (key, npar))
    key, X, _ = _make_rv(args[: 1 + npar])
    st = _stats()
    if not has_expr:
        out.line("X ~ %s，$Var(X)=%s$" % (key, sp.latex(sp.simplify(st.variance(X)))))
        out.set_result(sp.simplify(st.variance(X)))
        return
    g0 = parse(args[-1])
    g = g0.subs(sp.Symbol("X"), X).subs(sp.Symbol("x"), X)
    E1 = sp.simplify(st.E(g))
    E2 = sp.simplify(st.E(g ** 2))
    V = sp.simplify(E2 - E1 ** 2)
    out.line("$E[%s]=%s$ ，$E[%s^2]=%s$" % (sp.latex(g0), sp.latex(E1), sp.latex(g0), sp.latex(E2)))
    out.set_result(V)
    out.line("$Var(%s)=E(g^2)-[E(g)]^2=%s$" % (sp.latex(g0), sp.latex(V)))


def cmd_mgf(out, args, opts):
    if len(args) < 2:
        raise ValueError("用法: mgf NAME 参数...")
    key, X, _ = _make_rv(args)
    st = _stats()
    t = sp.Symbol("t")
    M = _mgf_calc(X, key, t)
    if isinstance(M, sp.Piecewise):
        M = M.args[0][0]
        out.note("mgf 原为分段式（含收敛域条件），已取收敛域内的主支")
    out.set_result(M)
    out.line("$M(t)=E(e^{tX})=%s$" % sp.latex(M))
    try:
        out.line("$M(0)=%s$（应等于 1）" % sp.latex(sp.simplify(M.subs(t, 0))))
        out.line("$M'(0)=%s$（应等于 E(X)）" % sp.latex(sp.simplify(sp.diff(M, t).subs(t, 0))))
    except Exception:
        pass


def cmd_mle(out, args, opts):
    """mle LOGLIK VAR —— 对数似然对参数求导置零，并检验二阶导（极大）条件。"""
    if len(args) != 2:
        raise ValueError('用法: mle LOGLIK VAR（LOGLIK 为对数似然，含数据符号；'
                         '如 mle "n*log(theta)-theta*S" theta）')
    L = parse(args[0])
    th = sp.Symbol(args[1])
    out.line("对数似然: $\\ell(%s)=%s$" % (sp.latex(th), sp.latex(L)))
    d1 = sp.simplify(sp.diff(L, th))
    out.line("得分函数: $\\ell\'(%s)=%s$" % (sp.latex(th), sp.latex(d1)))
    sols = sp.solve(sp.Eq(d1, 0), th)
    if not sols:
        out.line("无驻点解 —— 极值可能在参数空间边界，需单独讨论")
        out.set_result(sp.S.EmptySet)
        return
    d2 = sp.simplify(sp.diff(L, th, 2))
    out.line("二阶导: $\\ell\'\'(%s)=%s$" % (sp.latex(th), sp.latex(d2)))
    best = None
    for s in sols:
        d2v = sp.simplify(d2.subs(th, s))
        if not (d2v.is_negative or d2v.is_positive):
            # 符号未定：按「数据/参数均为正」重判（考研似然里的 n、Σxᵢ 都是正量）
            try:
                pmap = {q: sp.Symbol(q.name, positive=True) for q in d2v.free_symbols}
                d2v2 = sp.simplify(d2v.subs(pmap))
                if d2v2.is_negative or d2v2.is_positive:
                    out.note("二阶导在原符号下符号未定，按「数据/参数为正」判定：$\\ell''=%s$"
                             % sp.latex(d2v2))
                    d2v = d2v2
            except Exception:
                pass
        try:
            if d2v.is_negative:
                verdict = "极大值 ✅"
                best = s
            elif d2v.is_positive:
                verdict = "极小值（舍去）"
            else:
                verdict = "二阶导符号未定，需人工判（可用 --sym 给数据符号加正假设）"
        except Exception:
            verdict = "二阶导符号未定"
        out.line("驻点 $\\hat{%s}=%s$ ，$\\ell''=%s$ → %s"
                 % (sp.latex(th), sp.latex(s), sp.latex(d2v), verdict))
    out.set_result(best if best is not None else sols[0])
    if best is not None:
        out.line("最大似然估计: $\\hat{%s}=%s$" % (sp.latex(th), sp.latex(best)))
        out.line("结果: $%s$" % sp.latex(best))


def _t_quantile(df, p):
    """t(df) 的上分位数，用 mpmath 数值反解。
    ⚠️ 不要用 sympy 的 st.quantile(StudentT)：符号反解会挂死（实测 SIGTERM）。"""
    d = mp.mpf(str(sp.N(df, 20)))
    pp = mp.mpf(str(sp.N(p, 20)))

    def F(q):
        x = d / (d + q * q)
        tail = 0.5 * mp.betainc(d / 2, mp.mpf("0.5"), 0, x, regularized=True)
        return (1 - tail if q > 0 else tail) - pp

    return mp.findroot(F, mp.mpf(1))


def cmd_ci(out, args, opts):
    """ci normal MU SIGMA N LEVEL（σ 已知，z 区间）；ci t XBAR S N LEVEL（σ 未知，t 区间）。"""
    if len(args) != 5:
        raise ValueError('用法: ci normal MU SIGMA N LEVEL（σ 已知） 或 ci t XBAR S N LEVEL（σ 未知）\n'
                         '  例: ci normal 0 1 16 0.95 ；ci t 3.2 0.5 9 0.95')
    kind = args[0].strip().lower()
    a1, a2, n, lvl = parse(args[1]), parse(args[2]), parse(args[3]), parse(args[4])
    q = sp.simplify((1 + lvl) / 2)
    st = _stats()
    if kind in ("normal", "z"):
        Z = st.Normal("Z", 0, 1)
        zq = st.quantile(Z)(q)
        if a1 != 0:
            out.note("z 区间用于 μ 未知但 σ 已知的场合；你给了 MU=%s，结果是在其附近的形式区间" % a1)
        half = sp.simplify(zq * a2 / sp.sqrt(n))
        lo, hi = sp.simplify(a1 - half), sp.simplify(a1 + half)
        mid = a1
        out.line("枢轴量 $Z=\\frac{\\bar X-\\mu}{\\sigma/\\sqrt n}\\sim N(0,1)$，"
                 "$z_{\\alpha/2}=%s$" % sp.latex(zq))
        out.line("置信区间: $[%s,\\ %s]$" % (sp.latex(lo), sp.latex(hi)))
        out.line("半宽: $%s\\cdot\\frac{%s}{\\sqrt{%s}}=%s$"
                 % (sp.latex(zq), sp.latex(a2), sp.latex(n), sp.latex(half)))
    elif kind in ("t", "student"):
        df = n - 1
        tqv = sp.Float(str(_t_quantile(df, q)), 15)
        half = sp.simplify(tqv * a2 / sp.sqrt(n))
        lo, hi = sp.simplify(a1 - half), sp.simplify(a1 + half)
        out.line("枢轴量 $T=\\frac{\\bar X-\\mu}{S/\\sqrt n}\\sim t(%s)$，$t_{\\alpha/2}(%s)\\approx %s$"
                 % (df, df, sp.N(tqv, 10)))
        out.line("置信区间: $[%s,\\ %s]$" % (sp.latex(lo), sp.latex(hi)))
        out.line("半宽: $%s\\cdot\\frac{%s}{\\sqrt{%s}}=%s$"
                 % (sp.N(tqv, 10), sp.latex(a2), sp.latex(n), sp.latex(sp.N(half, 10))))
    else:
        raise ValueError("只支持 normal（σ 已知）/ t（σ 未知），收到 %r" % kind)
    out.set_result(sp.Tuple(lo, hi))
    try:
        out.line("数值: [%s, %s]" % (sp.N(lo, 8), sp.N(hi, 8)))
    except Exception:
        pass


def _mt_main_inner(argv):
    global E_MAP_ENABLED
    flags = {"--json": "json", "--float": "float", "--numeric": "numeric", "--deep": "deep",
             "--preserve-e": "preserve_e", "--coeffs": "coeffs"}
    opts = {v: False for v in flags.values()}
    opts.update({"subs": None, "jac": None, "at": None})
    positional = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--subs", "--jac", "--at"):
            if i + 1 >= len(argv):
                print("ERROR: %s 需要参数（如 --subs \"x=r*cos(t),y=r*sin(t)\"）" % tok)
                return 1
            opts[tok[2:]] = argv[i + 1]
            i += 2
            continue
        if tok == "--sym":
            if i + 1 >= len(argv):
                print("ERROR: --sym 需要参数，如 --sym \"n:integer\"")
                return 1
            register_syms(argv[i + 1])
            i += 2
            continue
        if tok in flags:
            opts[flags[tok]] = True
            if tok == "--preserve-e":
                E_MAP_ENABLED = False
        else:
            positional.append(tok)
        i += 1
    if not positional or positional[0] in ("-h", "--help", "help", "commands"):
        print(__doc__)
        return 0
    cmd = positional[0]
    args = positional[1:]
    rational = not opts["float"]
    global parse
    _parse_orig = parse

    def parse(s, rational=rational):  # noqa: F811
        return _parse_orig(s, rational=rational)

    out = _MT_Out(opts["json"])
    try:
        if cmd == "integrate":
            cmd_integrate(out, args, opts)
        elif cmd == "subs-integrate":
            cmd_subs_integrate(out, args, opts)
        elif cmd == "multi-integrate":
            cmd_multi_integrate(out, args, opts)
        elif cmd == "converge":
            cmd_converge(out, args, opts)
        elif cmd == "fourier":
            cmd_fourier(out, args, opts)
        elif cmd == "orthdiag":
            cmd_orthdiag(out, args, opts)
        elif cmd == "dist":
            cmd_dist(out, args, opts)
        elif cmd == "expect":
            cmd_expect(out, args, opts)
        elif cmd == "var":
            cmd_var(out, args, opts)
        elif cmd == "mgf":
            cmd_mgf(out, args, opts)
        elif cmd == "mle":
            cmd_mle(out, args, opts)
        elif cmd == "ci":
            cmd_ci(out, args, opts)
        elif cmd == "diff":
            cmd_diff(out, args, opts)
        elif cmd == "solve":
            cmd_solve(out, args, opts)
        elif cmd == "simplify":
            cmd_simplify(out, args, opts)
        elif cmd in ("expand", "factor", "together", "apart", "cancel", "hyperexpand",
                     "trigsimp", "logcombine", "radsimp", "powsimp"):
            cmd_basic(out, args, opts, getattr(sp, cmd), cmd)
        elif cmd == "limit":
            cmd_limit(out, args, opts)
        elif cmd == "series":
            cmd_series(out, args, opts)
        elif cmd == "summation":
            cmd_sumprod(out, args, opts, sp.summation, "summation", "sum")
        elif cmd == "product":
            cmd_sumprod(out, args, opts, sp.product, "product", "prod")
        elif cmd == "matrix":
            cmd_matrix(out, args, opts)
        elif cmd == "matrix-eqsolve":
            cmd_matrix_eqsolve(out, args, opts)
        elif cmd == "evalf":
            cmd_evalf(out, args, opts)
        elif cmd == "subs":
            cmd_subs(out, args, opts)
        elif cmd == "coeff":
            cmd_coeff(out, args, opts)
        elif cmd == "latex":
            cmd_latex(out, args, opts)
        elif cmd == "nsimplify":
            cmd_nsimplify(out, args, opts)
        elif cmd == "gammasimp":
            cmd_gamma_simp(out, args, opts, sp.gammasimp, "gammasimp")
        elif cmd == "combsimp":
            cmd_gamma_simp(out, args, opts, sp.combsimp, "combsimp")
        elif cmd == "check":
            cmd_check(out, args, opts)
        elif cmd == "numeric-integrate":
            cmd_numeric_integrate(out, args, opts)
        elif cmd == "residue":
            cmd_residue(out, args, opts)
        elif cmd == "residues":
            cmd_residues(out, args, opts)
        elif cmd == "dsolve":
            cmd_dsolve(out, args, opts)
        elif cmd == "ode-series":
            cmd_ode_series(out, args, opts)
        else:
            out.finish(ok=False, error="未知命令: %s（用 help 查看用法）" % cmd)
            return 1
    except RuntimeError as e:
        return out.finish(ok=False, error=e)
    except Exception as e:
        msg = ("%s: %s" % (type(e).__name__, e)).strip()
        if msg in ("", ":", "Exception:", type(e).__name__ + ":"):
            msg = repr(e)
        # 内部哑变量名（Dummy 生成 _w/_x）对用户不可读，替换为友好名
        msg = msg.replace("_w", "w").replace("_x", "x")
        return out.finish(ok=False, error=msg)
    return out.finish(ok=True)

def _mt_main(argv):
    """math_tool 入口的隔离包装。

    math_tool 的 main 里有 `global parse` + `def parse(...)`，且**从不恢复** ——
    独立进程跑一条命令无所谓，但合并进 mathkit 后，一次 --float 委托会把全局
    parse 永久换成 rational=False，后续所有命令的 1/4 都悄悄变成 0.25。
    这里保存/恢复 parse 与 E_MAP_ENABLED，把副作用限制在单次调用内。
    """
    global parse, E_MAP_ENABLED
    _saved_parse, _saved_emap = parse, E_MAP_ENABLED
    try:
        return _mt_main_inner(argv)
    finally:
        parse, E_MAP_ENABLED = _saved_parse, _saved_emap


# ---------------------------------------------------------------- 输出编码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---------------------------------------------------------------- 可选引擎
try:
    import symengine as _se
    HAS_SE = True
except Exception:
    _se = None
    HAS_SE = False

ENGINE = "auto"

# 模块导入阶段耗时（ms）。不含 Python 解释器自身启动（约 0.5 s，脚本内部测不到），
# 但覆盖了 import sympy / math_tool / symengine 这几笔真正的大头。
IMPORT_MS = (time.perf_counter() - _T_PROC_START) * 1000

# --engine 只接受这三个值；用白名单判断「下一个 token 是不是引擎名」，
# 避免 `--engine expand "x^2"` 把命令名当成引擎值吞掉。
ENGINE_NAMES = ("auto", "sympy", "symengine")

TRIG_METHODS = ("matching", "groebner", "combined")

# 取值受限的选项：只有下一个 token 命中白名单才算作它的值，否则视为「缺值」。
_VALUE_FLAGS = {"engine": ENGINE_NAMES, "method": TRIG_METHODS}

# 纯布尔选项：永不消费后继 token。没有这张表时 `--trace EXPR` 会把表达式吃成
# 选项值（旧文档因此要求「布尔开关一律放最后」），这条限制现在取消了。
_BOOL_FLAGS = {
    "json", "trace", "trig", "no-check", "coeffs", "deep", "force",
    "preserve-e", "normalize", "quiet",
}


# ---------------------------------------------------------------- 解析工具
# math_tool 内部叫 parse / split_top / _sympify_matrix，mathkit 历史代码用带下划线的名字。
# 这里做一次别名，避免改动 100+ 处调用点。_sym 两边同名同义，直接用 math_tool 的。
_parse = parse
_matrix = _sympify_matrix
_split = split_top


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
        total = IMPORT_MS + ms
        if self.as_json:
            payload = {
                "ok": ok, "cmd": self.cmd, "result": text, "latex": latex,
                "engine": self.engine,
                # 三段耗时：compute 是本命令的计算，import 是解释器加载 SymPy 的固定成本，
                # total 才是用户体感的端到端（仍不含解释器自身启动，约 0.5 s）
                "compute_ms": round(ms, 1),
                "import_ms": round(IMPORT_MS, 1),
                "total_ms": round(total, 1),
                "ms": round(ms, 1),          # 兼容旧字段，等价于 compute_ms
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
            print(f"引擎: {self.engine} | 计算 {ms:.0f} ms | 导入 {IMPORT_MS:.0f} ms"
                  f" | 总计 {total / 1000:.2f} s")
        else:
            print(f"错误: {error}")
        return 0 if ok else 1


def _emit(out, value, engine=None):
    if engine:
        out.engine = engine
    return out.finish(value)


def _print_timing(out):
    """多行输出（contour / tensor / simp）的收尾计时行，与 Out.finish 同格式。"""
    c = (time.perf_counter() - out.t0) * 1000
    print("引擎: %s | 计算 %.0f ms | 导入 %.0f ms | 总计 %.2f s"
          % (out.engine, c, IMPORT_MS, (IMPORT_MS + c) / 1000))


# ---------------------------------------------------------------- 通用装饰
def _delegate(out, argv):
    """把命令交给内置的 math_tool 实现执行，保留其原有输出格式。"""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = _mt_main(argv)
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


# ---------------------------------------------------------------- 靶向化简（新增）
# simplify 是「挨个试 + 按 count_ops 挑最短」的启发式：没有 assumptions 参数，
# 也经常原地打转（sin^2/(1-cos) 它化不动）。下面补的是它够不着的那几块。
_ASSUME_PROPS = (
    "positive", "negative", "nonzero", "nonnegative", "nonpositive",
    "real", "complex", "imaginary", "nonreal", "rational", "irrational",
    "integer", "prime", "even", "odd", "finite", "infinite", "hermitian",
)


def _predicates(spec):
    """'x:positive,y:real' -> ([Q.positive(x), Q.real(y)], 无法识别的项)。"""
    preds, bad = [], []
    for item in _split(spec or ""):
        if ":" not in item:
            bad.append(item)
            continue
        name, prop = item.split(":", 1)
        prop = prop.strip().lower()
        if prop not in _ASSUME_PROPS:
            bad.append(item)
            continue
        preds.append(getattr(sp.Q, prop)(_sym(name.strip())))
    return preds, bad


def c_refine(out, pos, opt):
    """refine EXPR ["x:positive,..."] —— 在假设下化简（simplify 没有这个能力）。

    sqrt(x**2)：simplify 原样返回；refine(x:real) -> Abs(x)；refine(x:positive) -> x。
    """
    expr = _parse(pos[0])
    spec = pos[1] if len(pos) > 1 else opt.get("assume", "")
    if not isinstance(spec, str):
        spec = ""
    preds, bad = _predicates(spec)
    if bad:
        return out.finish(ok=False, error="无法识别的假设: %s（写法 x:positive，可选属性见 SKILL）"
                          % ", ".join(bad))
    if not preds:
        return out.finish(ok=False, error='refine 需要假设，例如 refine "sqrt(x^2)" "x:real"')
    # refine() 只收一个 assumptions 对象，多个谓词必须用 And 合成
    r = sp.refine(expr, sp.And(*preds))
    return out.finish(r, note="假设: " + ", ".join(str(p) for p in preds))


def c_fu(out, pos, opt):
    """fu EXPR [--rule TR8] —— Hardy 规则集；--rule 只跑单条规则。"""
    import importlib
    # 注意：sympy.simplify.fu 在包命名空间里被同名函数遮蔽，必须走 import_module。
    _fumod = importlib.import_module("sympy.simplify.fu")
    expr = _parse(pos[0])
    rule = opt.get("rule")
    if isinstance(rule, str) and rule.strip():
        rule = rule.strip()
        if not rule.startswith("TR"):
            return out.finish(ok=False, error="--rule 只接受 TR* 规则（如 TR2i / TR8 / TRmorrie / TR22）")
        fn = getattr(_fumod, rule, None)
        if not callable(fn):
            return out.finish(ok=False, error="未知 fu 规则: %s" % rule)
        return _emit(out, fn(expr))
    return _emit(out, _fumod.fu(expr))


def c_factor_terms(out, pos, opt):
    """factor_terms EXPR —— 只提公因子，不展开（factor 会先把式子摊平）。"""
    return _emit(out, sp.factor_terms(_parse(pos[0])))


def c_trigsimp(out, pos, opt):
    """trigsimp EXPR [--method matching|groebner|combined] [--deep]"""
    expr = _parse(pos[0])
    if len(pos) > 1:
        return out.finish(ok=False, error="多余的参数: %s（--method 只接受 %s）"
                          % (" ".join(pos[1:]), " / ".join(TRIG_METHODS)))
    method = opt.get("method", "matching")
    if method is True:
        return out.finish(ok=False, error="--method 需要取值: %s" % " / ".join(TRIG_METHODS))
    method = str(method).lower()
    if method not in TRIG_METHODS:
        return out.finish(ok=False, error="未知 trigsimp 方法: %s（可选: %s）"
                          % (method, " / ".join(TRIG_METHODS)))
    deep = bool(opt.get("deep"))
    r = sp.trigsimp(expr, method=method, deep=deep)
    note = None
    if method != "matching":
        note = ("method=%s：结果形状与 matching 不同（不是精度差异），更慢，且依赖进程哈希种子——"
                "同一输入跨进程可能给出 cos 基或 sin 基两种等价形式；要可复现就用 matching" % method)
    if deep:
        note = (note + "；" if note else "") + "deep=True：对子表达式递归触发"
    return out.finish(r, note=note)


# ---- 自动流水线：逐步尝试 + 数值抽检 + 变差就回退
_SIMP_STEPS = ("cancel", "trigsimp", "fu", "factor_terms")


def _fu_default(e):
    import importlib
    return importlib.import_module("sympy.simplify.fu").fu(e)


_SIMP_OPS = {
    "cancel": sp.cancel,
    "together": sp.together,
    "ratsimp": sp.ratsimp,
    "radsimp": sp.radsimp,
    "factor_terms": sp.factor_terms,
    "factor": sp.factor,
    "sqrtdenest": sp.sqrtdenest,
    "expand_trig": sp.expand_trig,
    "trigsimp": sp.trigsimp,
    "trigsimp-groebner": lambda e: sp.trigsimp(e, method="groebner"),
    "fu": _fu_default,
    "simplify": sp.simplify,
    "expand": sp.expand,
}


def _same_on_samples(a, b, n=3, seed=0, tol=1e-9):
    """随机实点上比对两个表达式。返回 (是否一致, 有效点数, 最大相对偏差)。

    一致 = True / 不一致 = False / 无法数值化 = None（绝不冒充 PASS）。
    抽检盲区：随机点几乎不会落在孤立奇点上，所以 cancel 填洞抓不到。
    """
    import math
    import random

    vs = sorted(a.free_symbols | b.free_symbols, key=str)
    if not vs:
        try:
            na, nb = complex(sp.N(a, 25)), complex(sp.N(b, 25))
        except Exception:
            return None, 0, None
        if not (math.isfinite(na.real) and math.isfinite(nb.real)):
            return None, 0, None
        rel = abs(na - nb) / max(abs(na), abs(nb), 1e-12)
        return rel <= tol, 1, rel
    try:
        fa = sp.lambdify(vs, a, "math")
        fb = sp.lambdify(vs, b, "math")
    except Exception:
        return None, 0, None
    rng = random.Random(seed)
    used, worst = 0, 0.0
    for _ in range(n * 8):
        if used >= n:
            break
        pt = [rng.choice((-1, 1)) * rng.uniform(0.2, 3.0) for _ in vs]
        try:
            va, vb = complex(fa(*pt)), complex(fb(*pt))
        except Exception:
            continue
        if not (math.isfinite(va.real) and math.isfinite(va.imag)
                and math.isfinite(vb.real) and math.isfinite(vb.imag)):
            continue
        used += 1
        worst = max(worst, abs(va - vb) / max(abs(va), abs(vb), 1e-12))
    if used == 0:
        return None, 0, None
    return worst <= tol, used, worst


def _brief(e, n=88):
    s = str(e).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def _holes(orig, res):
    """单变量下找出被 cancel 填掉的可去奇点：原式无定义、化简后有值。

    cancel((x^2-1)/(x-1)) = x+1 在 x=1 处把洞填了 —— 这是数学上不等价的改写，
    随机数值抽检抓不到（几乎不会命中奇点），所以单独枚举分母的零点来查。
    """
    import math

    fs = sorted(orig.free_symbols, key=str)
    if len(fs) != 1:
        return []
    v = fs[0]
    try:
        dens = sp.denom(sp.together(orig))
        if dens == 1:
            return []
        roots = sp.solve(dens, v)
    except Exception:
        return []
    if not isinstance(roots, list):
        return []
    holes = []
    for r in roots:
        try:
            a = complex(sp.N(orig.subs(v, r), 20))
            b = complex(sp.N(res.subs(v, r), 20))
        except Exception:
            continue
        if not math.isfinite(abs(a)) and math.isfinite(abs(b)):
            holes.append(r)
    return holes


def c_simp(out, pos, opt):
    """simp EXPR [--order "cancel,trigsimp,fu,factor_terms"] [--probe N] [--no-check]

    按固定顺序依次尝试，只有同时满足「count_ops 严格变小」和「数值抽检通过」才采纳，
    否则回退到上一步。--no-check 关掉数值抽检（只剩 count_ops 判据，慎用）。
    """
    if not pos:
        return out.finish(ok=False, error='用法: simp EXPR [--order "cancel,trigsimp,fu,factor_terms"]')
    orig = _parse(pos[0])
    order_s = opt.get("order")
    if isinstance(order_s, str) and order_s.strip():
        steps = [s.strip() for s in _split(order_s) if s.strip()]
    else:
        steps = list(_SIMP_STEPS)
    unknown = [s for s in steps if s not in _SIMP_OPS]
    if unknown:
        return out.finish(ok=False, error="未知步骤: %s（可用: %s）"
                          % (", ".join(unknown), " / ".join(sorted(_SIMP_OPS))))
    try:
        n_probe = int(opt.get("probe", 3))
    except Exception:
        n_probe = 3
    do_check = not bool(opt.get("no-check"))

    cur, cur_ops = orig, sp.count_ops(orig)
    lines = ["原式: $%s$   ops=%d" % (sp.latex(orig), cur_ops)]
    trace, applied, warn = [], [], []

    for name in steps:
        try:
            cand = _SIMP_OPS[name](cur)
        except Exception as e:
            lines.append("  %-18s 跳过（%s）" % (name, type(e).__name__))
            trace.append({"step": name, "accepted": False, "reason": "exception",
                          "error": type(e).__name__})
            continue
        if cand == cur:
            lines.append("  %-18s 无变化" % name)
            trace.append({"step": name, "accepted": False, "reason": "unchanged"})
            continue
        cand_ops = sp.count_ops(cand)
        verdict, reason = "✗ 回退", None
        ok_num = None
        if do_check:
            ok_num, used, worst = _same_on_samples(cur, cand, n=n_probe)
            if ok_num is False:
                verdict, reason = "✗ 回退", "数值不符"
                warn.append("%s 改变了表达式的值（最大相对偏差 %.1e），已回退" % (name, worst))
                lines.append("  %-18s ops %d → %d   %s（数值不符 %.1e）  %s"
                             % (name, cur_ops, cand_ops, verdict, worst, _brief(cand)))
                trace.append({"step": name, "accepted": False, "reason": "numeric",
                              "rel": worst, "candidate": str(cand)})
                continue
        # 主判据 ops 严格变小；ops 持平时用字符数做次级判据（sin(x)*cos(x) 与
        # sin(2x)/2 的 ops 都是 3，后者更短也更好读，不该被判成「变差」）。
        cur_len, cand_len = len(str(cur)), len(str(cand))
        better = cand_ops < cur_ops or (cand_ops == cur_ops and cand_len < cur_len)
        if not better:
            reason = "not_simpler"
            why = "ops 未变小" if cand_ops > cur_ops else "ops 持平且未变短"
            extra = "，数值一致" if ok_num else ""
            lines.append("  %-18s ops %d → %d   %s（%s%s）  %s"
                         % (name, cur_ops, cand_ops, verdict, why, extra, _brief(cand)))
            trace.append({"step": name, "accepted": False, "reason": reason,
                          "ops_before": cur_ops, "ops_after": cand_ops,
                          "candidate": str(cand)})
            continue
        tag = "✓ 采纳" + ("（已数值抽检）" if ok_num else ("（未能数值抽检）" if do_check else ""))
        note2 = "" if cand_ops < cur_ops else "，字符数 %d → %d" % (cur_len, cand_len)
        lines.append("  %-18s ops %d → %d   %s%s  %s"
                     % (name, cur_ops, cand_ops, tag, note2, _brief(cand)))
        trace.append({"step": name, "accepted": True, "ops_before": cur_ops,
                      "ops_after": cand_ops, "result": str(cand)})
        cur, cur_ops = cand, cand_ops
        applied.append(name)

    if not applied:
        lines.append("没有步骤能改进：原式已按 count_ops 最小。换靶向工具再试——"
                     "三角试 trigsimp --method groebner / fu，带假设的试 refine，"
                     "或 --order \"simplify,factor\" 换流水线")
    if "cancel" in applied:
        hs = _holes(orig, cur)
        if hs:
            vs = sorted(orig.free_symbols, key=str)
            vname = vs[0] if vs else "x"
            warn.append("cancel 填掉了可去奇点 %s：原式在该点无定义，化简后有值，"
                        "两者定义域不同，跨奇点使用前请自行确认"
                        % ", ".join("%s=%s" % (vname, h) for h in hs))
    if warn:
        lines.append("")
        for w in warn:
            lines.append("⚠️ " + w)

    if out.as_json:
        return out.finish(cur, note="\n".join(lines), extra={
            "steps": trace, "applied": applied,
            "ops_before": sp.count_ops(orig), "ops_after": cur_ops,
        })
    print("\n".join(lines))
    print("结果: %s" % cur)
    print("LaTeX: $%s$" % sp.latex(cur))
    _print_timing(out)
    return 0


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
            r_inf = _res_at_oo(f, var)
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
    _print_timing(out)
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


# ================================================================ Dirac 代数
class _DG(sp.Function):
    """gamma^mu（up=1）/ gamma_mu（up=-1）。"""
    is_commutative = False

    def _sympystr(self, pr):
        idx, up = self.args
        return "gamma%s%s" % ("^" if up == 1 else "_", idx)

    def _latex(self, pr):
        idx, up = self.args
        s = pr._print(idx)
        return (r"\gamma^{%s}" if up == 1 else r"\gamma_{%s}") % s


class _DG5(sp.Function):
    is_commutative = False

    def _sympystr(self, pr):
        return "gamma5"

    def _latex(self, pr):
        return r"\gamma^{5}"


class _DSlash(sp.Function):
    is_commutative = False

    def _sympystr(self, pr):
        return "%s/" % self.args[0]

    def _latex(self, pr):
        return r"%s\!\!/" % pr._print(self.args[0])


class _DMet(sp.Function):
    @classmethod
    def eval(cls, a, b):
        if sp.default_sort_key(a) > sp.default_sort_key(b):
            return _DMet(b, a)
        return None

    def _sympystr(self, pr):
        return "g(%s,%s)" % self.args

    def _latex(self, pr):
        a, b = self.args
        return r"g^{%s %s}" % (pr._print(a), pr._print(b))


class _DP2(sp.Function):
    def _sympystr(self, pr):
        return "%s^2" % self.args[0]

    def _latex(self, pr):
        return r"%s^{2}" % pr._print(self.args[0])


class _DDot(sp.Function):
    @classmethod
    def eval(cls, a, b):
        if a == b:
            return _DP2(a)
        if sp.default_sort_key(a) > sp.default_sort_key(b):
            return _DDot(b, a)
        return None

    def _sympystr(self, pr):
        return "(%s.%s)" % self.args

    def _latex(self, pr):
        a, b = self.args
        return r"%s\!\cdot\!%s" % (pr._print(a), pr._print(b))


class _DMom(sp.Function):
    def _sympystr(self, pr):
        p, mu = self.args
        return "%s^%s" % (p, mu)

    def _latex(self, pr):
        p, mu = self.args
        return r"%s^{%s}" % (pr._print(p), pr._print(mu))


class _DEps(sp.Function):
    @classmethod
    def eval(cls, *args):
        names = [str(a) for a in args]
        if len(set(names)) < len(names):
            return S.Zero
        keys = [sp.default_sort_key(a) for a in args]
        order = sorted(range(len(args)), key=lambda i: keys[i])
        if order != list(range(len(args))):
            inv = sum(1 for i in range(len(order)) for j in range(i + 1, len(order))
                      if order[i] > order[j])
            return (-1 if inv % 2 else 1) * _DEps(*[args[i] for i in order])
        return None

    def _sympystr(self, pr):
        return "eps(%s)" % ",".join(map(str, self.args))

    def _latex(self, pr):
        return r"\varepsilon^{%s}" % " ".join(pr._print(a) for a in self.args)


_D_ALIAS = {"μ": "mu", "ν": "nu", "ρ": "rho", "σ": "sigma", "λ": "lam",
            "τ": "tau", "α": "alpha", "β": "beta", "κ": "kappa", "ω": "omega"}


def _d_sym(name):
    name = name.strip().strip("{}").strip()
    return sp.Symbol(_D_ALIAS.get(name, name))


def _d_parse(text):
    """记号 -> 算子序列。支持 gamma^mu / gamma_mu / gamma5 / pslash / 希腊字母。"""
    out = []
    for t in text.replace("*", " ").replace(",", " ").split():
        tl = t.lower()
        if tl in ("gamma5", "g5", "γ5", "gamma^5", "g^5"):
            out.append(_DG5())
        elif tl.endswith("slash"):
            out.append(_DSlash(_d_sym(t[:-5])))
        elif tl.startswith("gamma^") or tl.startswith("g^") or tl.startswith("γ^"):
            out.append(_DG(_d_sym(t.split("^")[1]), sp.Integer(1)))
        elif tl.startswith("gamma_") or tl.startswith("g_") or tl.startswith("γ_"):
            out.append(_DG(_d_sym(t.split("_")[1]), sp.Integer(-1)))
        else:
            raise ValueError("无法解析记号: %s（用 gamma^mu / gamma_mu / gamma5 / pslash）" % t)
    return out


def _d_is_pair(x, y):
    return (isinstance(x, _DG) and isinstance(y, _DG)
            and x.args[0] == y.args[0] and x.args[1] != y.args[1])


def _d_find_pair(seq):
    """找最内层配对（区间内无其它配对），保证中间段是纯 γ 串。"""
    n = len(seq)
    for i in range(n):
        for j in range(i + 1, n):
            if _d_is_pair(seq[i], seq[j]):
                if not any(_d_is_pair(seq[a], seq[b])
                           for a in range(i + 1, j) for b in range(a + 1, j)):
                    return i, j
    return None


def _d_idx(x):
    if isinstance(x, _DG):
        return x.args[0]
    if isinstance(x, _DSlash):
        return sp.Symbol("_s_" + str(x.args[0]))
    raise ValueError("非 γ 对象: %r" % x)


def _d_slash_name(sym):
    s = str(sym)
    return sp.Symbol(s[3:]) if s.startswith("_s_") else None


def _d_met(a, b):
    """指标对的度规；slash 的哑指标分别落成 p·q / p^nu / p^2。"""
    na, nb = _d_slash_name(a), _d_slash_name(b)
    if na is not None and nb is not None:
        return _DDot(na, nb)
    if na is not None:
        return _DMom(na, b)
    if nb is not None:
        return _DMom(nb, a)
    return _DMet(a, b)


def _d_match(items):
    """完美匹配：yield (pairs, sign)，符号 = (-1)^{拉开距离之和}。"""
    if not items:
        yield [], 1
        return
    first, rest = items[0], items[1:]
    for i in range(len(rest)):
        for pairs, s in _d_match(rest[:i] + rest[i + 1:]):
            yield [(first, rest[i])] + pairs, s * (-1) ** i


def _d_simplify(seq):
    """Clifford 收缩：k 奇 → -2·反转；k 偶 → 4·Σ(±g·g 全配对)。γ5 先推到末尾。"""
    seq, s5 = _d_norm_g5(seq)
    seq = list(seq)
    scalar = S.One * s5
    while True:
        hit = None
        for i in range(len(seq) - 1):
            if isinstance(seq[i], _DSlash) and isinstance(seq[i + 1], _DSlash) \
                    and seq[i].args[0] == seq[i + 1].args[0]:
                hit = i
                break
        if hit is not None:
            scalar *= _DP2(seq[hit].args[0])
            seq = seq[:hit] + seq[hit + 2:]
            continue
        pr = _d_find_pair(seq)
        if pr is None:
            break
        i, j = pr
        mid = seq[i + 1:j]
        if len(mid) % 2 == 1:
            seq = seq[:i] + list(reversed(mid)) + seq[j + 1:]
            scalar *= -2
        else:
            s = S.Zero
            for pairs, sign in _d_match(mid):
                term = S.One
                for a, b in pairs:
                    term *= _d_met(_d_idx(a), _d_idx(b))
                s += sign * term
            scalar *= 4 * s
            seq = seq[:i] + seq[j + 1:]
    return sp.expand(scalar * sp.Mul(*seq))


def _d_norm_g5(seq):
    """把 γ5 全部推到末尾；每个 γ5 穿过 n 个 γ 贡献 (-1)^n。"""
    pos = [i for i, x in enumerate(seq) if isinstance(x, _DG5)]
    swaps = sum(sum(1 for x in seq[p + 1:] if not isinstance(x, _DG5)) for p in pos)
    sign = S(-1) ** (swaps % 2)
    rest = [x for x in seq if not isinstance(x, _DG5)]
    return (rest + [_DG5()] if len(pos) % 2 else rest), sign


def _d_tr_gammas(seq):
    """tr(γ^{a1}...γ^{an}) 递推；奇数个为 0，tr(1)=4。"""
    n = len(seq)
    if n % 2 == 1:
        return S.Zero
    if n == 0:
        return S(4)
    if n == 2:
        return 4 * _d_met(_d_idx(seq[0]), _d_idx(seq[1]))
    total = S.Zero
    for j in range(1, n):
        total += (-1) ** (j + 1) * _d_met(_d_idx(seq[0]), _d_idx(seq[j])) \
                 * _d_tr_gammas(seq[1:j] + seq[j + 1:])
    return total


def _d_trace_seq(seq):
    seq, sign = _d_norm_g5(seq)
    n5 = sum(1 for x in seq if isinstance(x, _DG5))
    gs = [x for x in seq if not isinstance(x, _DG5)]
    if n5 % 2 == 1:
        if len(gs) < 4:
            return S.Zero
        if len(gs) == 4:
            return sign * (-4 * sp.I) * _DEps(*[_d_idx(g) for g in gs])
        raise NotImplementedError("tr(gamma5 × 6 个以上 gamma) 暂未实现（该项需手算或查表）")
    return sign * _d_tr_gammas(gs)


def _d_trace(expr):
    expr = sp.expand(expr)
    if isinstance(expr, sp.Add):
        return sp.expand(sum(_d_trace(t) for t in expr.args))
    ops, scal = [], S.One
    for f in (expr.args if isinstance(expr, sp.Mul) else [expr]):
        if f.is_commutative:
            scal *= f
        else:
            ops.append(f)
    return 4 * scal if not ops else scal * _d_trace_seq(ops)


def c_dirac(out, pos, opt):
    """dirac EXPR [--trace]：Clifford 代数化简（保留自由指标）或求迹。"""
    if not pos and isinstance(opt.get("trace"), str):
        pos, opt = [opt["trace"]], {**opt, "trace": True}
    if not pos:
        raise ValueError("用法: dirac EXPR [--trace]（记号: gamma^mu / gamma_mu / gamma5 / pslash）")
    text = " ".join(pos)
    do_tr = bool(opt.get("trace"))
    e = _d_simplify(_d_parse(text))
    if do_tr:
        e = _d_trace(e)
    if out.as_json:
        return out.finish(e, extra={"input": text, "trace": do_tr})
    print("输入: %s" % text)
    print("%s: %s" % ("迹" if do_tr else "化简", e))
    print("LaTeX: $%s$" % sp.latex(e))
    return 0


# ================================================================ 广义相对论：张量
# 公式（n 维；协变/逆变只按矩阵行列取值，不按符号差判号）：
#   Γ^λ_μν  = ½ g^{λσ}(∂_μ g_νσ + ∂_ν g_μσ − ∂_σ g_μν)
#   Γ_λμν   = ½(∂_μ g_νλ + ∂_ν g_μλ − ∂_λ g_μν)
#   R^ρ_σμν = ∂_μ Γ^ρ_νσ − ∂_ν Γ^ρ_μσ + Γ^ρ_μλ Γ^λ_νσ − Γ^ρ_νλ Γ^λ_μσ
#   R_σν    = R^ρ_σρν,   R = g^{σν}R_σν,   G_μν = R_μν − ½R g_μν
#   K       = R_{ρσμν}R^{ρσμν}
#
# 为什么不复用 sympy.diffgeom：实测它要求坐标符号必须与 cs.coord_functions()
# 返回的是同一个对象；传自己造的 Symbol（同名、只是假设不同）会被判为无关变量，
# 求导静默返回 0 —— 张量全零且不报错。另外 metric_to_* 的返回值在 SymPy 1.x
# 之间从 dict 改成了 NDimArray。把这两条封装进来等于把坑一起打包，故自实现。
GR_OPS = ("metric", "christoffel", "riemann", "ricci", "scalar",
          "einstein", "kretschmann", "weyl", "geodesic",
          "covdiff", "divergence", "metric-compat")

# 需要「张量场」作为输入的 op：分量 + 每个指标的上/下都得给
GR_FIELD_OPS = ("covdiff", "divergence")


def _gr_kinds(spec):
    """'u,d' -> ['u','d']；u = 上指标，d = 下指标。顺序即分量的指标顺序。"""
    s = str(spec).replace(",", "").replace(" ", "").lower()
    if not s:
        raise ValueError('指标串为空，例如 "u,d" 表示 (1,1) 型张量')
    bad = [c for c in s if c not in ("u", "d")]
    if bad:
        raise ValueError("指标串只接受 u（上）/ d（下），收到 %r" % "".join(bad))
    return list(s)


def _gr_field(spec, rank, n):
    """把 "[[a,b],[c,d]]" 展平成 {(i,j): expr}，并校验形状与维数一致。"""
    raw = _parse(spec)
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    acc = {}

    def walk(node, path):
        if len(path) == rank:
            if isinstance(node, (list, tuple)):
                raise ValueError("张量是 %d 阶，但分量多嵌套了一层" % rank)
            acc[path] = sp.sympify(node)
            return
        if not isinstance(node, (list, tuple)):
            raise ValueError("张量是 %d 阶，第 %d 层应为列表" % (rank, len(path) + 1))
        if len(node) != n:
            raise ValueError("第 %d 层应有 %d 个分量，实际 %d 个"
                             % (len(path) + 1, n, len(node)))
        for i, ch in enumerate(node):
            walk(ch, path + (i,))

    walk(raw, ())
    return acc


def _gr_covdiff(field, kinds, Gam, X, n, simp):
    """∇_μ T^{a…}_{b…} = ∂_μ T + Σ_上 Γ^{a_k}_{μλ}T^{…λ…} − Σ_下 Γ^λ_{μ b_k}T^{…λ…}

    返回 {(导数指标, k1, k2, …): expr}：阶数 +1，**导数指标排在最前**。
    拿 g 本身（kinds="dd"）跑一遍，结果恒为零 —— 这就是 metric-compat。
    """
    rank = len(kinds)
    out = {}
    for idx, val in field.items():
        for mu in range(n):
            v = sp.diff(val, X[mu])
            for pos in range(rank):
                for lam in range(n):
                    new = list(idx)
                    new[pos] = lam
                    t = field.get(tuple(new), S.Zero)
                    if t == 0:
                        continue
                    if kinds[pos] == "u":
                        v += Gam[idx[pos]][mu][lam] * t
                    else:
                        v -= Gam[lam][mu][idx[pos]] * t
            out[(mu,) + idx] = _gr_simp(v) if simp else v
    return out


def _gr_simp(e):
    """GR 专用化简。三条路各有所长，实测数据：

    - ``cancel``：有理式通分。Schwarzschild 的 Ricci 残项 **9 ms** 归零，
      而 ``simplify`` 要 113 ms、``trigsimp`` 根本不动。
    - ``trigsimp``：三角恒等，但对 tan/cot 形式常常放弃 ——
      sin(2θ)/(2tanθ) − cos(2θ) 用 simplify 990 ms 原样返回。
    - ``trigsimp ∘ rewrite(sin)``：把 tan/cot 拆成 sin/cos 再化简，
      上例 183 ms 得到 sin²θ。

    所以按 cancel → trigsimp → trigsimp∘rewrite 顺序试，取 count_ops 最小者。
    任何一步得到精确 0 就立即返回（GR 里大量分量本该为零，这是最常见情形）。
    """
    if e.is_number and e == 0:
        return S.Zero

    best = e
    try:
        c = sp.cancel(e)
        if c == 0:
            return S.Zero
        best = c
    except Exception:
        pass

    try:
        t = sp.trigsimp(best)
        if t == 0:
            return S.Zero
        if sp.count_ops(t) < sp.count_ops(best):
            best = t
    except Exception:
        pass

    try:
        alt = sp.trigsimp(e.rewrite(sp.sin))
        if alt == 0:
            return S.Zero
        if sp.count_ops(alt) < sp.count_ops(best):
            best = alt
    except Exception:
        pass

    return best


def _gr_coords(spec):
    names = [s.strip() for s in _split(spec) if s.strip()]
    if not names:
        raise ValueError("坐标列表为空")
    return [_sym(n) for n in names]


def _gr_parse(rest, opt):
    """解析度规与坐标，返回 (g, ginv, X)。支持位置参数矩阵或 --diag 简写。"""
    if opt.get("diag") is not None:
        d = opt["diag"]
        if d is True:
            raise ValueError('--diag 需要取值，如 --diag "1,sin(theta)^2"')
        g = sp.diag(*[_parse(v) for v in _split(str(d))])
        if not rest:
            raise ValueError('用 --diag 时仍要给出坐标，如: tensor ricci '
                             '--diag "1,sin(theta)^2" "theta,phi"')
        X = _gr_coords(rest[0])
    else:
        if len(rest) < 2:
            raise ValueError('用法: tensor <op> "[[g00,g01],[g10,g11]]" "coords"\n'
                             '   或: tensor <op> --diag "g00,g11,..." "coords"')
        g = _matrix(rest[0])
        X = _gr_coords(rest[1])
    n = len(X)
    if g.rows != n or g.cols != n:
        raise ValueError("度规是 %dx%d，但给了 %d 个坐标" % (g.rows, g.cols, n))
    if sp.simplify(g - g.T) != sp.zeros(n, n):
        raise ValueError("度规必须对称（g != g^T），检查非对角元")
    g = sp.Matrix(g)
    try:
        ginv = g.inv()
    except Exception as e:
        raise ValueError("度规不可逆（%s）" % type(e).__name__)
    return g, ginv, X


def _gr_dg(g, X):
    """dg[mu][nu][sig] = ∂_sig g_{mu nu}"""
    n = len(X)
    return [[[sp.diff(g[mu, nu], X[sig]) for sig in range(n)]
             for nu in range(n)] for mu in range(n)]


def _gr_christoffel2(g, ginv, X, simp):
    n = len(X)
    dg = _gr_dg(g, X)
    G = [[[S.Zero] * n for _ in range(n)] for _ in range(n)]
    for lam in range(n):
        for mu in range(n):
            for nu in range(mu, n):
                s = S.Zero
                for sig in range(n):
                    s += ginv[lam, sig] * (dg[nu][sig][mu] + dg[mu][sig][nu]
                                           - dg[mu][nu][sig])
                v = s / 2
                G[lam][mu][nu] = G[lam][nu][mu] = (_gr_simp(v) if simp else v)
    return G


def _gr_christoffel1(g, X, simp):
    n = len(X)
    dg = _gr_dg(g, X)
    G = [[[S.Zero] * n for _ in range(n)] for _ in range(n)]
    for lam in range(n):
        for mu in range(n):
            for nu in range(mu, n):
                v = (dg[nu][lam][mu] + dg[mu][lam][nu] - dg[mu][nu][lam]) / 2
                G[lam][mu][nu] = G[lam][nu][mu] = (_gr_simp(v) if simp else v)
    return G


def _gr_riemann(G, X, simp):
    """R^ρ_σμν，利用 μ↔ν 反对称只算一半。"""
    n = len(X)
    dG = [[[[sp.diff(G[rho][mu][nu], X[sig]) for sig in range(n)]
            for nu in range(n)] for mu in range(n)] for rho in range(n)]
    R = [[[[S.Zero] * n for _ in range(n)] for _ in range(n)] for _ in range(n)]
    for rho in range(n):
        for sig in range(n):
            for mu in range(n):
                for nu in range(mu + 1, n):
                    t = dG[rho][nu][sig][mu] - dG[rho][mu][sig][nu]
                    for lam in range(n):
                        t += (G[rho][mu][lam] * G[lam][nu][sig]
                              - G[rho][nu][lam] * G[lam][mu][sig])
                    t = _gr_simp(t) if simp else t
                    R[rho][sig][mu][nu] = t
                    R[rho][sig][nu][mu] = -t
    return R


def _gr_ricci(R, n, simp):
    Ric = [[S.Zero] * n for _ in range(n)]
    for sig in range(n):
        for nu in range(sig, n):
            v = sum(R[rho][sig][rho][nu] for rho in range(n))
            Ric[sig][nu] = Ric[nu][sig] = (_gr_simp(v) if simp else v)
    return Ric


def _gr_scalar(Ric, ginv, n, simp):
    v = sum(ginv[i, j] * Ric[i][j] for i in range(n) for j in range(n))
    return _gr_simp(v) if simp else v


def _gr_einstein(Ric, g, Rs, n, simp):
    out = [[S.Zero] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            v = Ric[i][j] - Rs * g[i, j] / 2
            out[i][j] = _gr_simp(v) if simp else v
    return out


def _gr_lower(R, g, n):
    """R_{ρσμν} = g_{ρα} R^α_σμν"""
    out = [[[[S.Zero] * n for _ in range(n)] for _ in range(n)] for _ in range(n)]
    for rho in range(n):
        for sig in range(n):
            for mu in range(n):
                for nu in range(n):
                    out[rho][sig][mu][nu] = sum(
                        g[rho, a] * R[a][sig][mu][nu] for a in range(n))
    return out


def _gr_kretschmann(Rl, ginv, n, simp):
    """K = R_{ρσμν}R^{ρσμν}。

    必须升满四个指标。踩过的坑：只写 Σ R_{ρσμν}·(ginv R ginv)_{ρσ} 会得到
    R_{ρσμν}R^{ρσ}_{μν}（后两个指标没升），那不是标量，Schwarzschild 下会给出
    含 sin²θ 的怪式子而不是 48M²/r⁶。

    做法：把四指标张量展平成 n²×n² 矩阵 A_{(ρσ),(μν)} = R_{ρσμν}，
    度规提升算子 G_{(ρσ),(αβ)} = g^{ρα}g^{σβ}（即 kron(ginv, ginv)），
    则 K = Σ A_{ij}(G A G)_{ij}。
    """
    idx = [(a, b) for a in range(n) for b in range(n)]
    m = n * n
    A = sp.Matrix(m, m, lambda i, j: Rl[idx[i][0]][idx[i][1]][idx[j][0]][idx[j][1]])
    G = sp.Matrix(m, m, lambda i, j: ginv[idx[i][0], idx[j][0]] * ginv[idx[i][1], idx[j][1]])
    B = G * A * G
    total = sum(A[i, j] * B[i, j] for i in range(m) for j in range(m))
    return _gr_simp(total) if simp else total


def _gr_weyl(R, Ric, g, ginv, Rs, n, simp):
    """C^ρ_σμν（n≥3）；无迹性 C^ρ_σρν = 0 由本地自检核对。"""
    if n < 3:
        raise ValueError("Weyl 张量需要 n≥3（n=2 时公式分母 n−2 = 0）")
    Rup = [[sum(ginv[rho, a] * Ric[a][mu] for a in range(n)) for mu in range(n)]
           for rho in range(n)]

    def d(i, j):
        return S.One if i == j else S.Zero

    c1 = S.One / (n - 2)
    c2 = Rs / ((n - 1) * (n - 2))
    C = [[[[S.Zero] * n for _ in range(n)] for _ in range(n)] for _ in range(n)]
    for rho in range(n):
        for sig in range(n):
            for mu in range(n):
                for nu in range(n):
                    v = R[rho][sig][mu][nu]
                    v -= c1 * (d(rho, mu) * Ric[nu][sig] - d(rho, nu) * Ric[mu][sig]
                               + g[nu, sig] * Rup[rho][mu] - g[mu, sig] * Rup[rho][nu])
                    v += c2 * (d(rho, mu) * g[nu, sig] - d(rho, nu) * g[mu, sig])
                    C[rho][sig][mu][nu] = _gr_simp(v) if simp else v
    return C


def _gr_geodesic(G, X, n):
    """d²x^a/dλ² = −Γ^a_μν v^μ v^ν（v^μ = dx^μ/dλ）"""
    v = [sp.Symbol("v_" + str(x)) for x in X]
    rows = []
    for a in range(n):
        s = S.Zero
        for mu in range(n):
            for nu in range(mu, n):
                c = G[a][mu][nu]
                if c == 0:
                    continue
                s += c * (1 if mu == nu else 2) * v[mu] * v[nu]
        rows.append((X[a], sp.simplify(-s)))
    return v, rows


def _gr_flat(tensor, n, rank):
    """嵌套 list -> {(i,j,...): expr}"""
    acc = {}

    def walk(node, idx):
        if len(idx) == rank:
            acc[tuple(idx)] = node
            return
        for i in range(n):
            walk(node[i], idx + [i])

    walk(tensor, [])
    return acc


def _gr_show(out, title, comps, n, rank, show_all, X):
    ent = sorted(comps.items())
    nz = [(k, v) for k, v in ent if v != 0]
    shown = ent if show_all else nz
    head = "%s：非零 %d / 共 %d   坐标顺序 (%s)" % (
        title, len(nz), len(ent), ", ".join(str(x) for x in X))
    lines = [head]
    for k, v in shown:
        lines.append("  [%s] = %s" % (",".join(str(i) for i in k), v))
    if not nz:
        lines.append("  （全部分量为零）")
    return lines, nz


def c_tensor(out, pos, opt):
    """tensor <op> METRIC|--diag COORDS —— GR 张量运算（Christoffel/Riemann/Ricci/…）"""
    if not pos:
        raise ValueError("用法: tensor <%s> 度规 坐标" % "|".join(GR_OPS))
    op = pos[0].lower()
    if op not in GR_OPS:
        return out.finish(ok=False, error="未知 tensor 操作: %s（可选: %s）"
                          % (op, " / ".join(GR_OPS)))
    simp = not bool(opt.get("no-simplify"))
    show_all = bool(opt.get("all"))

    # covdiff / divergence 要先摘出「张量场 + 指标串」，剩下的才是度规与坐标
    field_spec, kinds = None, None
    if op == "covdiff":
        if len(pos) < 4:
            raise ValueError('用法: tensor covdiff "[[T..]]" "u,d" 度规 坐标\n'
                             '   例: tensor covdiff "[[r,0],[0,r]]" "u,d" '
                             '--diag "1,r^2" "r,phi"')
        field_spec, kinds = pos[1], _gr_kinds(pos[2])
        rest = pos[3:]
    elif op == "divergence":
        if len(pos) < 3:
            raise ValueError('用法: tensor divergence "[V^0,V^1,..]" 度规 坐标\n'
                             '   例: tensor divergence "[r,0]" --diag "1,r^2" "r,phi"')
        field_spec, kinds = pos[1], ["u"]
        rest = pos[2:]
    else:
        rest = pos[1:]

    g, ginv, X = _gr_parse(rest, opt)
    n = len(X)
    field = _gr_field(field_spec, len(kinds), n) if field_spec is not None else None

    if op == "metric":
        det = sp.simplify(g.det())
        lines = ["度规 g_μν："]
        lines += ["  [%s] = %s" % (",".join(str(i) for i in k), v)
                  for k, v in sorted(_gr_flat(g.tolist(), n, 2).items())]
        lines.append("逆度规 g^μν：")
        lines += ["  [%s] = %s" % (",".join(str(i) for i in k), sp.simplify(v))
                  for k, v in sorted(_gr_flat(ginv.tolist(), n, 2).items())]
        lines.append("det(g) = %s" % det)
        if out.as_json:
            return out.finish(det, extra={
                "g": {str(k): str(v) for k, v in _gr_flat(g.tolist(), n, 2).items()},
                "ginv": {str(k): str(v) for k, v in _gr_flat(ginv.tolist(), n, 2).items()},
                "det": str(det)})
        print("\n".join(lines))
        print("结果: det(g) = %s" % det)
        _print_timing(out)
        return 0

    if op == "scalar":
        G = _gr_christoffel2(g, ginv, X, simp)
        R = _gr_riemann(G, X, simp)
        Ric = _gr_ricci(R, n, simp)
        Rs = _gr_scalar(Ric, ginv, n, simp)
        # 对账：G^μ_μ = −R(n−2)/2
        trG = sum(ginv[i, j] * Ric[i][j] for i in range(n) for j in range(n))
        trG = sp.simplify(trG - Rs * n / 2)
        expect = sp.simplify(-Rs * (n - 2) / 2)
        chk = "PASS" if sp.simplify(trG - expect) == 0 else "FAIL"
        note = "对账 G^μ_μ = −R(n−2)/2 → %s" % chk
        return out.finish(Rs, note=note, extra={"check": chk, "dim": n})

    if op == "geodesic":
        G = _gr_christoffel2(g, ginv, X, simp)
        v, rows = _gr_geodesic(G, X, n)
        lines = ["测地线方程（仿射参数 λ，v_x = dx/dλ）："]
        for x, rhs in rows:
            lines.append("  d²%s/dλ² = %s" % (x, rhs))
        if out.as_json:
            return out.finish({str(x): str(r) for x, r in rows},
                              extra={"velocities": [str(s) for s in v]})
        print("\n".join(lines))
        _print_timing(out)
        return 0

    G = _gr_christoffel2(g, ginv, X, simp)

    if op == "christoffel":
        first = bool(opt.get("first"))
        C = _gr_christoffel1(g, X, simp) if first else G
        title = ("Christoffel 第一类 Γ_λμν" if first
                 else "Christoffel 第二类 Γ^λ_μν")
        comps = _gr_flat(C, n, 3)
        lines, nz = _gr_show(out, title, comps, n, 3, show_all, X)
        if out.as_json:
            return out.finish({str(k): str(v) for k, v in nz},
                              extra={"nonzero": len(nz), "total": len(comps),
                                     "kind": 1 if first else 2})
        print("\n".join(lines))
        _print_timing(out)
        return 0

    if op == "metric-compat":
        gg = {(i, j): g[i, j] for i in range(n) for j in range(n)}
        D = _gr_covdiff(gg, ["d", "d"], G, X, n, simp)
        nz = {k: v for k, v in D.items() if v != 0}
        ok = not nz
        lines = ["度量相容性 ∇_α g_μν（%d 个分量）" % len(D)]
        if ok:
            lines.append("  （全部为零 —— 联络与度规相容，这正是 Levi-Civita 联络的定义性质）")
        else:
            for k, v in sorted(nz.items()):
                lines.append("  [%s] = %s" % (",".join(str(i) for i in k), v))
        lines.append("对账 ∇_α g_μν = 0 → %s" % ("PASS" if ok else "FAIL"))
        if out.as_json:
            return out.finish("PASS" if ok else "FAIL", extra={
                "nonzero": len(nz), "total": len(D),
                "components": {str(k): str(v) for k, v in nz.items()}})
        print("\n".join(lines))
        _print_timing(out)
        return 0

    if op == "covdiff":
        D = _gr_covdiff(field, kinds, G, X, n, simp)
        lines, nz = _gr_show(out, "∇_μ T^%s" % "".join(kinds), D, n,
                             len(kinds) + 1, show_all, X)
        lines.append("约定：导数指标排在最前；上/下依指标串 %r" % "".join(kinds))
        if out.as_json:
            return out.finish({str(k): str(v) for k, v in nz},
                              extra={"nonzero": len(nz), "total": len(D),
                                     "kinds": list(kinds)})
        print("\n".join(lines))
        _print_timing(out)
        return 0

    if op == "divergence":
        D = _gr_covdiff(field, ["u"], G, X, n, simp)
        div = sum(D[(m, m)] for m in range(n))
        div = _gr_simp(div) if simp else div
        return out.finish(div, note="∇_μ V^μ（协变散度，标量）")

    R = _gr_riemann(G, X, simp)

    if op == "riemann":
        if opt.get("lower"):
            comps = _gr_flat(_gr_lower(R, g, n), n, 4)
            title = "Riemann 全下指标 R_ρσμν"
        else:
            comps = _gr_flat(R, n, 4)
            title = "Riemann R^ρ_σμν"
        lines, nz = _gr_show(out, title, comps, n, 4, show_all, X)
        if out.as_json:
            return out.finish({str(k): str(v) for k, v in nz},
                              extra={"nonzero": len(nz), "total": len(comps),
                                     "lower": bool(opt.get("lower"))})
        print("\n".join(lines))
        _print_timing(out)
        return 0

    Ric = _gr_ricci(R, n, simp)

    if op == "ricci":
        sym_ok = all(sp.simplify(Ric[i][j] - Ric[j][i]) == 0
                     for i in range(n) for j in range(n))
        comps = {(i, j): Ric[i][j] for i in range(n) for j in range(n)}
        lines, nz = _gr_show(out, "Ricci R_μν", comps, n, 2, show_all, X)
        lines.append("对账 R_μν = R_νμ → %s" % ("PASS" if sym_ok else "FAIL"))
        if not nz:
            lines.append("  → 真空解（Ricci 平坦，但 Riemann 一般不为零）")
        if out.as_json:
            return out.finish({str(k): str(v) for k, v in nz},
                              extra={"nonzero": len(nz), "total": n * n,
                                     "symmetric": sym_ok, "vacuum": not nz})
        print("\n".join(lines))
        _print_timing(out)
        return 0

    if op == "einstein":
        Rs = _gr_scalar(Ric, ginv, n, simp)
        E = _gr_einstein(Ric, g, Rs, n, simp)
        comps = {(i, j): E[i][j] for i in range(n) for j in range(n)}
        lines, nz = _gr_show(out, "Einstein G_μν", comps, n, 2, show_all, X)
        lines.append("R = %s" % Rs)
        if out.as_json:
            return out.finish({str(k): str(v) for k, v in nz},
                              extra={"nonzero": len(nz), "total": n * n,
                                     "ricci_scalar": str(Rs)})
        print("\n".join(lines))
        _print_timing(out)
        return 0

    if op == "kretschmann":
        Rl = _gr_lower(R, g, n)
        K = _gr_kretschmann(Rl, ginv, n, simp)
        return out.finish(K, note="K = R_{ρσμν}R^{ρσμν}（曲率非零的判据：K=0 且 Riemann=0 才是平直）")

    if op == "weyl":
        Rs = _gr_scalar(Ric, ginv, n, simp)
        C = _gr_weyl(R, Ric, g, ginv, Rs, n, simp)
        comps = _gr_flat(C, n, 4)
        lines, nz = _gr_show(out, "Weyl C^ρ_σμν", comps, n, 4, show_all, X)
        # 对账：全无迹 C^ρ_σρν = 0
        tr_ok = all(sp.simplify(sum(C[rho][sig][rho][nu] for rho in range(n))) == 0
                    for sig in range(n) for nu in range(n))
        lines.append("对账 C^ρ_σρν = 0（无迹）→ %s" % ("PASS" if tr_ok else "FAIL"))
        if not nz and Rs == 0:
            lines.append("  → 共形平坦（Weyl 为零）")
        if out.as_json:
            return out.finish({str(k): str(v) for k, v in nz},
                              extra={"nonzero": len(nz), "total": len(comps),
                                     "traceless": tr_ok})
        print("\n".join(lines))
        _print_timing(out)
        return 0

    return out.finish(ok=False, error="未实现的 tensor 操作: %s" % op)


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
    "trigsimp": c_trigsimp,
    "refine": c_refine,
    "fu": c_fu,
    "factor_terms": c_factor_terms,
    "simp": c_simp,
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
    "dirac": c_dirac,
    "tensor": c_tensor,
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

# 走内置加固层 _delegate() 的命令（原 math_tool 实现，已并入本文件）
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
化简     refine EXPR "x:positive,y:real"      假设下化简（simplify 无此能力）
         trigsimp EXPR [--method matching|groebner|combined] [--deep]
         fu EXPR [--rule TR8]                  Hardy 规则集，可单条触发
         factor_terms EXPR                     只提公因子，不展开
         simp EXPR [--order "cancel,trigsimp,fu,factor_terms"] [--probe 3] [--no-check]
                                              自动流水线：ops 变小 + 数值抽检通过才采纳
微积分   diff/integrate/limit/series/summation/product/residue/
         laplace/ilaplace/ftransform
留数     contour EXPR VAR --domain upper|lower|unit|all [--trig] [--no-check]
         （极点枚举 → 按围道筛选 → 2πi·ΣRes → 双通道对账）
求解     solve/solveset/linsolve/nonlinsolve/dsolve/rsolve/diophantine/ineq/nsolve
线代     linalg <det|inv|rank|rref|trace|transpose|nullspace|columnspace|rowspace|
         charpoly|eigen|eigenvects|jordan|qr|cholesky|svd|pinv|expm|norm|cond|solve> MAT [ARG]
数论     nt <factorint|isprime|primes|totient|divisors|nextprime|primepi|crt|cf|
         binomial|partitions> 参数...
统计     stats <pdf|cdf|quantile|sample> <分布> 参数...；dist/expect/var/mgf 走加固层
单位     convert "3*km/hour" "m/s"
物理     wigner j1 j2 j3 m1 m2 m3 [--kind 3j|6j|9j] / cg j1 m1 j2 m2 j m / hydrogen n l [r]
Dirac    dirac EXPR [--trace]  化简 gamma^mu/gamma_mu/gamma5/pslash 串，--trace 求迹
GR张量   tensor <metric|christoffel|riemann|ricci|scalar|einstein|
         kretschmann|weyl|geodesic|covdiff|divergence|metric-compat>
         度规 坐标 [--diag] [--all] [--no-simplify]
         tensor covdiff "[[T..]]" "u,d" 度规 坐标   # 协变导数
         tensor divergence "[V0,V1,..]" 度规 坐标    # ∇_μV^μ
         tensor metric-compat 度规 坐标              # ∇_α g_μν ≡ 0 自检
逻辑     logic <sat|simplify|dnf|cnf> "表达式" [--syms p,q,r]
输出     latex / codegen EXPR [--lang c|fortran|rust|julia|octave] / to-numpy EXPR [vars]
其它     evalf / check / matrix / solve / diff ... 走内置加固层（无需外部依赖）
"""


def main(argv):
    global ENGINE, E_MAP_ENABLED
    as_json = False
    pos, opt = [], {}
    eng_rejected = None          # --engine 后面那个「不是引擎名」的 token
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
            elif key in _VALUE_FLAGS:
                # 受限取值选项：只有下一个 token 命中白名单才消费它。
                # 否则不消费（那个 token 可能是命令名），记下来用于报错提示。
                allowed = _VALUE_FLAGS[key]
                if i + 1 < len(argv) and argv[i + 1].lower() in allowed:
                    opt[key] = argv[i + 1].lower()
                    i += 1
                else:
                    opt[key] = True
                    if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                        eng_rejected = argv[i + 1]
            elif key in _BOOL_FLAGS:
                opt[key] = True
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
    out = Out(cmd, as_json)

    # 引擎选择：--engine 缺值（裸标志）或给了非法值，都报友好错误而不是崩在 .lower()
    _eng = opt.get("engine", "auto")
    if _eng is True:
        names = " / ".join(ENGINE_NAMES)
        if eng_rejected is not None:
            if eng_rejected.lower() in TABLE or eng_rejected.lower() in DELEGATE:
                return out.finish(ok=False, error=(
                    "--engine 需要一个引擎名（%s）；%r 是命令名，不是引擎名。"
                    "若想用 SymEngine 跑 %r，请写 --engine symengine %r"
                    % (names, eng_rejected, eng_rejected, eng_rejected)))
            return out.finish(ok=False, error=(
                "--engine 需要一个引擎名（%s）；收到 %r。"
                % (names, eng_rejected)))
        return out.finish(ok=False, error="--engine 需要一个引擎名（%s）" % names)
    _eng = str(_eng).lower()
    if _eng not in ENGINE_NAMES:
        return out.finish(ok=False, error="未知引擎: %s（可选：%s）"
                          % (_eng, " / ".join(ENGINE_NAMES)))
    ENGINE = _eng

    if "sym" in opt:
        register_syms(opt["sym"])
    if opt.get("preserve-e"):
        E_MAP_ENABLED = False

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
