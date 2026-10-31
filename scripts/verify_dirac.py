#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dirac 引擎的矩阵级回归测试。

用显式 4x4 Dirac 矩阵（Dirac 表示）直接做矩阵乘法与迹，与符号化简结果逐点比对。
覆盖：2/4/6 个 γ 的迹、γ5+4γ 的迹、以及 k=1..5 重缩并。
每次运行取随机指标，所以能反复跑。

    python verify_dirac.py        # 全过输出 0 fail(s)，退出码 0
"""
import itertools
import os
import sys

import numpy as np
import sympy as sp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mathkit as M  # noqa: E402

G, G5, Met, Eps = M._DG, M._DG5, M._DMet, M._DEps
parse, simplify_seq, trace_expr = M._d_parse, M._d_simplify, M._d_trace
S = sp.Symbol

sx = np.array([[0, 1], [1, 0]], complex)
sy = np.array([[0, -1j], [1j, 0]], complex)
sz = np.array([[1, 0], [0, -1]], complex)
I2, Z = np.eye(2, dtype=complex), np.zeros((2, 2), complex)
GAM = [np.block([[I2, Z], [Z, -I2]])] + [np.block([[Z, s], [-s, Z]]) for s in (sx, sy, sz)]
G5M = 1j * GAM[0] @ GAM[1] @ GAM[2] @ GAM[3]
MET = np.diag([1.0, -1.0, -1.0, -1.0])
N_TRIAL = 12


def eps4(v):
    if len(set(v)) < 4:
        return 0
    inv = sum(1 for i in range(4) for j in range(i + 1, 4) if v[i] > v[j])
    return -1 if inv % 2 else 1


def scal(e, a):
    e = sp.expand(e)
    e = e.replace(lambda x: isinstance(x, Met),
                  lambda x: sp.Float(MET[int(a[x.args[0]]), int(a[x.args[1]])]))
    e = e.replace(lambda x: isinstance(x, Eps),
                  lambda x: sp.Integer(eps4([int(a[k]) for k in x.args])))
    return complex(sp.N(e))


def mat(expr, a):
    expr = sp.expand(expr)
    if isinstance(expr, sp.Add):
        return sum(mat(t, a) for t in expr.args)
    if isinstance(expr, sp.Mul):
        m = np.eye(4, dtype=complex)
        for f in expr.args:
            m = m * scal(f, a) if f.is_commutative else m @ mat(f, a)
        return m
    if isinstance(expr, G):
        return GAM[int(a[expr.args[0]])]
    if expr.is_commutative:
        return np.eye(4) * scal(expr, a)
    raise TypeError(expr)


def direct(text, a, sumidx=None):
    seq = parse(text)
    if sumidx is None:
        m = np.eye(4, dtype=complex)
        for x in seq:
            m = m @ (G5M if isinstance(x, G5) else GAM[int(a[x.args[0]])])
        return m
    i = next(k for k, x in enumerate(seq) if isinstance(x, G) and x.args[0] == sumidx and x.args[1] == 1)
    j = next(k for k, x in enumerate(seq) if isinstance(x, G) and x.args[0] == sumidx and x.args[1] == -1)
    out = np.zeros((4, 4), complex)
    for mu, rho in itertools.product(range(4), repeat=2):
        m = np.eye(4, dtype=complex)
        for k, x in enumerate(seq):
            if k == i:
                m = m @ GAM[mu]
            elif k == j:
                m = m @ GAM[rho]
            elif isinstance(x, G):
                m = m @ GAM[int(a[x.args[0]])]
        out += MET[mu, rho] * m
    return out


TRACES = [
    ("gamma^mu gamma^nu", ["mu", "nu"]),
    ("gamma^mu gamma^nu gamma^rho gamma^sigma", ["mu", "nu", "rho", "sigma"]),
    ("gamma^mu gamma^nu gamma^rho gamma^sigma gamma^lam gamma^tau",
     ["mu", "nu", "rho", "sigma", "lam", "tau"]),
    ("gamma5 gamma^mu gamma^nu gamma^rho gamma^sigma", ["mu", "nu", "rho", "sigma"]),
]
CONTRACTS = [
    ("gamma^mu gamma^nu gamma_mu", ["nu"]),
    ("gamma^mu gamma^nu gamma^rho gamma_mu", ["nu", "rho"]),
    ("gamma^mu gamma^nu gamma^rho gamma^sigma gamma_mu", ["nu", "rho", "sigma"]),
    ("gamma^mu gamma^nu gamma^rho gamma^sigma gamma^lam gamma_mu", ["nu", "rho", "sigma", "lam"]),
]


def main():
    rng = np.random.default_rng(20261031)
    fails = 0
    for text, names in TRACES:
        symres = trace_expr(simplify_seq(parse(text)))
        ok = True
        for _ in range(N_TRIAL):
            vals = [int(rng.integers(0, 4)) for _ in names]
            a = {S(n): v for n, v in zip(names, vals)}
            lhs, rhs = np.trace(direct(text, a)), scal(symres, a)
            if abs(lhs - rhs) > 1e-9:
                ok = False
                print("  MISMATCH", text, vals, lhs, rhs)
                break
        print(("PASS " if ok else "FAIL ") + "tr(%-55s)" % text)
        fails += 0 if ok else 1
    for text, names in CONTRACTS:
        symres = simplify_seq(parse(text))
        ok = True
        for _ in range(N_TRIAL):
            vals = [int(rng.integers(0, 4)) for _ in names]
            a = {S(n): v for n, v in zip(names, vals)}
            if not np.allclose(direct(text, a, sumidx=S("mu")), mat(symres, a), atol=1e-9):
                ok = False
                print("  MISMATCH", text, vals)
                break
        print(("PASS " if ok else "FAIL ") + "%-58s" % text)
        fails += 0 if ok else 1
    print("\n%d fail(s)  (%d cases x %d random index sets)" % (fails, len(TRACES) + len(CONTRACTS), N_TRIAL))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
