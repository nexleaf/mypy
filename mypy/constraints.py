"""Type inference constraints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, List, Sequence
from typing_extensions import Final

import mypy.subtypes
import mypy.typeops
from mypy.argmap import ArgTypeExpander
from mypy.erasetype import erase_typevars
from mypy.maptype import map_instance_to_supertype
from mypy.nodes import ARG_OPT, ARG_POS, CONTRAVARIANT, COVARIANT, ArgKind
from mypy.types import (
    TUPLE_LIKE_INSTANCE_NAMES,
    AnyType,
    CallableType,
    DeletedType,
    ErasedType,
    Instance,
    LiteralType,
    NoneType,
    Overloaded,
    Parameters,
    ParamSpecType,
    PartialType,
    ProperType,
    TupleType,
    Type,
    TypeAliasType,
    TypedDictType,
    TypeList,
    TypeOfAny,
    TypeQuery,
    TypeType,
    TypeVarId,
    TypeVarLikeType,
    TypeVarTupleType,
    TypeVarType,
    TypeVisitor,
    UnboundType,
    UninhabitedType,
    UnionType,
    UnpackType,
    callable_with_ellipsis,
    get_proper_type,
    has_recursive_types,
    has_type_vars,
    is_named_instance,
    is_union_with_any,
)
from mypy.typestate import TypeState
from mypy.typevartuples import (
    extract_unpack,
    find_unpack_in_list,
    split_with_instance,
    split_with_prefix_and_suffix,
)

if TYPE_CHECKING:
    from mypy.infer import ArgumentInferContext

SUBTYPE_OF: Final = 0
SUPERTYPE_OF: Final = 1


class Constraint:
    """A representation of a type constraint.

    It can be either T <: type or T :> type (T is a type variable).
    """

    type_var: TypeVarId
    op = 0  # SUBTYPE_OF or SUPERTYPE_OF
    target: Type

    def __init__(self, type_var: TypeVarLikeType, op: int, target: Type) -> None:
        self.type_var = type_var.id
        self.op = op
        self.target = target
        self.origin_type_var = type_var

    def __repr__(self) -> str:
        op_str = "<:"
        if self.op == SUPERTYPE_OF:
            op_str = ":>"
        return f"{self.type_var} {op_str} {self.target}"

    def __hash__(self) -> int:
        return hash((self.type_var, self.op, self.target))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Constraint):
            return False
        return (self.type_var, self.op, self.target) == (other.type_var, other.op, other.target)


def infer_constraints_for_callable(
    callee: CallableType,
    arg_types: Sequence[Type | None],
    arg_kinds: list[ArgKind],
    formal_to_actual: list[list[int]],
    context: ArgumentInferContext,
) -> list[Constraint]:
    """Infer type variable constraints for a callable and actual arguments.

    Return a list of constraints.
    """
    constraints: list[Constraint] = []
    mapper = ArgTypeExpander(context)

    for i, actuals in enumerate(formal_to_actual):
        for actual in actuals:
            actual_arg_type = arg_types[actual]
            if actual_arg_type is None:
                continue

            actual_type = mapper.expand_actual_type(
                actual_arg_type, arg_kinds[actual], callee.arg_names[i], callee.arg_kinds[i]
            )
            c = infer_constraints(callee.arg_types[i], actual_type, SUPERTYPE_OF)
            constraints.extend(c)

    return constraints


def infer_constraints(template: Type, actual: Type, direction: int) -> list[Constraint]:
    """Infer type constraints.

    Match a template type, which may contain type variable references,
    recursively against a type which does not contain (the same) type
    variable references. The result is a list of type constrains of
    form 'T is a supertype/subtype of x', where T is a type variable
    present in the template and x is a type without reference to type
    variables present in the template.

    Assume T and S are type variables. Now the following results can be
    calculated (read as '(template, actual) --> result'):

      (T, X)            -->  T :> X
      (X[T], X[Y])      -->  T <: Y and T :> Y
      ((T, T), (X, Y))  -->  T :> X and T :> Y
      ((T, S), (X, Y))  -->  T :> X and S :> Y
      (X[T], Any)       -->  T <: Any and T :> Any

    The constraints are represented as Constraint objects.
    """
    if any(
        get_proper_type(template) == get_proper_type(t)
        and get_proper_type(actual) == get_proper_type(a)
        for (t, a) in reversed(TypeState.inferring)
    ):
        return []
    if has_recursive_types(template):
        # This case requires special care because it may cause infinite recursion.
        if not has_type_vars(template):
            # Return early on an empty branch.
            return []
        TypeState.inferring.append((template, actual))
        res = _infer_constraints(template, actual, direction)
        TypeState.inferring.pop()
        return res
    return _infer_constraints(template, actual, direction)


def _infer_constraints(template: Type, actual: Type, direction: int) -> list[Constraint]:

    orig_template = template
    template = get_proper_type(template)
    actual = get_proper_type(actual)

    # Type inference shouldn't be affected by whether union types have been simplified.
    # We however keep any ErasedType items, so that the caller will see it when using
    # checkexpr.has_erased_component().
    if isinstance(template, UnionType):
        template = mypy.typeops.make_simplified_union(template.items, keep_erased=True)
    if isinstance(actual, UnionType):
        actual = mypy.typeops.make_simplified_union(actual.items, keep_erased=True)

    # Ignore Any types from the type suggestion engine to avoid them
    # causing us to infer Any in situations where a better job could
    # be done otherwise. (This can produce false positives but that
    # doesn't really matter because it is all heuristic anyway.)
    if isinstance(actual, AnyType) and actual.type_of_any == TypeOfAny.suggestion_engine:
        return []

    # If the template is simply a type variable, emit a Constraint directly.
    # We need to handle this case before handling Unions for two reasons:
    #  1. "T <: Union[U1, U2]" is not equivalent to "T <: U1 or T <: U2",
    #     because T can itself be a union (notably, Union[U1, U2] itself).
    #  2. "T :> Union[U1, U2]" is logically equivalent to "T :> U1 and
    #     T :> U2", but they are not equivalent to the constraint solver,
    #     which never introduces new Union types (it uses join() instead).
    if isinstance(template, TypeVarType):
        return [Constraint(template, direction, actual)]

    # Now handle the case of either template or actual being a Union.
    # For a Union to be a subtype of another type, every item of the Union
    # must be a subtype of that type, so concatenate the constraints.
    if direction == SUBTYPE_OF and isinstance(template, UnionType):
        res = []
        for t_item in template.items:
            res.extend(infer_constraints(t_item, actual, direction))
        return res
    if direction == SUPERTYPE_OF and isinstance(actual, UnionType):
        res = []
        for a_item in actual.items:
            res.extend(infer_constraints(orig_template, a_item, direction))
        return res

    # Now the potential subtype is known not to be a Union or a type
    # variable that we are solving for. In that case, for a Union to
    # be a supertype of the potential subtype, some item of the Union
    # must be a supertype of it.
    if direction == SUBTYPE_OF and isinstance(actual, UnionType):
        # If some of items is not a complete type, disregard that.
        items = simplify_away_incomplete_types(actual.items)
        # We infer constraints eagerly -- try to find constraints for a type
        # variable if possible. This seems to help with some real-world
        # use cases.
        return any_constraints(
            [infer_constraints_if_possible(template, a_item, direction) for a_item in items],
            eager=True,
        )
    if direction == SUPERTYPE_OF and isinstance(template, UnionType):
        # When the template is a union, we are okay with leaving some
        # type variables indeterminate. This helps with some special
        # cases, though this isn't very principled.
        result = any_constraints(
            [
                infer_constraints_if_possible(t_item, actual, direction)
                for t_item in template.items
            ],
            eager=False,
        )
        if result:
            return result
        elif has_recursive_types(template) and not has_recursive_types(actual):
            return handle_recursive_union(template, actual, direction)
        return []

    # Remaining cases are handled by ConstraintBuilderVisitor.
    return template.accept(ConstraintBuilderVisitor(actual, direction))


def infer_constraints_if_possible(
    template: Type, actual: Type, direction: int
) -> list[Constraint] | None:
    """Like infer_constraints, but return None if the input relation is
    known to be unsatisfiable, for example if template=List[T] and actual=int.
    (In this case infer_constraints would return [], just like it would for
    an automatically satisfied relation like template=List[T] and actual=object.)
    """
    if direction == SUBTYPE_OF and not mypy.subtypes.is_subtype(erase_typevars(template), actual):
        return None
    if direction == SUPERTYPE_OF and not mypy.subtypes.is_subtype(
        actual, erase_typevars(template)
    ):
        return None
    if (
        direction == SUPERTYPE_OF
        and isinstance(template, TypeVarType)
        and not mypy.subtypes.is_subtype(actual, erase_typevars(template.upper_bound))
    ):
        # This is not caught by the above branch because of the erase_typevars() call,
        # that would return 'Any' for a type variable.
        return None
    return infer_constraints(template, actual, direction)


def select_trivial(options: Sequence[list[Constraint] | None]) -> list[list[Constraint]]:
    """Select only those lists where each item is a constraint against Any."""
    res = []
    for option in options:
        if option is None:
            continue
        if all(isinstance(get_proper_type(c.target), AnyType) for c in option):
            res.append(option)
    return res


def merge_with_any(constraint: Constraint) -> Constraint:
    """Transform a constraint target into a union with given Any type."""
    target = constraint.target
    if is_union_with_any(target):
        # Do not produce redundant unions.
        return constraint
    # TODO: if we will support multiple sources Any, use this here instead.
    any_type = AnyType(TypeOfAny.implementation_artifact)
    return Constraint(
        constraint.origin_type_var,
        constraint.op,
        UnionType.make_union([target, any_type], target.line, target.column),
    )


def handle_recursive_union(template: UnionType, actual: Type, direction: int) -> list[Constraint]:
    # This is a hack to special-case things like Union[T, Inst[T]] in recursive types. Although
    # it is quite arbitrary, it is a relatively common pattern, so we should handle it well.
    # This function may be called when inferring against such union resulted in different
    # constraints for each item. Normally we give up in such case, but here we instead split
    # the union in two parts, and try inferring sequentially.
    non_type_var_items = [t for t in template.items if not isinstance(t, TypeVarType)]
    type_var_items = [t for t in template.items if isinstance(t, TypeVarType)]
    return infer_constraints(
        UnionType.make_union(non_type_var_items), actual, direction
    ) or infer_constraints(UnionType.make_union(type_var_items), actual, direction)


def any_constraints(options: list[list[Constraint] | None], eager: bool) -> list[Constraint]:
    """Deduce what we can from a collection of constraint lists.

    It's a given that at least one of the lists must be satisfied. A
    None element in the list of options represents an unsatisfiable
    constraint and is ignored.  Ignore empty constraint lists if eager
    is true -- they are always trivially satisfiable.
    """
    if eager:
        valid_options = [option for option in options if option]
    else:
        valid_options = [option for option in options if option is not None]

    if not valid_options:
        return []

    if len(valid_options) == 1:
        return valid_options[0]

    if all(is_same_constraints(valid_options[0], c) for c in valid_options[1:]):
        # Multiple sets of constraints that are all the same. Just pick any one of them.
        return valid_options[0]

    if all(is_similar_constraints(valid_options[0], c) for c in valid_options[1:]):
        # All options have same structure. In this case we can merge-in trivial
        # options (i.e. those that only have Any) and try again.
        # TODO: More generally, if a given (variable, direction) pair appears in
        # every option, combine the bounds with meet/join always, not just for Any.
        trivial_options = select_trivial(valid_options)
        if trivial_options and len(trivial_options) < len(valid_options):
            merged_options = []
            for option in valid_options:
                if option in trivial_options:
                    continue
                if option is not None:
                    merged_option: list[Constraint] | None = [merge_with_any(c) for c in option]
                else:
                    merged_option = None
                merged_options.append(merged_option)
            return any_constraints(list(merged_options), eager)

    # If normal logic didn't work, try excluding trivially unsatisfiable constraint (due to
    # upper bounds) from each option, and comparing them again.
    filtered_options = [filter_satisfiable(o) for o in options]
    if filtered_options != options:
        return any_constraints(filtered_options, eager=eager)

    # Otherwise, there are either no valid options or multiple, inconsistent valid
    # options. Give up and deduce nothing.
    return []


def filter_satisfiable(option: list[Constraint] | None) -> list[Constraint] | None:
    """Keep only constraints that can possibly be satisfied.

    Currently, we filter out constraints where target is not a subtype of the upper bound.
    Since those can be never satisfied. We may add more cases in future if it improves type
    inference.
    """
    if not option:
        return option
    satisfiable = []
    for c in option:
        # TODO: add similar logic for TypeVar values (also in various other places)?
        if mypy.subtypes.is_subtype(c.target, c.origin_type_var.upper_bound):
            satisfiable.append(c)
    if not satisfiable:
        return None
    return satisfiable


def is_same_constraints(x: list[Constraint], y: list[Constraint]) -> bool:
    for c1 in x:
        if not any(is_same_constraint(c1, c2) for c2 in y):
            return False
    for c1 in y:
        if not any(is_same_constraint(c1, c2) for c2 in x):
            return False
    return True


def is_same_constraint(c1: Constraint, c2: Constraint) -> bool:
    # Ignore direction when comparing constraints against Any.
    skip_op_check = isinstance(get_proper_type(c1.target), AnyType) and isinstance(
        get_proper_type(c2.target), AnyType
    )
    return (
        c1.type_var == c2.type_var
        and (c1.op == c2.op or skip_op_check)
        and mypy.subtypes.is_same_type(c1.target, c2.target)
    )


def is_similar_constraints(x: list[Constraint], y: list[Constraint]) -> bool:
    """Check that two lists of constraints have similar structure.

    This means that each list has same type variable plus direction pairs (i.e we
    ignore the target). Except for constraints where target is Any type, there
    we ignore direction as well.
    """
    return _is_similar_constraints(x, y) and _is_similar_constraints(y, x)


def _is_similar_constraints(x: list[Constraint], y: list[Constraint]) -> bool:
    """Check that every constraint in the first list has a similar one in the second.

    See docstring above for definition of similarity.
    """
    for c1 in x:
        has_similar = False
        for c2 in y:
            # Ignore direction when either constraint is against Any.
            skip_op_check = isinstance(get_proper_type(c1.target), AnyType) or isinstance(
                get_proper_type(c2.target), AnyType
            )
            if c1.type_var == c2.type_var and (c1.op == c2.op or skip_op_check):
                has_similar = True
                break
        if not has_similar:
            return False
    return True


def simplify_away_incomplete_types(types: Iterable[Type]) -> list[Type]:
    complete = [typ for typ in types if is_complete_type(typ)]
    if complete:
        return complete
    else:
        return list(types)


def is_complete_type(typ: Type) -> bool:
    """Is a type complete?

    A complete doesn't have uninhabited type components or (when not in strict
    optional mode) None components.
    """
    return typ.accept(CompleteTypeVisitor())


class CompleteTypeVisitor(TypeQuery[bool]):
    def __init__(self) -> None:
        super().__init__(all)

    def visit_uninhabited_type(self, t: UninhabitedType) -> bool:
        return False


class ConstraintBuilderVisitor(TypeVisitor[List[Constraint]]):
    """Visitor class for inferring type constraints."""

    # The type that is compared against a template
    # TODO: The value may be None. Is that actually correct?
    actual: ProperType

    def __init__(self, actual: ProperType, direction: int) -> None:
        # Direction must be SUBTYPE_OF or SUPERTYPE_OF.
        self.actual = actual
        self.direction = direction

    # Trivial leaf types

    def visit_unbound_type(self, template: UnboundType) -> list[Constraint]:
        return []

    def visit_any(self, template: AnyType) -> list[Constraint]:
        return []

    def visit_none_type(self, template: NoneType) -> list[Constraint]:
        return []

    def visit_uninhabited_type(self, template: UninhabitedType) -> list[Constraint]:
        return []

    def visit_erased_type(self, template: ErasedType) -> list[Constraint]:
        return []

    def visit_deleted_type(self, template: DeletedType) -> list[Constraint]:
        return []

    def visit_literal_type(self, template: LiteralType) -> list[Constraint]:
        return []

    # Errors

    def visit_partial_type(self, template: PartialType) -> list[Constraint]:
        # We can't do anything useful with a partial type here.
        assert False, "Internal error"

    # Non-trivial leaf type

    def visit_type_var(self, template: TypeVarType) -> list[Constraint]:
        assert False, (
            "Unexpected TypeVarType in ConstraintBuilderVisitor"
            " (should have been handled in infer_constraints)"
        )

    def visit_param_spec(self, template: ParamSpecType) -> list[Constraint]:
        # Can't infer ParamSpecs from component values (only via Callable[P, T]).
        return []

    def visit_type_var_tuple(self, template: TypeVarTupleType) -> list[Constraint]:
        raise NotImplementedError

    def visit_unpack_type(self, template: UnpackType) -> list[Constraint]:
        raise NotImplementedError

    def visit_parameters(self, template: Parameters) -> list[Constraint]:
        # constraining Any against C[P] turns into infer_against_any([P], Any)
        # ... which seems like the only case this can happen. Better to fail loudly.
        if isinstance(self.actual, AnyType):
            return self.infer_against_any(template.arg_types, self.actual)
        raise RuntimeError("Parameters cannot be constrained to")

    # Non-leaf types

    def visit_instance(self, template: Instance) -> list[Constraint]:
        original_actual = actual = self.actual
        res: list[Constraint] = []
        if isinstance(actual, (CallableType, Overloaded)) and template.type.is_protocol:
            if template.type.protocol_members == ["__call__"]:
                # Special case: a generic callback protocol
                if not any(template == t for t in template.type.inferring):
                    template.type.inferring.append(template)
                    call = mypy.subtypes.find_member(
                        "__call__", template, actual, is_operator=True
                    )
                    assert call is not None
                    if mypy.subtypes.is_subtype(actual, erase_typevars(call)):
                        subres = infer_constraints(call, actual, self.direction)
                        res.extend(subres)
                    template.type.inferring.pop()
                    return res
        if isinstance(actual, CallableType) and actual.fallback is not None:
            if actual.is_type_obj() and template.type.is_protocol:
                ret_type = get_proper_type(actual.ret_type)
                if isinstance(ret_type, TupleType):
                    ret_type = mypy.typeops.tuple_fallback(ret_type)
                if isinstance(ret_type, Instance):
                    if self.direction == SUBTYPE_OF:
                        subtype = template
                    else:
                        subtype = ret_type
                    res.extend(
                        self.infer_constraints_from_protocol_members(
                            ret_type, template, subtype, template, class_obj=True
                        )
                    )
            actual = actual.fallback
        if isinstance(actual, TypeType) and template.type.is_protocol:
            if isinstance(actual.item, Instance):
                if self.direction == SUBTYPE_OF:
                    subtype = template
                else:
                    subtype = actual.item
                res.extend(
                    self.infer_constraints_from_protocol_members(
                        actual.item, template, subtype, template, class_obj=True
                    )
                )

        if isinstance(actual, Overloaded) and actual.fallback is not None:
            actual = actual.fallback
        if isinstance(actual, TypedDictType):
            actual = actual.as_anonymous().fallback
        if isinstance(actual, LiteralType):
            actual = actual.fallback
        if isinstance(actual, Instance):
            instance = actual
            erased = erase_typevars(template)
            assert isinstance(erased, Instance)  # type: ignore[misc]
            # We always try nominal inference if possible,
            # it is much faster than the structural one.
            if self.direction == SUBTYPE_OF and template.type.has_base(instance.type.fullname):
                mapped = map_instance_to_supertype(template, instance.type)
                tvars = mapped.type.defn.type_vars
                # N.B: We use zip instead of indexing because the lengths might have
                # mismatches during daemon reprocessing.
                for tvar, mapped_arg, instance_arg in zip(tvars, mapped.args, instance.args):
                    # TODO(PEP612): More ParamSpec work (or is Parameters the only thing accepted)
                    if isinstance(tvar, TypeVarType):
                        # The constraints for generic type parameters depend on variance.
                        # Include constraints from both directions if invariant.
                        if tvar.variance != CONTRAVARIANT:
                            res.extend(infer_constraints(mapped_arg, instance_arg, self.direction))
                        if tvar.variance != COVARIANT:
                            res.extend(
                                infer_constraints(mapped_arg, instance_arg, neg_op(self.direction))
                            )
                    elif isinstance(tvar, ParamSpecType) and isinstance(mapped_arg, ParamSpecType):
                        suffix = get_proper_type(instance_arg)

                        if isinstance(suffix, CallableType):
                            prefix = mapped_arg.prefix
                            from_concat = bool(prefix.arg_types) or suffix.from_concatenate
                            suffix = suffix.copy_modified(from_concatenate=from_concat)

                        if isinstance(suffix, Parameters) or isinstance(suffix, CallableType):
                            # no such thing as variance for ParamSpecs
                            # TODO: is there a case I am missing?
                            # TODO: constraints between prefixes
                            prefix = mapped_arg.prefix
                            suffix = suffix.copy_modified(
                                suffix.arg_types[len(prefix.arg_types) :],
                                suffix.arg_kinds[len(prefix.arg_kinds) :],
                                suffix.arg_names[len(prefix.arg_names) :],
                            )
                            res.append(Constraint(mapped_arg, SUPERTYPE_OF, suffix))
                        elif isinstance(suffix, ParamSpecType):
                            res.append(Constraint(mapped_arg, SUPERTYPE_OF, suffix))
                    elif isinstance(tvar, TypeVarTupleType):
                        raise NotImplementedError

                return res
            elif self.direction == SUPERTYPE_OF and instance.type.has_base(template.type.fullname):
                mapped = map_instance_to_supertype(instance, template.type)
                tvars = template.type.defn.type_vars
                if template.type.has_type_var_tuple_type:
                    mapped_prefix, mapped_middle, mapped_suffix = split_with_instance(mapped)
                    template_prefix, template_middle, template_suffix = split_with_instance(
                        template
                    )

                    # Add a constraint for the type var tuple, and then
                    # remove it for the case below.
                    template_unpack = extract_unpack(template_middle)
                    if template_unpack is not None:
                        if isinstance(template_unpack, TypeVarTupleType):
                            res.append(
                                Constraint(
                                    template_unpack, SUPERTYPE_OF, TypeList(list(mapped_middle))
                                )
                            )
                        elif (
                            isinstance(template_unpack, Instance)
                            and template_unpack.type.fullname == "builtins.tuple"
                        ):
                            for item in mapped_middle:
                                res.extend(
                                    infer_constraints(
                                        template_unpack.args[0], item, self.direction
                                    )
                                )
                        elif isinstance(template_unpack, TupleType):
                            if len(template_unpack.items) == len(mapped_middle):
                                for template_arg, item in zip(
                                    template_unpack.items, mapped_middle
                                ):
                                    res.extend(
                                        infer_constraints(template_arg, item, self.direction)
                                    )

                    mapped_args = mapped_prefix + mapped_suffix
                    template_args = template_prefix + template_suffix

                    assert template.type.type_var_tuple_prefix is not None
                    assert template.type.type_var_tuple_suffix is not None
                    tvars_prefix, _, tvars_suffix = split_with_prefix_and_suffix(
                        tuple(tvars),
                        template.type.type_var_tuple_prefix,
                        template.type.type_var_tuple_suffix,
                    )
                    tvars = list(tvars_prefix + tvars_suffix)
                else:
                    mapped_args = mapped.args
                    template_args = template.args
                # N.B: We use zip instead of indexing because the lengths might have
                # mismatches during daemon reprocessing.
                for tvar, mapped_arg, template_arg in zip(tvars, mapped_args, template_args):
                    assert not isinstance(tvar, TypeVarTupleType)
                    if isinstance(tvar, TypeVarType):
                        # The constraints for generic type parameters depend on variance.
                        # Include constraints from both directions if invariant.
                        if tvar.variance != CONTRAVARIANT:
                            res.extend(infer_constraints(template_arg, mapped_arg, self.direction))
                        if tvar.variance != COVARIANT:
                            res.extend(
                                infer_constraints(template_arg, mapped_arg, neg_op(self.direction))
                            )
                    elif isinstance(tvar, ParamSpecType) and isinstance(
                        template_arg, ParamSpecType
                    ):
                        suffix = get_proper_type(mapped_arg)

                        if isinstance(suffix, CallableType):
                            prefix = template_arg.prefix
                            from_concat = bool(prefix.arg_types) or suffix.from_concatenate
                            suffix = suffix.copy_modified(from_concatenate=from_concat)

                        if isinstance(suffix, Parameters) or isinstance(suffix, CallableType):
                            # no such thing as variance for ParamSpecs
                            # TODO: is there a case I am missing?
                            # TODO: constraints between prefixes
                            prefix = template_arg.prefix

                            suffix = suffix.copy_modified(
                                suffix.arg_types[len(prefix.arg_types) :],
                                suffix.arg_kinds[len(prefix.arg_kinds) :],
                                suffix.arg_names[len(prefix.arg_names) :],
                            )
                            res.append(Constraint(template_arg, SUPERTYPE_OF, suffix))
                        elif isinstance(suffix, ParamSpecType):
                            res.append(Constraint(template_arg, SUPERTYPE_OF, suffix))
                return res
            if (
                template.type.is_protocol
                and self.direction == SUPERTYPE_OF
                and
                # We avoid infinite recursion for structural subtypes by checking
                # whether this type already appeared in the inference chain.
                # This is a conservative way to break the inference cycles.
                # It never produces any "false" constraints but gives up soon
                # on purely structural inference cycles, see #3829.
                # Note that we use is_protocol_implementation instead of is_subtype
                # because some type may be considered a subtype of a protocol
                # due to _promote, but still not implement the protocol.
                not any(template == t for t in reversed(template.type.inferring))
                and mypy.subtypes.is_protocol_implementation(instance, erased)
            ):
                template.type.inferring.append(template)
                res.extend(
                    self.infer_constraints_from_protocol_members(
                        instance, template, original_actual, template
                    )
                )
                template.type.inferring.pop()
                return res
            elif (
                instance.type.is_protocol
                and self.direction == SUBTYPE_OF
                and
                # We avoid infinite recursion for structural subtypes also here.
                not any(instance == i for i in reversed(instance.type.inferring))
                and mypy.subtypes.is_protocol_implementation(erased, instance)
            ):
                instance.type.inferring.append(instance)
                res.extend(
                    self.infer_constraints_from_protocol_members(
                        instance, template, template, instance
                    )
                )
                instance.type.inferring.pop()
                return res
        if res:
            return res

        if isinstance(actual, AnyType):
            return self.infer_against_any(template.args, actual)
        if (
            isinstance(actual, TupleType)
            and is_named_instance(template, TUPLE_LIKE_INSTANCE_NAMES)
            and self.direction == SUPERTYPE_OF
        ):
            for item in actual.items:
                cb = infer_constraints(template.args[0], item, SUPERTYPE_OF)
                res.extend(cb)
            return res
        elif isinstance(actual, TupleType) and self.direction == SUPERTYPE_OF:
            return infer_constraints(template, mypy.typeops.tuple_fallback(actual), self.direction)
        elif isinstance(actual, TypeVarType):
            if not actual.values:
                return infer_constraints(template, actual.upper_bound, self.direction)
            return []
        elif isinstance(actual, ParamSpecType):
            return infer_constraints(template, actual.upper_bound, self.direction)
        elif isinstance(actual, TypeVarTupleType):
            raise NotImplementedError
        else:
            return []

    def infer_constraints_from_protocol_members(
        self,
        instance: Instance,
        template: Instance,
        subtype: Type,
        protocol: Instance,
        class_obj: bool = False,
    ) -> list[Constraint]:
        """Infer constraints for situations where either 'template' or 'instance' is a protocol.

        The 'protocol' is the one of two that is an instance of protocol type, 'subtype'
        is the type used to bind self during inference. Currently, we just infer constrains for
        every protocol member type (both ways for settable members).
        """
        res = []
        for member in protocol.type.protocol_members:
            inst = mypy.subtypes.find_member(member, instance, subtype, class_obj=class_obj)
            temp = mypy.subtypes.find_member(member, template, subtype)
            if inst is None or temp is None:
                return []  # See #11020
            # The above is safe since at this point we know that 'instance' is a subtype
            # of (erased) 'template', therefore it defines all protocol members
            res.extend(infer_constraints(temp, inst, self.direction))
            if mypy.subtypes.IS_SETTABLE in mypy.subtypes.get_member_flags(member, protocol):
                # Settable members are invariant, add opposite constraints
                res.extend(infer_constraints(temp, inst, neg_op(self.direction)))
        return res

    def visit_callable_type(self, template: CallableType) -> list[Constraint]:
        # Normalize callables before matching against each other.
        # Note that non-normalized callables can be created in annotations
        # using e.g. callback protocols.
        template = template.with_unpacked_kwargs()
        if isinstance(self.actual, CallableType):
            res: list[Constraint] = []
            cactual = self.actual.with_unpacked_kwargs()
            param_spec = template.param_spec()
            if param_spec is None:
                # FIX verify argument counts
                # FIX what if one of the functions is generic

                # We can't infer constraints from arguments if the template is Callable[..., T]
                # (with literal '...').
                if not template.is_ellipsis_args:
                    # The lengths should match, but don't crash (it will error elsewhere).
                    for t, a in zip(template.arg_types, cactual.arg_types):
                        # Negate direction due to function argument type contravariance.
                        res.extend(infer_constraints(t, a, neg_op(self.direction)))
            else:
                # sometimes, it appears we try to get constraints between two paramspec callables?
                # TODO: Direction
                # TODO: check the prefixes match
                prefix = param_spec.prefix
                prefix_len = len(prefix.arg_types)
                cactual_ps = cactual.param_spec()

                if not cactual_ps:
                    max_prefix_len = len([k for k in cactual.arg_kinds if k in (ARG_POS, ARG_OPT)])
                    prefix_len = min(prefix_len, max_prefix_len)
                    res.append(
                        Constraint(
                            param_spec,
                            SUBTYPE_OF,
                            cactual.copy_modified(
                                arg_types=cactual.arg_types[prefix_len:],
                                arg_kinds=cactual.arg_kinds[prefix_len:],
                                arg_names=cactual.arg_names[prefix_len:],
                                ret_type=NoneType(),
                            ),
                        )
                    )
                else:
                    res.append(Constraint(param_spec, SUBTYPE_OF, cactual_ps))

                # compare prefixes
                cactual_prefix = cactual.copy_modified(
                    arg_types=cactual.arg_types[:prefix_len],
                    arg_kinds=cactual.arg_kinds[:prefix_len],
                    arg_names=cactual.arg_names[:prefix_len],
                )

                # TODO: see above "FIX" comments for param_spec is None case
                # TODO: this assume positional arguments
                for t, a in zip(prefix.arg_types, cactual_prefix.arg_types):
                    res.extend(infer_constraints(t, a, neg_op(self.direction)))

            template_ret_type, cactual_ret_type = template.ret_type, cactual.ret_type
            if template.type_guard is not None:
                template_ret_type = template.type_guard
            if cactual.type_guard is not None:
                cactual_ret_type = cactual.type_guard

            res.extend(infer_constraints(template_ret_type, cactual_ret_type, self.direction))
            return res
        elif isinstance(self.actual, AnyType):
            param_spec = template.param_spec()
            any_type = AnyType(TypeOfAny.from_another_any, source_any=self.actual)
            if param_spec is None:
                # FIX what if generic
                res = self.infer_against_any(template.arg_types, self.actual)
            else:
                res = [
                    Constraint(
                        param_spec,
                        SUBTYPE_OF,
                        callable_with_ellipsis(any_type, any_type, template.fallback),
                    )
                ]
            res.extend(infer_constraints(template.ret_type, any_type, self.direction))
            return res
        elif isinstance(self.actual, Overloaded):
            return self.infer_against_overloaded(self.actual, template)
        elif isinstance(self.actual, TypeType):
            return infer_constraints(template.ret_type, self.actual.item, self.direction)
        elif isinstance(self.actual, Instance):
            # Instances with __call__ method defined are considered structural
            # subtypes of Callable with a compatible signature.
            call = mypy.subtypes.find_member(
                "__call__", self.actual, self.actual, is_operator=True
            )
            if call:
                return infer_constraints(template, call, self.direction)
            else:
                return []
        else:
            return []

    def infer_against_overloaded(
        self, overloaded: Overloaded, template: CallableType
    ) -> list[Constraint]:
        # Create constraints by matching an overloaded type against a template.
        # This is tricky to do in general. We cheat by only matching against
        # the first overload item that is callable compatible. This
        # seems to work somewhat well, but we should really use a more
        # reliable technique.
        item = find_matching_overload_item(overloaded, template)
        return infer_constraints(template, item, self.direction)

    def visit_tuple_type(self, template: TupleType) -> list[Constraint]:
        actual = self.actual
        # TODO: Support subclasses of Tuple
        is_varlength_tuple = (
            isinstance(actual, Instance) and actual.type.fullname == "builtins.tuple"
        )
        unpack_index = find_unpack_in_list(template.items)

        if unpack_index is not None:
            unpack_item = get_proper_type(template.items[unpack_index])
            assert isinstance(unpack_item, UnpackType)

            unpacked_type = get_proper_type(unpack_item.type)
            if isinstance(unpacked_type, TypeVarTupleType):
                if is_varlength_tuple:
                    # This case is only valid when the unpack is the only
                    # item in the tuple.
                    #
                    # TODO: We should support this in the case that all the items
                    # in the tuple besides the unpack have the same type as the
                    # varlength tuple's type. E.g. Tuple[int, ...] should be valid
                    # where we expect Tuple[int, Unpack[Ts]], but not for Tuple[str, Unpack[Ts]].
                    assert len(template.items) == 1

                if isinstance(actual, (TupleType, AnyType)) or is_varlength_tuple:
                    modified_actual = actual
                    if isinstance(actual, TupleType):
                        # Exclude the items from before and after the unpack index.
                        # TODO: Support including constraints from the prefix/suffix.
                        _, actual_items, _ = split_with_prefix_and_suffix(
                            tuple(actual.items),
                            unpack_index,
                            len(template.items) - unpack_index - 1,
                        )
                        modified_actual = actual.copy_modified(items=list(actual_items))
                    return [
                        Constraint(
                            type_var=unpacked_type, op=self.direction, target=modified_actual
                        )
                    ]

        if isinstance(actual, TupleType) and len(actual.items) == len(template.items):
            if (
                actual.partial_fallback.type.is_named_tuple
                and template.partial_fallback.type.is_named_tuple
            ):
                # For named tuples using just the fallbacks usually gives better results.
                return infer_constraints(
                    template.partial_fallback, actual.partial_fallback, self.direction
                )
            res: list[Constraint] = []
            for i in range(len(template.items)):
                res.extend(infer_constraints(template.items[i], actual.items[i], self.direction))
            return res
        elif isinstance(actual, AnyType):
            return self.infer_against_any(template.items, actual)
        else:
            return []

    def visit_typeddict_type(self, template: TypedDictType) -> list[Constraint]:
        actual = self.actual
        if isinstance(actual, TypedDictType):
            res: list[Constraint] = []
            # NOTE: Non-matching keys are ignored. Compatibility is checked
            #       elsewhere so this shouldn't be unsafe.
            for (item_name, template_item_type, actual_item_type) in template.zip(actual):
                res.extend(infer_constraints(template_item_type, actual_item_type, self.direction))
            return res
        elif isinstance(actual, AnyType):
            return self.infer_against_any(template.items.values(), actual)
        else:
            return []

    def visit_union_type(self, template: UnionType) -> list[Constraint]:
        assert False, (
            "Unexpected UnionType in ConstraintBuilderVisitor"
            " (should have been handled in infer_constraints)"
        )

    def visit_type_alias_type(self, template: TypeAliasType) -> list[Constraint]:
        assert False, f"This should be never called, got {template}"

    def infer_against_any(self, types: Iterable[Type], any_type: AnyType) -> list[Constraint]:
        res: list[Constraint] = []
        for t in types:
            # Note that we ignore variance and simply always use the
            # original direction. This is because for Any targets direction is
            # irrelevant in most cases, see e.g. is_same_constraint().
            res.extend(infer_constraints(t, any_type, self.direction))
        return res

    def visit_overloaded(self, template: Overloaded) -> list[Constraint]:
        if isinstance(self.actual, CallableType):
            items = find_matching_overload_items(template, self.actual)
        else:
            items = template.items
        res: list[Constraint] = []
        for t in items:
            res.extend(infer_constraints(t, self.actual, self.direction))
        return res

    def visit_type_type(self, template: TypeType) -> list[Constraint]:
        if isinstance(self.actual, CallableType):
            return infer_constraints(template.item, self.actual.ret_type, self.direction)
        elif isinstance(self.actual, Overloaded):
            return infer_constraints(template.item, self.actual.items[0].ret_type, self.direction)
        elif isinstance(self.actual, TypeType):
            return infer_constraints(template.item, self.actual.item, self.direction)
        elif isinstance(self.actual, AnyType):
            return infer_constraints(template.item, self.actual, self.direction)
        else:
            return []


def neg_op(op: int) -> int:
    """Map SubtypeOf to SupertypeOf and vice versa."""

    if op == SUBTYPE_OF:
        return SUPERTYPE_OF
    elif op == SUPERTYPE_OF:
        return SUBTYPE_OF
    else:
        raise ValueError(f"Invalid operator {op}")


def find_matching_overload_item(overloaded: Overloaded, template: CallableType) -> CallableType:
    """Disambiguate overload item against a template."""
    items = overloaded.items
    for item in items:
        # Return type may be indeterminate in the template, so ignore it when performing a
        # subtype check.
        if mypy.subtypes.is_callable_compatible(
            item, template, is_compat=mypy.subtypes.is_subtype, ignore_return=True
        ):
            return item
    # Fall back to the first item if we can't find a match. This is totally arbitrary --
    # maybe we should just bail out at this point.
    return items[0]


def find_matching_overload_items(
    overloaded: Overloaded, template: CallableType
) -> list[CallableType]:
    """Like find_matching_overload_item, but return all matches, not just the first."""
    items = overloaded.items
    res = []
    for item in items:
        # Return type may be indeterminate in the template, so ignore it when performing a
        # subtype check.
        if mypy.subtypes.is_callable_compatible(
            item, template, is_compat=mypy.subtypes.is_subtype, ignore_return=True
        ):
            res.append(item)
    if not res:
        # Falling back to all items if we can't find a match is pretty arbitrary, but
        # it maintains backward compatibility.
        res = items[:]
    return res
