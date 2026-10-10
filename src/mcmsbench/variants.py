"""Task variants and the splits they are drawn for.

A task may name `params`, each with a default and a domain:

    params:
      size:  {default: 9, choices: [7, 9, 11]}
      logs:  {default: 8, range: [6, 10]}
      shape: {default: {s: 9, rooms: 2}, choices: [{s: 9, rooms: 2}, {s: 11, rooms: 3}]}   # values that go together

and use them anywhere in its file as `${expr}`: `${size}`, `${size*size}`, `${shape.rooms}`, `${4*(size-1)*h + 48}`,
`{sx${off(dx)}}` (an offset for the bench's own `{sx+40}`: nothing when it is 0), `${n:+d}` (a format spec), `${'north' if dz < 0 else 'south'}`,
`${['two', 'three', 'four'][doors - 2]}`. They are put in the file's text before it is read as YAML, so the prompt,
setup, events, inventory and grader always agree.

Which values a trial gets is its split's:

  public   the defaults: every task as written, the same every run. Agents are developed here.
  varied   drawn from the domains by a public seed (the task and the trial): different instances, the same for
           everyone. For an agent's developer to see whether what works on the public instance generalises.
  heldout  drawn by a secret key (MCMSBENCH_HELDOUT_KEY, in .env, never committed): instances no one has seen
           before the run, never the public one. Each trial records the key's id (a hash), so scores from different
           keys are not compared. Rotate the key once its instances have been looked at.

A task without params is the same in every split.

A task file is public, and so are the domains in it: an agent developed on the public and varied instances has seen
every value the held-out split can draw from them (commission: all seven targets, in its first eight instances). A
held-out pack is what is never committed: a directory (MCMSBENCH_HELDOUT_DIR, else heldout/ in the checkout, which git
ignores) of <task>.yaml files, each giving that task's held-out split its own domains, and the setup they need:

    params:                 # these params' domains in the heldout split (choices or range), in place of the file's
      target:
        choices:
          - {id: anvil, words: "an anvil", ...}     # the same fields as the file's own choices
    setup:                  # appended to the task's setup in the heldout split; ${...} filled like the file's
      - "setblock {sx+5} {sy} {sz+5} minecraft:iron_ore"

Each held-out trial records its pack's id (`pack_id`, a hash), as it does its key's; one drawn from a task's public
domains carries a caveat that says so. The public and varied splits never read a pack.
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import os
import random
import re
import secrets
from pathlib import Path
from types import SimpleNamespace

import yaml

SPLITS = ("public", "varied", "heldout")
KEY_ENV = "MCMSBENCH_HELDOUT_KEY"
PACK_ENV = "MCMSBENCH_HELDOUT_DIR"
_EXPR = re.compile(r"\$\{([^{}]*)\}")
_FUNCS = {"int": int, "round": round, "min": min, "max": max, "abs": abs,
          "off": lambda n: "" if n == 0 else f"{n:+d}"}     # an offset for the bench's {sx}: {sx${off(dx)}} -> {sx-40}, {sx}


class VariantError(ValueError):
    """A task's params or a `${...}` in it that cannot be used."""


# ------------------------------------------------------------------ the spec

def params_of(text: str) -> dict:
    """A task file's `params` (empty when it has none). The file is read with every `${...}` as 0: a `${...}` inside a
    flow mapping is not YAML until it is filled in, and `params` itself holds none."""
    if "params:" not in text:
        return {}
    d = yaml.safe_load(_EXPR.sub("0", text)) or {}
    spec = d.get("params") or {}
    if not isinstance(spec, dict):
        raise VariantError("`params` is a mapping of name: {default, choices | range}")
    for name, p in spec.items():
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", str(name)):
            raise VariantError(f"param {name!r}: lower-case letters, digits and _")
        if not isinstance(p, dict) or "default" not in p or ("choices" in p) == ("range" in p):
            raise VariantError(f"param {name!r}: needs `default` and one of `choices` or `range`")
        if "choices" in p:
            if not p["choices"] or p["default"] not in p["choices"]:
                raise VariantError(f"param {name!r}: its default must be one of its choices")
        else:
            lo, hi = p["range"]
            if not (isinstance(lo, int) and isinstance(hi, int) and lo <= p["default"] <= hi):
                raise VariantError(f"param {name!r}: `range: [lo, hi]` of integers, the default within it")
    return spec


def domain(p: dict) -> list:
    return list(p["choices"]) if "choices" in p else list(range(p["range"][0], p["range"][1] + 1))


def defaults(spec: dict) -> dict:
    return {k: p["default"] for k, p in spec.items()}


# ------------------------------------------------------------------ the draw

def heldout_key() -> str | None:
    """The key: the environment's, else the bench's .env (read here, not loaded into the environment)."""
    if os.environ.get(KEY_ENV):
        return os.environ[KEY_ENV]
    from dotenv import dotenv_values
    from .config import ROOT
    return dotenv_values(ROOT / ".env").get(KEY_ENV) or None


def key_id(key: str) -> str:
    """A key's public name: enough to tell two keys' results apart, nothing about its draws."""
    return hashlib.sha256(("mcmsbench-heldout|" + key).encode()).hexdigest()[:8]


def new_key() -> str:
    return secrets.token_hex(32)


def draw(task_id: str, spec: dict, split: str, trial: int, key: str | None = None) -> dict:
    """The values trial `trial` of `task_id` gets in `split`. Deterministic: the same split, trial (and key) give the
    same instance. A held-out draw is never the public instance; it is redrawn until it differs."""
    if split not in SPLITS:
        raise VariantError(f"unknown split {split!r}; known: {SPLITS}")
    if split == "public" or not spec:
        return defaults(spec)
    if split == "varied":
        seed = hashlib.sha256(f"varied|{task_id}|{trial}".encode()).digest()
    else:
        if not key:
            raise VariantError(f"the heldout split needs {KEY_ENV} (`mcmsbench heldout init` makes one in .env)")
        seed = hmac.new(key.encode(), f"heldout|{task_id}|{trial}".encode(), hashlib.sha256).digest()
    rng = random.Random(seed)
    for _ in range(64):
        values = {name: rng.choice(domain(spec[name])) for name in sorted(spec)}
        if split != "heldout" or values != defaults(spec):
            return values
    raise VariantError(f"{task_id}: no held-out instance other than the public one (its params have one value each)")


# ------------------------------------------------------------------ held-out packs

def heldout_dir() -> Path | None:
    """The held-out pack's directory: MCMSBENCH_HELDOUT_DIR (the environment's, else the bench's .env), else heldout/
    in the checkout when there is one; None when there is no pack."""
    from .config import ROOT
    d = os.environ.get(PACK_ENV)
    if not d:
        from dotenv import dotenv_values
        d = dotenv_values(ROOT / ".env").get(PACK_ENV) or None
    path = Path(d).expanduser() if d else ROOT / "heldout"
    return path if path.is_dir() else None


def pack_file(task_id: str) -> Path | None:
    d = heldout_dir()
    p = d / f"{task_id}.yaml" if d else None
    return p if p is not None and p.is_file() else None


def pack_id(text: str) -> str:
    """A pack file's public name: enough to tell two packs' results apart, nothing of what is in it."""
    return hashlib.sha256(("mcmsbench-pack|" + text).encode()).hexdigest()[:8]


def pack_spec(task_id: str, spec: dict, text: str) -> dict:
    """The task's params with the pack's domains in place of the file's (`text` is the pack file). A pack param the
    task does not have, a domain that is not choices or a range, or a choice without the fields the file's `${...}`
    read (those of its default), is refused."""
    raw = yaml.safe_load(_EXPR.sub("0", text)) or {}
    over = raw.get("params") or {}
    if not isinstance(over, dict):
        raise VariantError(f"{task_id} pack: `params` is a mapping of name: {{choices | range}}")
    out = {k: dict(v) for k, v in spec.items()}
    for name, p in over.items():
        if name not in spec:
            raise VariantError(f"{task_id} pack: the task has no param {name!r} (it has {sorted(spec)})")
        if not isinstance(p, dict) or ("choices" in p) == ("range" in p):
            raise VariantError(f"{task_id} pack: param {name!r} needs one of `choices` or `range`")
        default = spec[name]["default"]
        if "choices" in p:
            if not p["choices"]:
                raise VariantError(f"{task_id} pack: param {name!r} has no choices")
            for c in p["choices"]:
                if isinstance(default, dict) and (not isinstance(c, dict) or set(c) != set(default)):
                    raise VariantError(f"{task_id} pack: a choice of {name!r} needs the fields {sorted(default)}: {c!r}")
            out[name] = {"default": default, "choices": list(p["choices"])}
        else:
            lo, hi = p["range"]
            if not (isinstance(lo, int) and isinstance(hi, int) and lo <= hi):
                raise VariantError(f"{task_id} pack: param {name!r}: `range: [lo, hi]` of integers")
            out[name] = {"default": default, "range": [lo, hi]}
    return out


def pack_setup(text: str, values: dict) -> list[str]:
    """The pack's setup, its `${...}` filled with the instance's values."""
    raw = yaml.safe_load(render(text, values)) or {}
    setup = raw.get("setup") or []
    if not isinstance(setup, list):
        raise VariantError("a pack's `setup` is a list of commands")
    return [str(c) for c in setup]


# ------------------------------------------------------------------ `${...}`

def render(text: str, values: dict) -> str:
    """The file's text with every `${expr}` (or `${expr:format}`) evaluated against `values`."""
    env = {k: _ns(v) for k, v in values.items()}

    def one(m: re.Match) -> str:
        body = m.group(1).strip()
        expr, fmt = body, ""
        if ":" in body:
            expr, fmt = body.rsplit(":", 1)
        try:
            v = _eval(ast.parse(expr.strip(), mode="eval").body, env)
        except VariantError as e:
            raise VariantError(f"${{{body}}}: {e}") from None
        except (SyntaxError, TypeError, ZeroDivisionError, ValueError) as e:
            raise VariantError(f"${{{body}}}: {type(e).__name__}: {e}") from None
        if fmt:
            return format(v, fmt)
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)
    return _EXPR.sub(one, text)


def _ns(v):
    return SimpleNamespace(**{k: _ns(x) for k, x in v.items()}) if isinstance(v, dict) else v


_BIN = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
        ast.FloorDiv: lambda a, b: a // b, ast.Div: lambda a, b: a / b, ast.Mod: lambda a, b: a % b}
_CMP = {ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
        ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
        ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}


def _eval(n, env: dict):
    """Arithmetic, comparisons, `a if c else b`, `and`/`or`, a param's fields, and int/round/min/max/abs/off. Nothing else:
    a task file is data."""
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float, str, bool)):
        return n.value
    if isinstance(n, ast.Name):
        if n.id not in env:
            raise VariantError(f"no param {n.id!r}")
        return env[n.id]
    if isinstance(n, ast.Attribute):
        base = _eval(n.value, env)
        if not isinstance(base, SimpleNamespace) or not hasattr(base, n.attr):
            raise VariantError(f"no field {n.attr!r}")
        return getattr(base, n.attr)
    if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
        return _BIN[type(n.op)](_eval(n.left, env), _eval(n.right, env))
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd, ast.Not)):
        v = _eval(n.operand, env)
        return -v if isinstance(n.op, ast.USub) else (not v) if isinstance(n.op, ast.Not) else v
    if isinstance(n, ast.IfExp):
        return _eval(n.body, env) if _eval(n.test, env) else _eval(n.orelse, env)
    if isinstance(n, ast.BoolOp):
        vals = [_eval(v, env) for v in n.values]
        return all(vals) if isinstance(n.op, ast.And) else any(vals)
    if isinstance(n, ast.Compare) and all(type(o) in _CMP for o in n.ops):
        left = _eval(n.left, env)
        for op, c in zip(n.ops, n.comparators):
            right = _eval(c, env)
            if not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(n, (ast.List, ast.Tuple)):
        return [_eval(e, env) for e in n.elts]
    if isinstance(n, ast.Subscript):          # a list indexed by a number: ['zero', 'one', 'two'][doors]
        base, i = _eval(n.value, env), _eval(n.slice, env)
        if not isinstance(base, list) or not isinstance(i, int) or isinstance(i, bool):
            raise VariantError("only a list may be indexed, and by a whole number")
        return base[i]
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
        return _FUNCS[n.func.id](*[_eval(a, env) for a in n.args])
    raise VariantError(f"not allowed in a task file: {ast.dump(n)[:60]}")
