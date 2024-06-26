from __future__ import annotations
from typing import Iterator, Optional, Tuple, Any, Dict, List, DefaultDict, Set, Callable
import functools, itertools, heapq
from collections import defaultdict
from enum import Enum, auto
from dataclasses import dataclass
from tinygrad.dtype import dtypes, DType
from tinygrad.shape.symbolic import sint, Variable
from tinygrad.ops import UnaryOps, BinaryOps, TernaryOps, exec_alu
from tinygrad.helpers import prod, DEBUG, getenv

# the order of these UOps controls the order of the toposort
class UOps(Enum):
  # ops that aren't rendered
  SINK = auto()
  DEFINE_GLOBAL = auto(); DEFINE_VAR = auto(); DEFINE_LOCAL = auto(); DEFINE_ACC = auto() # noqa: E702
  CONST = auto(); SPECIAL = auto() # noqa: E702
  NOOP = auto(); UNMUL = auto(); GEP = auto() # noqa: E702
  # math ops
  CAST = auto(); BITCAST = auto() # noqa: E702
  ALU = auto(); WMMA = auto() # noqa: E702
  # memory/assignment ops
  LOAD = auto(); STORE = auto(); PHI = auto() # noqa: E702
  # control flow ops
  BARRIER = auto(); IF = auto(); RANGE = auto() # noqa: E702
  # these two are not graph nodes
  ENDRANGE = auto(); ENDIF = auto() # noqa: E702

@dataclass(eq=False)
class UOp:
  uop: UOps
  dtype: Optional[DType] = None
  vin: Tuple[UOp, ...] = tuple()
  arg: Any = None
  def tuple(self): return (self.uop, self.dtype, self.vin, self.arg)
  @functools.cached_property
  def cmp_tuple(self):
    # NOTE: this sort of DEFINE_VAR shouldn't have to be here. only for PTX
    return (self.uop.value, (self.arg if self.uop is not UOps.DEFINE_VAR else self.arg.expr) if self.uop is not UOps.ALU else \
            (type(self.uop), self.uop.value), self.dtype, self.vin)
  def __lt__(self, x:UOp): return self.cmp_tuple < x.cmp_tuple
  def __repr__(self):
    return f"{str(self.uop):20s}: {str(self.dtype) if self.dtype is not None else '':25s} {str([x.uop for x in self.vin]):32s} {self.arg}"
  def cast(self, dtype): return UOp(UOps.CAST, dtype, (self,))
  def __neg__(self): return UOp.alu(UnaryOps.NEG, self)
  def __add__(self, x): return UOp.alu(BinaryOps.ADD, self, x)
  def __sub__(self, x): return UOp.alu(BinaryOps.SUB, self, x)
  def __mul__(self, x): return UOp.alu(BinaryOps.MUL, self, x)
  @staticmethod
  def max(x, y): return UOp.alu(BinaryOps.MAX, x, y)
  @staticmethod
  def min(x, y): return -UOp.alu(BinaryOps.MAX, -x, -y)
  @staticmethod
  def const(dtype, val): return UOp(UOps.CONST, dtype, arg=dtypes.as_const(val, dtype))
  @staticmethod
  def alu(arg, *vin:UOp): return UOp(UOps.ALU, vin[0].dtype, vin, arg)
  @functools.cached_property
  def parents(self) -> Set[UOp]: return set.union(set(self.vin), *[x.parents for x in self.vin])

def uop_alu_resolve(u:UOp) -> sint:
  if u.uop is UOps.CONST: return u.arg
  elif u.uop is UOps.DEFINE_VAR: return u.arg
  elif u.uop is UOps.SPECIAL: return u.arg[2]-1
  elif u.uop is UOps.ALU and u.arg is BinaryOps.MUL: return uop_alu_resolve(u.vin[0]) * uop_alu_resolve(u.vin[1])
  elif u.uop is UOps.ALU and u.arg is BinaryOps.ADD: return uop_alu_resolve(u.vin[0]) + uop_alu_resolve(u.vin[1])
  else: raise RuntimeError(f"ALU resolve fail @ {u.uop}")

# *** simplification logic ***

def _match(uop:UOp, pattern:Dict[str, Any], store:Dict[str, UOp]) -> bool:
  for k,v in pattern.items():
    if k == "__name__":
      if v in store and store[v] != uop: return False
      store[v] = uop
    elif k == "arg":
      if uop.arg != v: return False
    elif k == "dtype":
      if isinstance(v, set):
        if uop.dtype not in v: return False
      elif uop.dtype != v: return False
    elif k == "uop":
      if isinstance(v, set):
        if uop.uop not in v: return False
      elif uop.uop != v: return False
    elif k == "vin":
      # only one if it's a tuple
      # try all permutations if it's a list
      # repeat if it's a dict
      for vp in itertools.permutations(v) if isinstance(v, list) else ([v] if isinstance(v, tuple) else [(v,)*len(uop.vin)]):
        if len(uop.vin) != len(vp) and (len(uop.vin) not in pattern.get('__allow_len__', [])): return False
        new_store = store.copy()
        if all(_match(uu, vv, new_store) for uu, vv in zip(uop.vin, vp)):
          for k,v in new_store.items(): store[k] = v
          return True
      return False
  return True

class PatternMatcher:
  def __init__(self, patterns:List[Tuple[Dict[str, Any], Callable]]):
    self.patterns = patterns
    self.pdict: DefaultDict[Tuple[UOps, Any], List[Tuple[Dict[str, Any], Callable]]] = defaultdict(list)
    # uop is required, arg is optional
    for p,fxn in self.patterns:
      uops = p["uop"]
      if isinstance(uops, set):
        for uop in uops: self.pdict[(uop, p.get("arg", None))].append((p, fxn))
      else:
        self.pdict[(uops, p.get("arg", None))].append((p, fxn))

  def rewrite(self, uop:UOp) -> Optional[UOp]:
    for p,fxn in itertools.chain(self.pdict[(uop.uop, uop.arg)], self.pdict[(uop.uop, None)]):
      store: Dict[str, UOp] = {}
      if _match(uop, p, store): return fxn(**store)
    return None

def sum_collapse(phi_input, loop, val1, val2):
  for v1,v2 in [(val1, val2), (val2, val1)]:
    if loop not in v1.parents:
      loop_range = loop.vin[1]-loop.vin[0]
      ret = v1*loop_range.cast(v1.dtype)
      return UOp(UOps.PHI, phi_input.dtype, (phi_input, v2))+ret
  return None

def loop_collapse(loop_start, loop_end, compval, idx, mval, multconst):
  if mval.arg >= 0 or loop_start.arg != 0:
    # TODO: support and test this with other mvals and loop_starts
    if DEBUG >= 1: print(f"WARNING, NOT FOLDING: mval:{mval.arg} loop_start:{loop_start.arg}")
    return None
  comprange = UOp.min(loop_end, UOp.max(UOp.alu(BinaryOps.DIV, idx-compval-mval, mval) + (loop_end-loop_start), loop_start))
  return UOp(UOps.UNMUL, multconst.dtype, (comprange.cast(multconst.dtype) * multconst, loop_end-loop_start))

# this is symbolic 2.0
constant_folder = PatternMatcher([
  # arange loop folding (early)
  ({"uop": UOps.ALU, "arg": TernaryOps.WHERE, "vin": ({"uop": UOps.ALU, "arg": BinaryOps.CMPLT, "vin": (
    {"uop": UOps.ALU, "arg": BinaryOps.ADD, "vin":
      [{"__name__": "idx"}, {"uop": UOps.ALU, "arg": BinaryOps.MUL,
        "vin": [{"__name__": "mval", "uop": UOps.CONST}, {"uop": UOps.RANGE, "vin": ({"__name__": "loop_start"}, {"__name__": "loop_end"})}]}]},
      {"__name__": "compval", "uop": UOps.CONST})}, {"__name__": "multconst", "uop": UOps.CONST}, {"uop": UOps.CONST, "arg": 0})}, loop_collapse),
  # sum collapse to mul (with possible GEP)
  ({"uop": UOps.PHI, "vin": ({"__name__": "phi_input", "uop": UOps.DEFINE_ACC, "vin": ({"uop": UOps.RANGE, "__name__": "loop"},)},
      {"uop": UOps.ALU, "arg": BinaryOps.ADD, "vin": ({"__name__": "val1"}, {"__name__": "val2"})})}, sum_collapse),
  ({"uop": UOps.PHI, "vin": ({"__name__": "phi_input", "uop": UOps.GEP,
                              "vin": ({"uop": UOps.DEFINE_ACC, "vin":({"uop": UOps.RANGE, "__name__": "loop"},)},)},
      {"uop": UOps.ALU, "arg": BinaryOps.ADD, "vin": ({"__name__": "val1"}, {"__name__": "val2"})})}, sum_collapse),
  # deal with UNMUL
  ({"uop": UOps.ALU, "arg": BinaryOps.MUL, "vin": [{"uop": UOps.CONST, "__name__": "c1"},
                                                   {"uop": UOps.UNMUL, "vin": [{"uop": UOps.CONST, "__name__": "c2"}, {"__name__": "v"}]}]},
                                                   lambda c1,c2,v: v if c1.arg == c2.arg else None),
  ({"uop": UOps.UNMUL, "vin": ({"uop": UOps.CONST, "__name__": "zero", "arg": 0}, {})}, lambda zero: zero),
  ({"__name__": "root", "uop": UOps.CAST, "vin": ({"uop": UOps.UNMUL, "__name__": "unmul"},)},
    lambda root,unmul: UOp(UOps.UNMUL, root.dtype, (unmul.vin[0].cast(root.dtype), unmul.vin[1]))),
  # max on special can go away (TODO: special should be variable, same thing applies)
  ({"uop": UOps.ALU, "arg": BinaryOps.MAX, "vin": [{"__name__": "c", "uop": UOps.CONST}, {"__name__": "s", "uop": UOps.SPECIAL}]},
    lambda c,s: c if (s.arg[2]-1) <= c.arg else None),
  # const rules
  ({"__name__": "root", "uop": UOps.GEP, "vin": ({"__name__": "c", "uop": UOps.CONST},)}, lambda root, c: UOp.const(root.dtype, c.arg)),
  ({"__name__": "root", "uop": UOps.CAST, "vin": {"__name__": "c", "uop": UOps.CONST}}, lambda root, c: UOp.const(root.dtype, c.arg)),
  # a phi on a DEFINE_ACC without loops or a CONST is a noop. this is for correctness, not just speed
  ({"uop": UOps.PHI, "vin": ({"uop": UOps.DEFINE_ACC, "__name__": "acc"}, {"__name__": "acc"})}, lambda acc: UOp.const(acc.dtype, acc.arg[0])),
  ({"uop": UOps.PHI, "vin": ({"uop": UOps.DEFINE_ACC, "vin": tuple()}, {"__name__": "x"})}, lambda x: x),
  ({"uop": UOps.PHI, "vin": ({"uop": UOps.CONST}, {"__name__": "x"})}, lambda x: x),
  # a DEFINE_ACC without inputs is a const + GEP on a const is the const
  ({"__name__": "root", "uop": UOps.DEFINE_ACC, "vin": tuple()}, lambda root: UOp.const(root.dtype, root.arg[0])),
  ({"__name__": "root", "uop": UOps.GEP, "vin": ({"__name__": "x", "uop": UOps.CONST},)}, lambda root,x: UOp.const(root.dtype, x.arg)),
  # max -2147483648
  ({"uop": UOps.ALU, "arg": BinaryOps.MAX, "dtype": dtypes.int, "vin": [{"__name__": "x"}, {"uop": UOps.CONST, "arg": -2147483648}]}, lambda x: x),
  # -(-x) -> x
  ({"uop": UOps.ALU, "arg": UnaryOps.NEG, "vin": ({"uop": UOps.ALU, "arg": UnaryOps.NEG, "vin": ({"__name__": "x"},)})}, lambda x: x),
  # x+-y -> x-y
  ({"uop": UOps.ALU, "arg": BinaryOps.ADD, "vin": ({"__name__": "x"}, {"__name__": "my", "uop": UOps.ALU, "arg": UnaryOps.NEG})},
    lambda x, my: x-my.vin[0]),
  # -1*x -> -x
  ({"uop": UOps.ALU, "arg": BinaryOps.MUL, "vin": [{"__name__": "x"}, {"uop": UOps.CONST, "arg": -1}]}, lambda x: -x),
  # bool < False is always false, True < bool is always false
  ({"uop": UOps.ALU, "arg": BinaryOps.CMPLT, "vin": ({}, {"__name__": "x", "uop": UOps.CONST, "dtype": dtypes.bool, "arg": False})}, lambda x: x),
  ({"uop": UOps.ALU, "arg": BinaryOps.CMPLT, "vin": ({"__name__": "x", "uop": UOps.CONST, "dtype": dtypes.bool, "arg": True}, {})},
    lambda x: UOp.const(dtypes.bool, False)),
  # a conditional with the same results either way is a noop, also fold const conditionals
  ({"uop": UOps.ALU, "arg": TernaryOps.WHERE, "vin": ({}, {"__name__": "val"}, {"__name__": "val"})}, lambda val: val),
  ({"uop": UOps.ALU, "arg": TernaryOps.WHERE, "vin": ({"__name__": "gate", "uop": UOps.CONST}, {"__name__": "c0"}, {"__name__": "c1"})},
    lambda gate, c0, c1: c0 if gate.arg else c1),
  # ** constant folding **
  ({"__name__": "root", "uop": UOps.ALU, "vin": {"uop": UOps.CONST}},
    lambda root: UOp.const(root.dtype, exec_alu(root.arg, root.dtype, [x.arg for x in root.vin]))),
  # ** self folding **
  ({"uop": UOps.ALU, "arg": BinaryOps.ADD, "vin": [{"__name__": "x"}, {"uop": UOps.CONST, "arg": 0}]}, lambda x: x),   # x+0 -> x or 0+x -> x
  ({"uop": UOps.ALU, "arg": BinaryOps.MUL, "vin": [{"__name__": "x"}, {"uop": UOps.CONST, "arg": 1}]}, lambda x: x),   # x*1 -> x or 1*x -> x
  ({"uop": UOps.ALU, "arg": BinaryOps.SUB, "vin": ({"__name__": "x"}, {"uop": UOps.CONST, "arg": 0})}, lambda x: x),   # x-0 -> x
  ({"uop": UOps.ALU, "arg": BinaryOps.DIV, "vin": ({"__name__": "x"}, {"uop": UOps.CONST, "arg": 1})}, lambda x: x),   # x/1 -> x
  ({"uop": UOps.ALU, "arg": BinaryOps.DIV, "vin": ({"__name__": "x"}, {"uop": UOps.CONST, "arg": -1})}, lambda x: -x), # x/-1 -> -x
  # ** zero folding **
  ({"uop": UOps.ALU, "arg": BinaryOps.MUL, "vin": [{}, {"__name__": "c", "uop": UOps.CONST, "arg": 0}]}, lambda c: c), # x*0 -> 0 or 0*x -> 0
  ({"uop": UOps.ALU, "arg": BinaryOps.SUB, "vin": ({"__name__": "x"}, {"__name__": "x"})}, lambda x: UOp.const(x.dtype, 0)),   # x-x -> 0
  # ** load/store folding **
  ({"uop": UOps.STORE, "vin": ({"__name__": "buf"}, {"__name__": "idx"},
                               {"uop": UOps.LOAD, "vin": ({"__name__": "buf"}, {"__name__": "idx"})})}, lambda buf, idx: UOp(UOps.NOOP)),
  # ** two stage add/sub folding **
  ({"uop": UOps.ALU, "arg": BinaryOps.ADD, "vin": [{"uop": UOps.ALU, "arg": BinaryOps.ADD,
                     "vin": [{"__name__": "x"}, {"__name__": "c1", "uop": UOps.CONST}]}, {"__name__": "c2", "uop": UOps.CONST}]},
     lambda x,c1,c2: x+UOp.const(x.dtype, exec_alu(BinaryOps.ADD, x.dtype, [c1.arg, c2.arg]))),
  ({"uop": UOps.ALU, "arg": BinaryOps.ADD, "vin": [{"uop": UOps.ALU, "arg": BinaryOps.SUB,
                     "vin": ({"__name__": "x"}, {"__name__": "c1", "uop": UOps.CONST})}, {"__name__": "c2", "uop": UOps.CONST}]},
     lambda x,c1,c2: x+UOp.const(x.dtype, exec_alu(BinaryOps.SUB, x.dtype, [c2.arg, c1.arg]))),
  # TODO: can do the invert of this (flip alt/load) when we fix double ops
  ({"uop": UOps.STORE, "vin": ({"__name__": "buf"}, {"__name__": "idx"}, {"uop": UOps.ALU, "arg": TernaryOps.WHERE,
                       "vin": ({"__name__": "gate"}, {"__name__": "alt"}, {"uop": UOps.LOAD, "vin": ({"__name__": "buf"}, {"__name__": "idx"})})})},
    lambda buf, idx, gate, alt: UOp(UOps.STORE, None, (buf, idx, alt, gate))),
  # store float4/float2 directly (remove CAST/GEP)
  ({"uop": UOps.STORE, "vin": ({"__name__": "buf"}, {"__name__": "idx"}, {"uop": UOps.CAST, "vin":
                                tuple({"uop": UOps.GEP, "vin": ({"__name__": "val"},), "arg": i} for i in range(4))})},
   lambda buf,idx,val: UOp(UOps.STORE, None, (buf, idx, val))),
  ({"uop": UOps.STORE, "vin": ({"__name__": "buf"}, {"__name__": "idx"}, {"uop": UOps.CAST, "vin":
                                tuple({"uop": UOps.GEP, "vin": ({"__name__": "val"},), "arg": i} for i in range(2))})},
   lambda buf,idx,val: UOp(UOps.STORE, None, (buf, idx, val))),
  # CAST-PHI-GEP -> PHI-CAST
  ({"__name__": "root", "uop": UOps.CAST, "vin":
    tuple({"uop": UOps.PHI, "vin": ({"uop": UOps.GEP, "vin": ({"__name__": "val"},), "arg": i}, {"__name__": f"v{i}"})} for i in range(4))},
    lambda root, val, v0, v1, v2, v3: UOp(UOps.PHI, root.dtype, (val, UOp(UOps.CAST, val.dtype, (v0, v1, v2, v3))))),
  ({"__name__": "root", "uop": UOps.CAST, "vin":
    tuple({"uop": UOps.PHI, "vin": ({"uop": UOps.GEP, "vin": ({"__name__": "val"},), "arg": i}, {"__name__": f"v{i}"})} for i in range(2))},
    lambda root, val, v0, v1: UOp(UOps.PHI, root.dtype, (val, UOp(UOps.CAST, val.dtype, (v0, v1))))),
  # NEG/CMPLT -> CMPLT
  ({"uop": UOps.ALU, "arg": BinaryOps.CMPLT, "vin": ({"uop": UOps.ALU, "arg": UnaryOps.NEG, "vin": ({"__name__": "x"},)},
                                                     {"__name__": "c", "uop": UOps.CONST, "dtype": dtypes.int})},
    lambda c,x: UOp(UOps.ALU, dtypes.bool, (UOp.const(c.dtype, -c.arg), x), BinaryOps.CMPLT)),
  # cast NOOP (NOTE: it's str to deal with PtrDType)
  ({"__name__": "root", "uop": UOps.CAST}, lambda root: root.vin[0] if str(root.dtype) == str(root.vin[0].dtype) else None),
])

# *** uop graph ***

class UOpGraph:
  def __init__(self):
    self.nodes: Dict[Tuple, UOp] = {}
    self._uops: Optional[List[UOp]] = None

  def __iter__(self) -> Iterator[UOp]: return iter(self.uops)
  def __getitem__(self, index) -> UOp: return self.uops[index]

  def vars(self) -> List[Variable]: return [x.arg for x in self.uops if x.uop is UOps.DEFINE_VAR]
  def globals(self) -> List[Tuple[int, bool]]: return [x.arg for x in self.uops if x.uop is UOps.DEFINE_GLOBAL]

  @property
  def uops(self):
    if self._uops is None: self.linearize()
    return self._uops

  def graph(self):
    from tinygrad.engine.graph import graph_uops
    graph_uops(self.uops)

  def print(self):
    for i,u in enumerate(self):
      print(f"{i:4d} {str(u.uop):20s}: {str(u.dtype) if u.dtype is not None else '':25s} " f"{str([self.uops.index(x) for x in u.vin]):32s} {u.arg}")

  def graph_rewrite(self, sink, pm):
    # recursive rewrite
    changed = getenv("UOPS_REWRITE", 1)
    run_cnt = 0
    while changed:
      changed = 0
      @functools.lru_cache
      def rewrite(u:UOp) -> UOp:
        nonlocal changed
        recurse_cnt = 0
        up = u
        # locally recursively rewrite
        while (rewritten := pm.rewrite(up)):
          assert recurse_cnt < 100, f"recursive_rewrite looped {up} <--> {rewritten}"
          up = rewritten
          recurse_cnt += 1
        changed += recurse_cnt
        # NOTE: this changes UOp, so we have to delete caches
        up.vin = tuple(rewrite(x) for x in up.vin)
        if hasattr(up, "parents"): del up.parents
        if hasattr(up, "cmp_tuple"): del up.cmp_tuple
        # replace with cached nodes
        if found:=self.nodes.get(key:=up.tuple()): return found
        else: self.nodes[key] = up
        return up
      sink = rewrite(sink)
      run_cnt += 1
      assert run_cnt < 100, "exceeded 100 rewrite loops!"
    return sink

  def linearize(self, extra_pm:Optional[PatternMatcher]=None, type_verify=True):
    # NOTE: relinearizering should be okay
    #assert self._uops is None, "already linearized"

    # get sink
    _sinks: List[UOp] = []
    for u in self.nodes.values():
      if u.uop is UOps.STORE: _sinks.append(u)
      if u.uop is UOps.SINK: _sinks.extend(u.vin)
    sink = UOp(UOps.SINK, None, tuple(_sinks))
    del _sinks

    sink = self.graph_rewrite(sink, constant_folder)
    if extra_pm: sink = self.graph_rewrite(sink, PatternMatcher(constant_folder.patterns+extra_pm.patterns))

    # filter nodes that don't link to a sink
    # BFS toposort
    graph: DefaultDict[UOp, List[UOp]] = defaultdict(list)
    in_degree: DefaultDict[UOp, int] = defaultdict(int)
    loops = []
    ifs = []
    nodes: Dict[UOp, None] = {}
    def add_parents(u:UOp):
      if u in nodes: return
      nodes[u] = None
      for x in u.vin:
        add_parents(x)
        in_degree[u] += 1
        graph[x].append(u)
      if u.uop is UOps.RANGE: loops.append(u)
      if u.uop is UOps.IF: ifs.append(u)
    sink = UOp(UOps.SINK, None, tuple(x for x in sink.vin if x.uop is not UOps.NOOP))
    add_parents(sink)

    @functools.lru_cache(None)
    def get_recursive_children(x:UOp, include_self=False) -> Set[UOp]:
      if x.uop is UOps.SINK: return set()
      return set.union(set((x,)) if include_self else set(), *([get_recursive_children(u, True) for u in graph[x]] if x.uop is not UOps.PHI else []))
    loops_children = {l:get_recursive_children(l) for l in loops[::-1]}

    queue: List = []
    def push(u):
      priority = 0
      # prefer uops that are loop children
      for l, ss in loops_children.items():
        if u in ss: priority -= l.arg[0]*1000 + l.arg[1]
      heapq.heappush(queue, (priority, u))

    for u in nodes:
      if in_degree[u] == 0: push(u)

    if getenv("FUZZ_UOPS", 0):
      from test.external.fuzz_uops import fuzz_uops
      self.fuzz_paths = fuzz_uops(graph, in_degree.copy(), loops_children)

    self._uops = []
    while queue:
      p,x = heapq.heappop(queue)
      if DEBUG >= 7: print(p,x)
      if x.uop is UOps.DEFINE_ACC and len(x.vin):
        idx = min([self._uops.index(l) for l in x.vin])
        self._uops.insert(idx, x)
      else:
        self._uops.append(x)
      for u, ss in loops_children.items():
        if x in ss:
          ss.remove(x)
          if len(ss) == 0: self._uops.append(UOp(UOps.ENDRANGE, None, (u,)))
      for u in graph[x]:
        in_degree[u] -= 1
        if in_degree[u] == 0: push(u)

    assert self._uops[-1].uop is UOps.SINK, f"didn't end with SINK, ended with {self._uops[-1]}"
    self._uops = self._uops[:-1]

    # TODO: ifs should be removed and just the store should be gated
    for u in ifs[::-1]: self._uops.append(UOp(UOps.ENDIF, None, (u,)))

    if type_verify: self.type_verify()

  def add(self, uop:UOps, dtype:Optional[DType]=None, vin:Tuple[UOp, ...]=tuple(), arg:Any=None) -> UOp:
    if found:=self.nodes.get(key:=(uop, dtype, vin, arg)): return found
    self.nodes[key] = ret = UOp(*key)
    return ret

  # *** checker functions ***

  def flops_mem(self) -> Tuple[sint, sint]:
    flops: sint = 0
    mem: sint = 0
    mults: sint = 1
    mult_stack = []
    for u in self.uops:
      if u.uop is UOps.RANGE:
        mult_stack.append(mults)
        mults *= uop_alu_resolve(u.vin[1])
      elif u.uop is UOps.ENDRANGE:
        mults = mult_stack.pop(-1)
      elif u.uop is UOps.ALU:
        flops += mults * (2 if u.arg == TernaryOps.MULACC else 1)
      elif u.uop is UOps.LOAD:
        assert u.dtype is not None
        mem += u.dtype.itemsize * mults
      elif u.uop is UOps.STORE:
        assert u.vin[2].dtype is not None
        mem += u.vin[2].dtype.itemsize * mults
      elif u.uop is UOps.WMMA:
        assert u.arg[1] is not None
        flops += 2 * prod(u.arg[1]) // 32 * mults
    return flops, mem

  def type_verify(self):
    for u in self.uops:
      uop, arg, vin, dtype = u.uop, u.arg, u.vin, u.dtype
      if uop in {UOps.CONST, UOps.DEFINE_ACC}:
        if uop is UOps.DEFINE_ACC: arg = arg[0]
        assert dtype is not None and type(arg) is type(dtypes.as_const(arg, dtype)), f"type of {arg=} does not match {dtype}"
      if uop in {UOps.CAST, UOps.BITCAST}: assert arg is None   # type is the output type, not an arg
      if uop is UOps.ALU:
        if arg in UnaryOps:
          assert dtype == vin[0].dtype, f"{arg} dtype mismatch {dtype=} != {vin[0].dtype=}"
        elif arg in (BinaryOps.CMPLT, BinaryOps.CMPEQ):
          assert dtype == dtypes.bool, f"{arg} output dtype mismatch {dtype=} != {dtypes.bool}"
          assert vin[0].dtype == vin[1].dtype, f"{arg} dtype mismatch {dtype=} != {vin[0].dtype=} != {vin[1].dtype=}"
        elif arg in BinaryOps:
          assert dtype == vin[0].dtype == vin[1].dtype, f"{arg} dtype mismatch {dtype=} != {vin[0].dtype=} != {vin[1].dtype=}"
        elif arg == TernaryOps.WHERE:
          assert vin[0].dtype == dtypes.bool, f"{arg} selector dtype mismatch {vin[0].dtype=} != {dtypes.bool}"
          assert dtype == vin[1].dtype == vin[2].dtype, f"{arg} choice dtype mismatch {dtype=} != {vin[1].dtype=} != {vin[2].dtype=}"
