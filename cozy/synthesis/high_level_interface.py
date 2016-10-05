from collections import namedtuple, deque, defaultdict

from cozy.common import typechecked, fresh_name
from cozy.target_syntax import *
from cozy.syntax_tools import all_types, alpha_equivalent, BottomUpExplorer, BottomUpRewriter, free_vars, pprint, subst, implies
from . import core
from . import caching
from cozy.typecheck import INT, BOOL
import cozy.incrementalization as inc

HINTS = True

SynthCtx = namedtuple("SynthCtx", ["all_types", "basic_types"])

def all_exps(e):
    class V(BottomUpExplorer):
        def join(self, x, children):
            for child in children:
                yield from child
            if isinstance(x, Exp):
                yield x
    return V().visit(e)

def fragmentize(exp : Exp, bound_names : {str} = set()):
    so_far = []
    for e in all_exps(exp):
        if isinstance(e, ELambda):
            # lambdas may only appear in certain places---they aren't
            # first-class expressions---so we don't really want to see
            # them in the list of "all expressions"
            continue
        fvs = [fv for fv in free_vars(e) if fv.id not in bound_names]
        remap = { v.id : core.EHole(fresh_name(), v.type, None) for v in fvs }
        e = subst(e, remap)
        if not any(alpha_equivalent(e, root) for root in so_far):
            so_far.append(e)
            yield e

# def constructors(type, roots, basic_types):
#     builder = core.Builder(roots, basic_types, build_sums=False, build_maps=False, build_filters=False)
#     if isinstance(type, TMap):
#         bag_types = set(t for t in basic_types if isinstance(t, TBag)) | set(TBag(t) for t in basic_types)
#         for bag_type in bag_types:
#             for bag_ctor in constructors(bag_type, roots, basic_types):
#                 for key_proj in roots:
#                     # TODO: leave holes in key??
#                     holes = list(core.find_holes(key_proj))
#                     if key_proj.type == type.k and len(holes) == 1 and holes[0].type == bag_type.t:
#                         e = EVar(fresh_name()).with_type(bag_type.t)
#                         es = EVar(fresh_name()).with_type(bag_type)
#                         for vhole in constructors(type.v, roots + [es], basic_types):
#                             map = EMakeMap(
#                                 bag_ctor,
#                                 ELambda(e, subst(key_proj, { holes[0].name : e })),
#                                 ELambda(es, vhole)).with_type(type)
#                             yield map
#         return
#     elif isinstance(type, TTuple):
#         if len(type.ts) == 2:
#             for hole1 in constructors(type.ts[0], roots, basic_types):
#                 for hole2 in constructors(type.ts[1], roots, basic_types):
#                     yield ETuple((hole1, hole2)).with_type(type)
#         else:
#             for hole in constructors(type.ts[0], roots, basic_types):
#                 for rest in constructors(TTuple(type.ts[1:]), roots, basic_types):
#                     yield ETuple((hole,) + rest.es).with_type(type)
#         return
#     elif isinstance(type, TBag):
#         for bag in roots:
#             if isinstance(bag.type, TBag):
#                 m = { h.name : core.EHole(fresh_name(), h.type, builder) for h in core.find_holes(bag) }
#                 bag = subst(bag, m)

#                 src_type = bag.type.t
#                 dst_type = type.t
#                 filt_arg = EVar(fresh_name()).with_type(src_type)
#                 filt_body = core.EHole(fresh_name(), BOOL, builder)
#                 filt = EFilter(bag, ELambda(filt_arg, filt_body)).with_type(TBag(src_type))

#                 if src_type == dst_type:
#                     yield filt
#                 else:
#                     map_arg = EVar(fresh_name()).with_type(src_type)
#                     for proj in roots:
#                         holes = list(core.find_holes(proj))
#                         if proj.type == dst_type and len(holes) == 1 and holes[0].type == src_type:
#                             proj = subst(proj, { holes[0].name : map_arg })
#                             yield EMap(filt, ELambda(map_arg, proj)).with_type(type)
#         return
#     elif isinstance(type, TInt):
#         for bag_of_ints in constructors(TBag(INT), roots, basic_types):
#             yield EUnaryOp("sum", bag_of_ints).with_type(INT)

#     yield core.EHole(fresh_name(), type, builder)

@typechecked
def synthesize_queries(ctx : SynthCtx, state : [EVar], assumptions : [Exp], queries : [Query]) -> (EVar, Exp, [Query]):
    """
    Synthesize efficient re-implementations for the given queries.

    Input:
        ctx     - a synthesis context for the problem
        state   - list of state variables
        queries - a list of queries in the specification

    Output:
        (new_state, state_proj, new_queries)
    where
        new_state is a variable
        state_proj is an expression mapping state to new_state
        new_queries is a list of new query expressions
    """
    assert len(queries) > 0

    res_type = TTuple(tuple(q.ret.type for q in queries)) if len(queries) > 1 else queries[0].ret.type
    all_types = ctx.all_types
    basic_types = ctx.basic_types

    if HINTS:
        state_var_names = set(v.id for v in state)
        state_roots = list(fragmentize(ETuple(tuple(q.ret for q in queries)).with_type(res_type) if len(queries) > 1 else queries[0].ret, bound_names=state_var_names))
    else:
        state_roots = list(state)
        for t in basic_types:
            if isinstance(t, TEnum):
                for case in t.cases:
                    state_roots.append(EEnumEntry(case).with_type(t))
    print("State roots:")
    for r in state_roots:
        print("  --> {}".format(pprint(r)))

    class TopLevelBuilder(core.Builder):
        def __init__(self):
            super().__init__((), basic_types)
            self.args_by_q = { q.name: [EVar(fresh_name(name)).with_type(t) for (name, t) in q.args] for q in queries }
            self.state_var_name = fresh_name("state")
            # self.state_hole_name = fresh_name("state")
        def make_state_hole_core(self, type, builder):
            builder.build_maps = True
            builder.build_tuples = False
            return core.EHole(fresh_name(), type, builder)
        def make_state_hole(self, type, builder=None):
            if builder is None:
                builder = core.Builder(state_roots, basic_types)
            if isinstance(type, TMap):
                for t in all_types:
                    if isinstance(t, TBag) and isinstance(t.t, THandle):
                        bag_type = t
                        for r in state_roots:
                            holes = list(core.find_holes(r))
                            if r.type == type.k and len(holes) == 1 and holes[0].type == bag_type.t:
                                e = EVar(fresh_name()).with_type(bag_type.t)
                                es = EVar(fresh_name()).with_type(bag_type)
                                vhole = core.EHole(fresh_name("xxx"), type.v, builder.with_roots([es], build_maps=True))
                                for bag in self.make_state_hole(bag_type, builder):
                                    yield EMakeMap(
                                        bag,
                                        ELambda(e, subst(r, { holes[0].name : e })),
                                        ELambda(es, vhole)).with_type(type)
            elif isinstance(type, TTuple):
                if len(type.ts) == 2:
                    for hole1 in self.make_state_hole(type.ts[0], builder):
                        for hole2 in self.make_state_hole(type.ts[1], builder):
                            yield ETuple((hole1, hole2)).with_type(type)
                else:
                    for hole in self.make_state_hole(type.ts[0], builder):
                        for rest in self.make_state_hole(TTuple(type.ts[1:]), builder):
                            yield ETuple((hole,) + rest.es).with_type(type)
            else:
                yield self.make_state_hole_core(type, builder)
        def make_query_hints(self, state_type, state_exp):
            yield state_exp
            if isinstance(state_type, TMap):
                e = EMapGet(state_exp, core.EHole(fresh_name(), state_type.k, None)).with_type(state_type.v)
                yield from self.make_query_hints(state_type.v, e)
            elif isinstance(state_type, TTuple):
                for i in range(len(state_type.ts)):
                    e = ETupleGet(state_exp, i).with_type(state_type.ts[i])
                    yield from self.make_query_hints(state_type.ts[i], e)
        def make_query_hole(self, q, state_var):
            args = self.args_by_q[q.name]
            hints = list(self.make_query_hints(state_var.type, state_var))
            for r in list(fragmentize(ETuple(tuple(qq.ret for qq in queries)).with_type(res_type) if len(queries) > 1 else queries[0].ret)):
                hints.append(r)
            # print("hints:")
            # for h in hints:
            #     print("  {}".format(pprint(h)))
            b = core.Builder(args + hints if HINTS else [state_var], basic_types)
            b.build_maps = True
            b.build_tuples = False
            return core.EHole(q.name, q.ret.type, b)
        def build(self, cache, size):
            # TODO: HACK
            cheat = None
            # cheat = TMap(TNative("org.xmpp.packet.JID"), TBag([t for t in basic_types if isinstance(t, THandle)][0]))
            # cheat = TMap(TNative("org.xmpp.packet.JID"), TMaybe([t for t in basic_types if isinstance(t, THandle)][0]))
            # cheat = TMap(TBool(), TBag([t for t in basic_types if isinstance(t, THandle)][0]))
            # cheat = TTuple((TInt(), TInt()))
            # cheat = TTuple((TMap(TInt(), TInt()), TMap(TInt(), TInt())))
            if cheat and size != 1: return
            it = (cheat,) if cheat else self.enum_types(size - 1, allow_tuples=False)
            for state_type in it:
                # if state_type == cheat:
                #     print("now exploring {}".format(pprint(state_type)))
                # print("state ?= {}".format(pprint(state_type)))
                # print(pprint(state_type))
                state_var = EVar(self.state_var_name).with_type(state_type)
                for state_hole in self.make_state_hole(state_type):
                    # print("   --> {}".format(pprint(state_hole)))
                    # print("{} --> {}".format(pprint(state_type), pprint(state_hole)))

                    out = []
                    for q in queries:
                        q_hole = self.make_query_hole(q, state_var)
                        out.append(q_hole)

                    yield EApp(
                        ELambda(state_var, ETuple(tuple(out)) if len(out) > 1 else out[0]),
                        state_hole).with_type(res_type)

    builder = TopLevelBuilder()
    hole = core.EHole(fresh_name(), res_type, builder)
    target = tuple(subst(q.ret, { a1name:a2 for ((a1name, type), a2) in zip(q.args, builder.args_by_q[q.name]) }) for q in queries)
    if len(target) == 1:
        target = target[0]
    else:
        target = ETuple(target)

    assumption = EAll(assumptions)
    spec = implies(assumption, EBinOp(hole, "==", target))
    print(pprint(spec))

    for mapping in core.synth(spec):

        print("SOLUTION")
        expr = core.expand(hole, mapping)
        result = expr.arg
        type = result.type
        print("{} : {} = {}".format(
            builder.state_var_name,
            pprint(type),
            pprint(result)))

        new_queries = []
        for q in queries:
            q_hole = core.EHole(q.name, q.ret.type, None)
            q_result = core.expand(q_hole, mapping)
            print("{} = {}".format(q.name, pprint(q_result)))
            arg_remap = builder.args_by_q[q.name]
            new_queries.append(Query(q.name, [(a.id, a.type) for a in arg_remap], [], q_result))

        return (EVar(builder.state_var_name).with_type(result.type), result, new_queries)

def desugar(e):
    class V(BottomUpRewriter):
        def visit_EListComprehension(self, e):
            res, _, _ = self.visit_clauses(e.clauses, self.visit(e.e))
            return res
        def visit_clauses(self, clauses, final, i=0):
            if i >= len(clauses):
                return final, [], False
            clause = clauses[i]
            if isinstance(clause, CPull):
                bag = self.visit(clause.e)
                arg = EVar(clause.id).with_type(bag.type.t)
                rest, guards, pulls = self.visit_clauses(clauses, final, i + 1)
                if guards:
                    guard = guards[0]
                    for g in guards[1:]:
                        guard = EBinOp(guard, "and", g).with_type(BOOL)
                    bag = EFilter(bag, ELambda(arg, guard)).with_type(bag.type)
                res = EMap(bag, ELambda(arg, rest)).with_type(TBag(rest.type))
                if pulls:
                    res = EFlatten(res)
                return res, [], True
            elif isinstance(clause, CCond):
                rest, guards, pulls = self.visit_clauses(clauses, final, i + 1)
                return rest, guards + [self.visit(clause.e)], pulls
            else:
                raise NotImplementedError(clause)
        def visit_EUnaryOp(self, e):
            sub = self.visit(e.e)
            if e.op == "empty":
                arg = EVar(fresh_name()).with_type(sub.type.t)
                return EBinOp(
                    EUnaryOp("sum", EMap(sub, ELambda(arg, ENum(1).with_type(INT))).with_type(TBag(INT))).with_type(INT),
                    "==",
                    ENum(0).with_type(INT)).with_type(BOOL)
            else:
                return EUnaryOp(e.op, sub).with_type(e.type)
    return V().visit(e)

@typechecked
def synthesize(
        spec      : Spec,
        use_cache : bool = True) -> (Spec, dict):
    """
    Main synthesis routine.

    Returns refined specification with better asymptotic performance, plus a
    dictionary mapping new state variables to their expressions in terms of
    original state variables.
    """

    # gather root types
    types = all_types(spec)
    basic_types = set(t for t in types if not isinstance(t, TBag))
    basic_types |= { TBool(), TInt() }
    print("basic types:")
    for t in basic_types:
        print("  --> {}".format(pprint(t)))
    basic_types = list(basic_types)
    ctx = SynthCtx(all_types=types, basic_types=basic_types)

    # rewrite enums
    enum_types = [t for t in basic_types if isinstance(t, TEnum)]
    repl = {
        name : EEnumEntry(name).with_type(t)
        for t in enum_types
        for name in t.cases }
    spec = subst(spec, repl)

    # collect state variables
    state_vars = [EVar(name).with_type(t) for (name, t) in spec.statevars]

    # collect queries, rewrite list comprehensions
    # qs = [qs[0]]
    # qs = [q for q in spec.methods if isinstance(q, Query) if q.name == "inMemEntries"]
    # qs = [q for q in spec.methods if isinstance(q, Query) if q.name in ("totalMemSize", "totalDiskSize")]
    qs = [Query(q.name, q.args, q.assumptions, desugar(q.ret)) for q in spec.methods if isinstance(q, Query)]
    # assert len(qs) > 0

    worklist = deque(qs)
    new_statevars = []
    state_var_exps = { }
    new_qs = []
    op_stms = defaultdict(list)

    # synthesis
    while worklist:
        q = worklist.popleft()
        print("##### SYNTHESIZING {}".format(q.name))

        cached_result = caching.find_cached_result(state_vars, list(spec.assumptions), q) if use_cache else None
        if cached_result:
            print("##### FOUND CACHED RESULT")
            state_var, state_exp, new_q = cached_result
        else:
            state_var, state_exp, new_q = synthesize_queries(ctx, state_vars, list(spec.assumptions), [q])
            new_q = new_q[0]
            caching.cache((state_vars, list(spec.assumptions), q), (state_var, state_exp, new_q))

        print("  -> {} : {} = {}".format(state_var.id, pprint(state_var.type), pprint(state_exp)))
        print("  -> return {}".format(pprint(new_q.ret)))

        new_statevars.append((state_var.id, state_var.type))
        state_var_exps[state_var.id] = state_exp
        new_qs.append(new_q)

        for op in spec.methods:
            if isinstance(op, Op):
                print("###### INCREMENTALIZING: {}".format(op.name))
                (member, delta) = inc.to_delta(spec.statevars, op)
                print(member, delta)
                (state_update, subqueries) = inc.derivative(state_exp, member, delta, state_vars)
                print(state_update, subqueries)
                state_update_stm = inc.apply_delta_in_place(state_var, state_update)
                print(pprint(state_update_stm))
                op_stms[op.name].append(state_update_stm)
                for sub_q in subqueries:
                    print("########### SUBGOAL: {}".format(pprint(sub_q)))
                    worklist.append(sub_q)

    new_ops = []
    for op in spec.methods:
        if isinstance(op, Op):
            if isinstance(op.body, SAssign) and isinstance(op.body.lhs, EGetField):
                op_stms[op.name].append(op.body)
            new_stms = seq(op_stms[op.name])
            new_ops.append(Op(
                op.name,
                op.args,
                [],
                new_stms))

    return (Spec(
        spec.name,
        spec.types,
        spec.extern_funcs,
        new_statevars,
        [],
        new_ops + new_qs), state_var_exps)

    raise NotImplementedError()