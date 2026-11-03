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
_CORE_DIRS = [] if os.environ.get("MATHKIT_NO_CORE") else _CORE_DIRS
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
def _parse(s: str):
    """优先用 math_tool.parse（含 ^->**、教材别名、有理化、--sym 假设）。"""
    if _CORE is not None:
        return _CORE.parse(s)
    from sympy.parsing.sympy_parser import (
        parse_expr, standard_transformations, convert_xor,
        implicit_multiplication_application,
    )
    import sympy as _sp
    aliases = {"ln": _sp.log, "arcsin": _sp.asin, "arccos": _sp.acos,
               "arctan": _sp.atan, "arcsinh": _sp.asinh, "arccosh": _sp.acosh,
               "arctanh": _sp.atanh, "lg": lambda v: _sp.log(v, 10),
               "e": _sp.E}
    src = s.replace("^", "**")
    base = standard_transformations + (convert_xor,)
    try:
        return parse_expr(src, transformations=base, local_dict=aliases)
    except Exception:
        return parse_expr(src, transformations=base + (implicit_multiplication_application,),
                          local_dict=aliases)


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
    print("引擎: sympy | %.0f ms" % ((time.perf_counter() - out.t0) * 1000))
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
          "einstein", "kretschmann", "weyl", "geodesic")


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
    g, ginv, X = _gr_parse(pos[1:], opt)
    n = len(X)

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
        return 0

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
统计     stats <pdf|cdf|quantile|sample> <分布> 参数...；dist/expect/var/mgf 走 math_tool
单位     convert "3*km/hour" "m/s"
物理     wigner j1 j2 j3 m1 m2 m3 [--kind 3j|6j|9j] / cg j1 m1 j2 m2 j m / hydrogen n l [r]
Dirac    dirac EXPR [--trace]  化简 gamma^mu/gamma_mu/gamma5/pslash 串，--trace 求迹
GR张量   tensor <metric|christoffel|riemann|ricci|scalar|einstein|
         kretschmann|weyl|geodesic> 度规 坐标 [--diag] [--all] [--no-simplify]
逻辑     logic <sat|simplify|dnf|cnf> "表达式" [--syms p,q,r]
输出     latex / codegen EXPR [--lang c|fortran|rust|julia|octave] / to-numpy EXPR [vars]
其它     evalf / check / matrix / solve / diff ... 自动委托 sympy-math 加固实现
"""


def main(argv):
    global ENGINE
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

    if _CORE is not None:
        if "sym" in opt:
            _CORE.register_syms(opt["sym"])
        if opt.get("preserve-e"):
            _CORE.E_MAP_ENABLED = False
    for k in ("sym",):
        if k in opt and _CORE is None:
            pass

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
