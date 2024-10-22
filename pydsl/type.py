import ast
from collections import namedtuple
import ctypes
from enum import Enum, auto
from functools import reduce
from math import log2
import math
import numbers
import sys
import operator
from typing import TYPE_CHECKING, Any, Protocol, Self, runtime_checkable
import typing

import mlir.dialects.index as mlir_index
import mlir.ir as mlir
from mlir.dialects import arith, transform, math as mlirmath
from mlir.ir import (
    F16Type,
    F32Type,
    F64Type,
    IndexType,
    IntegerType,
    OpView,
    Operation,
    Value,
)

from pydsl.macro import CallMacro, Uncompiled
from pydsl.protocols import ToMLIRBase

if TYPE_CHECKING:
    # This is for imports for type hinting purposes only and which can result
    # in cyclic imports.
    from pydsl.frontend import CTypeTree


class Supportable(type):
    """
    A metaclass that modifies the behavior of initialization such that if a
    single variable that is passed in supports the class being initiated,
    then immediate casting of that variable is performed instead.

    This behavior is in likeness to built-in types like str, which uses __str__
    method to indicate a class' ability to be casted into a str.

    Implementers are required to specify their supporter class and caster class
    """

    def __init__(cls, name, bases, namespace, *args, **kwargs):
        if not hasattr(cls, "supporter"):
            raise TypeError(
                f"{cls.__qualname__} is an instance of Supportable, but did "
                f"not specify a supporter protocol"
            )

        if not hasattr(cls, "caster"):
            raise TypeError(
                f"{cls.__qualname__} is an instance of Supportable, but did "
                f"not specify a cast operation in the supporter protocol"
            )

        super().__init__(name, bases, namespace)

    def __call__(cls, *args, **kwargs):
        match args:
            case (cls.supporter(),):
                # If there's exactly one argument and it's a supporter of this
                # class, have the rep cast itself
                (rep,) = args
                return cls.caster(cls, rep)
            case _:
                # Initialize the class as normal
                return super().__call__(*args, **kwargs)


@runtime_checkable
class Lowerable(Protocol):
    def lower(self) -> tuple[Value]: ...

    @classmethod
    def lower_class(cls) -> tuple[mlir.Type]: ...


def lower(
    v: Lowerable | type | OpView | Value | mlir.Type,
) -> tuple[Value] | tuple[mlir.Type]:
    """
    Convert a `Lowerable` type, type instance, and other MLIR objects into its
    lowest MLIR representation, as a tuple.

    This function is *not* idempotent.

    Specific behavior:
    - If `v` is a `Lowerable` type, a `mlir.ir.Type` is returned.
    - If `v` is a `Lowerable` type instance, a `mlir.ir.Value` is returned.
    - If `v` is an `mlir.ir.OpView` type instance, then its results (of type
      `mlir.ir.Value`) are returned.
    - If `v` is already an `mlir.ir.Value` or `mlir.ir.Type`, `v` is returned
      enclosed in a tuple.
    - If `v` is not any of the types above, `TypeError` will be raised.

    For example:
    - `lower(Index)` should be equivalent to `(IndexType.get(),)`.
    - `lower(Index(5))` should be equivalent to
      `(ConstantOp(IndexType.get(), 5).results,)`.
    - ```lower(UInt8(4).op_add(UInt8(5)))``` should be equivalent to ::

        tuple(AddIOp(
            ConstantOp(IntegerType.get_signless(8), 4), )
            ConstantOp(IntegerType.get_signless(8), 5)).results)
    """
    match v:
        case OpView():
            return tuple(v.results)
        case Value() | mlir.Type():
            return (v,)
        case type() if issubclass(v, Lowerable):
            # Lowerable class
            return v.lower_class()
        case _ if issubclass(type(v), Lowerable):
            # Lowerable class instance
            return v.lower()
        case _:
            raise TypeError(f"{v} is not Lowerable")


def get_operator(x):
    target = x

    if issubclass(type(target), Lowerable):
        target = lower_single(target).owner

    if not (
        issubclass(type(target), OpView) or issubclass(type(target), Operation)
    ):
        raise TypeError(f"{x} cannot be cast into an operator")

    return target


def supports_operator(x):
    try:
        get_operator(x)
        return True
    except TypeError:
        return False


def lower_single(
    v: Lowerable | type | OpView | Value | mlir.Type,
) -> mlir.Type | Value:
    """
    lower with the return value stripped of its tuple.
    Lowered output tuple must have length of exactly 1. Otherwise,
    `ValueError` is raised.

    This function is idempotent.
    """

    res = lower(v)
    if len(res) != 1:
        raise ValueError(f"lowering expected single element, got {res}")
    return res[0]


def lower_flatten(li):
    """
    Apply lower to each element of the list, then unpack the resulting tuples
    within the list.
    """
    # Uses map-reduce
    # Map:    lower each element
    # Reduce: flatten the resulting list of tuples into a list of its
    #         constituents
    return reduce(lambda a, b: a + [*b], map(lower, li), [])


class Sign(Enum):
    SIGNED = auto()
    UNSIGNED = auto()


@runtime_checkable
class SupportsInt(Protocol):
    I = typing.TypeVar("I", bound="Int")

    def Int(self, target_type: type[I]) -> I: ...


class Int(metaclass=Supportable):
    supporter = SupportsInt

    def caster(cls, rep):
        return rep.Int(cls)

    width: int = None
    sign: Sign = None
    value: Value

    def __init_subclass__(
        cls, /, width: int, sign: Sign = Sign.SIGNED, **kwargs
    ) -> None:
        super().__init_subclass__()
        cls.width = width
        cls.sign = sign

    def __init__(self, rep: Any) -> None:
        # WARNING: There is no good way to enforce that the OpView type passed
        # in has the right sign.
        # This class is technically low-level enough that it's possible to
        # construct the wrong sign with this function.
        # This is because MLIR by default uses signless for many dialects, and
        # it's up to the language to enforce signs.
        # Users who never touch type implementation won't need to worry, but
        # those who develop type classes can potentially
        # use the wrong sign when wrapping their MLIR OpView back into a
        # language type.

        if not all([self.width, self.sign]):
            raise TypeError(
                f"attempted to initialize {type(self).__name__} without "
                f"defined size or sign"
            )

        if isinstance(rep, OpView):
            rep = rep.result

        match rep:
            # the rep must be real and close enough to an integer
            case numbers.Real() if (math.isclose(rep, int(rep))):
                rep = int(rep)

                if not self.in_range(rep):
                    raise ValueError(
                        f"{rep} is out of range for {type(self).__name__}"
                    )

                self.value = arith.ConstantOp(
                    self.lower_class()[0], rep
                ).result

            case Value():
                self._init_from_mlir_value(rep)

            case _:
                raise TypeError(f"{rep} cannot be casted as an Int")

    def lower(self) -> tuple[Value]:
        return (self.value,)

    @classmethod
    def lower_class(cls) -> tuple[mlir.Type]:
        return (IntegerType.get_signless(cls.width),)

    @classmethod
    def val_range(cls) -> tuple[int, int]:
        match cls.sign:
            case Sign.SIGNED:
                return (-(1 << (cls.width - 1)), 1 << (cls.width - 1))
            case Sign.UNSIGNED:
                return (0, (1 << cls.width) - 2)
            case _:
                AssertionError("unimplemented sign")

    @classmethod
    def in_range(cls, val) -> bool:
        return cls.val_range()[0] <= val <= cls.val_range()[1]

    def _init_from_mlir_value(self, rep) -> None:
        if (rep_type := type(rep.type)) is not IntegerType:
            raise TypeError(
                f"{rep_type.__name__} cannot be casted as a "
                f"{type(self).__name__}"
            )
        if (width := rep.type.width) != self.width:
            raise TypeError(
                f"{type(self).__name__} expected to have width of "
                f"{self.width}, got {width}"
            )
        if not rep.type.is_signless:
            raise TypeError(
                f"ops passed into {type(self).__name__} must have "
                f"signless result, but was signed or unsigned"
            )

        self.value = rep

    @classmethod
    def _try_casting(cls, val) -> None:
        return cls(val)

    # TODO: figure out how to do unsigned -> signed conversion
    # TODO: these arith operators should have automatic width-expansion
    def op_add(self, rhs: SupportsInt) -> "Int":
        rhs = self._try_casting(rhs)
        return self._try_casting(arith.AddIOp(self.value, rhs.value))

    op_radd = op_add  # commutative

    def op_sub(self, rhs: SupportsInt) -> "Int":
        rhs = self._try_casting(rhs)
        return self._try_casting(arith.SubIOp(self.value, rhs.value))

    def op_rsub(self, lhs: SupportsInt) -> "Int":
        lhs = self._try_casting(lhs)
        # note that operators are reversed
        return self._try_casting(arith.SubIOp(lhs.value, self.value))

    def op_mul(self, rhs: SupportsInt) -> "Int":
        rhs = self._try_casting(rhs)
        return self._try_casting(arith.MulIOp(self.value, rhs.value))

    op_rmul = op_mul  # commutative

    # TODO: op_truediv cannot be implemented right now as it returns floating
    # points

    def op_floordiv(self, rhs: SupportsInt) -> "Int":
        rhs = self._try_casting(rhs)
        # assertion ensures that self and rhs have the same sign
        op = (
            arith.FloorDivSIOp if (self.sign == Sign.SIGNED) else arith.DivUIOp
        )
        return self._try_casting(op(self.value, rhs.value))

    def op_rfloordiv(self, lhs: SupportsInt) -> "Int":
        lhs = self._try_casting(lhs)
        # assertion ensures that self and rhs have the same sign
        op = (
            arith.FloorDivSIOp if (self.sign == Sign.SIGNED) else arith.DivUIOp
        )
        return self._try_casting(op(lhs.value, self.value))

    def _compare_with_pred(self, rhs: SupportsInt, pred: arith.CmpIPredicate):
        return Bool(
            arith.CmpIOp(pred, self.value, self._try_casting(rhs).value)
        )

    def op_lt(self, rhs: SupportsInt) -> "Bool":
        rhs = self._try_casting(rhs)

        match self.sign:
            case Sign.SIGNED:
                pred = arith.CmpIPredicate.slt
            case Sign.UNSIGNED:
                pred = arith.CmpIPredicate.ult

        return self._compare_with_pred(rhs, pred)

    def op_le(self, rhs: SupportsInt) -> "Bool":
        rhs = self._try_casting(rhs)

        match self.sign:
            case Sign.SIGNED:
                pred = arith.CmpIPredicate.sle
            case Sign.UNSIGNED:
                pred = arith.CmpIPredicate.ule

        return self._compare_with_pred(rhs, pred)

    def op_eq(self, rhs: SupportsInt) -> "Bool":
        rhs = self._try_casting(rhs)

        return self._compare_with_pred(rhs, arith.CmpIPredicate.eq)

    def op_ne(self, rhs: SupportsInt) -> "Bool":
        rhs = self._try_casting(rhs)

        return self._compare_with_pred(rhs, arith.CmpIPredicate.ne)

    def op_gt(self, rhs: SupportsInt) -> "Bool":
        rhs = self._try_casting(rhs)

        match self.sign:
            case Sign.SIGNED:
                pred = arith.CmpIPredicate.sgt
            case Sign.UNSIGNED:
                pred = arith.CmpIPredicate.ugt

        return self._compare_with_pred(rhs, pred)

    def op_ge(self, rhs: SupportsInt) -> "Bool":
        rhs = self._try_casting(rhs)

        match self.sign:
            case Sign.SIGNED:
                pred = arith.CmpIPredicate.sge
            case Sign.UNSIGNED:
                pred = arith.CmpIPredicate.uge

        return self._compare_with_pred(rhs, pred)

    @classmethod
    def CType(cls) -> tuple[type]:
        ctypes_map = {
            (Sign.SIGNED, 1): ctypes.c_bool,
            (Sign.SIGNED, 8): ctypes.c_int8,
            (Sign.SIGNED, 16): ctypes.c_int16,
            (Sign.SIGNED, 32): ctypes.c_int32,
            (Sign.SIGNED, 64): ctypes.c_int64,
            (Sign.UNSIGNED, 1): ctypes.c_bool,
            (Sign.UNSIGNED, 8): ctypes.c_uint8,
            (Sign.UNSIGNED, 16): ctypes.c_uint16,
            (Sign.UNSIGNED, 32): ctypes.c_uint32,
            (Sign.UNSIGNED, 64): ctypes.c_uint64,
        }

        if (key := (cls.sign, cls.width)) in ctypes_map:
            return (ctypes_map[key],)

        raise TypeError(f"{cls.__name__} does not have a corresponding ctype")

    @classmethod
    def to_CType(cls, pyval: Any):
        try:
            pyval = int(pyval)
        except Exception as e:
            raise TypeError(
                f"{pyval} cannot be converted into an Int ctype"
            ) from e

        if (1 << cls.width) <= pyval:
            raise TypeError(
                f"{pyval} cannot fit into an Int of size {cls.width}"
            )

        if cls.sign is Sign.UNSIGNED and pyval < 0:
            raise ValueError(
                f"expected positive pyval for signless Int, got {pyval}"
            )

        return (pyval,)

    @classmethod
    def from_CType(cls, cval: "CTypeTree"):
        return int(cval[0])

    @CallMacro.generate(is_member=True)
    def on_Call(cls, visitor: "ToMLIRBase", rep: Uncompiled) -> Any:
        match rep:
            case ast.Constant():
                return cls(rep.value)
            case _:
                return cls(visitor.visit(rep))

    I = typing.TypeVar("I", bound="Int")

    def Int(self, target_type: type[I]) -> I:
        if target_type.sign != self.sign:
            raise TypeError(
                f"Int cannot be casted into another Int with differing signs"
            )

        if target_type.width < self.width:
            raise TypeError(
                f"Int of width {self.width} cannot be casted into width "
                f"{target_type.width}. Width must be extended"
            )

        if target_type.width == self.width:
            return target_type._try_casting(self.value)

        if target_type.width > self.width:
            match self.sign:
                case Sign.SIGNED:
                    new_val = arith.ExtSIOp(
                        lower_single(target_type), lower_single(self)
                    )
                case Sign.UNSIGNED:
                    new_val = arith.ExtUIOp(
                        lower_single(target_type), lower_single(self)
                    )

            return target_type._try_casting(new_val)

    F = typing.TypeVar("F", bound="Float")

    def Float(self, target_type: type[F]) -> F:
        match self.sign:
            case Sign.SIGNED:
                return target_type(
                    arith.sitofp(lower_single(target_type), lower_single(self))
                )

            case Sign.UNSIGNED:
                return target_type(
                    arith.uitofp(lower_single(target_type), lower_single(self))
                )


class UInt8(Int, width=8, sign=Sign.UNSIGNED):
    pass


class UInt16(Int, width=16, sign=Sign.UNSIGNED):
    pass


class UInt32(Int, width=32, sign=Sign.UNSIGNED):
    pass


class UInt64(Int, width=64, sign=Sign.UNSIGNED):
    pass


class SInt8(Int, width=8, sign=Sign.SIGNED):
    pass


class SInt16(Int, width=16, sign=Sign.SIGNED):
    pass


class SInt32(Int, width=32, sign=Sign.SIGNED):
    pass


class SInt64(Int, width=64, sign=Sign.SIGNED):
    pass


# It's worth noting that Python treat bool as an integer, meaning that e.g.
# (1 + True) == 2. It is also i1 in MLIR.
# To reflect this behavior, Bool inherits all integer operator overloading
# functions

# TODO: Bool currently does not accept anything except for Python value.
# It should also support ops returning i1


@runtime_checkable
class SupportsBool(Protocol):
    def Bool(self) -> "Bool": ...


class Bool(Int, width=1, sign=Sign.UNSIGNED):
    supporter = SupportsBool

    def caster(cls, rep):
        return rep.Bool()

    def __init__(self, rep: Any) -> None:
        match rep:
            case bool():
                lit_as_bool = 1 if rep else 0
                self.value = arith.ConstantOp(
                    IntegerType.get_signless(1), lit_as_bool
                ).result
            case _:
                return super().__init__(rep)

    def Bool(self) -> "Bool":
        return self

    def on_And(self, rhs: SupportsBool) -> "Bool":
        rhs = Bool(rhs)
        return Bool(arith.AndIOp(self.value, rhs.value))

    def on_Or(self, rhs: SupportsBool) -> "Bool":
        rhs = Bool(rhs)
        return Bool(arith.OrIOp(self.value, rhs.value))

    def on_Not(self) -> "Bool":
        # I have no idea why MLIR doesn't have bitwise not...
        return Bool(
            arith.SelectOp(
                self.value,
                arith.ConstantOp(IntegerType.get_signless(1), 0).result,
                arith.ConstantOp(IntegerType.get_signless(1), 1).result,
            )
        )

    @classmethod
    def from_CType(cls, cval: "CTypeTree") -> bool:
        return bool(cval[0])

    @classmethod
    def to_CType(cls, pyval: int | bool):
        try:
            pyval = bool(pyval)
        except Exception as e:
            raise TypeError(
                f"{pyval} cannot be converted into a {cls.__name__} ctype. "
                f"Reason: {e}"
            )

        return (pyval,)

    @CallMacro.generate(is_member=True)
    def on_Call(
        cls: type[Self], visitor: "ToMLIRBase", rep: Uncompiled
    ) -> Any:
        match rep:
            case ast.Constant():
                return cls(rep.value)
            case _:
                return cls(visitor.visit(rep))


@runtime_checkable
class SupportsFloat(Protocol):
    F = typing.TypeVar("F", bound="Float")

    def Float(self, target_type: type[F]) -> F: ...


class Float(metaclass=Supportable):
    supporter = SupportsFloat

    def caster(cls, rep):
        return rep.Float(cls)

    width: int
    mlir_type: mlir.Type
    value: Value

    def __init_subclass__(
        cls, /, width: int, mlir_type: mlir.Type, **kwargs
    ) -> None:
        super().__init_subclass__(**kwargs)
        cls.width = width
        cls.mlir_type = mlir_type

    def __init__(self, rep: Any) -> None:
        if not all([self.width, self.mlir_type]):
            raise TypeError(
                "attempted to initialize Float without defined width or "
                "mlir_type"
            )

        # TODO: Code duplication in many classes. Consider a superclass?
        if isinstance(rep, OpView):
            rep = rep.result

        match rep:
            case float() | int() | bool():
                rep = float(rep)
                self.value = arith.ConstantOp(
                    self.lower_class()[0], rep
                ).result

            case Value():
                if (rep_type := type(rep.type)) is not self.mlir_type:
                    raise TypeError(
                        f"{rep_type.__name__} cannot be casted as a "
                        f"{type(self).__name__}"
                    )

                self.value = rep

            case _:
                raise TypeError(
                    f"{rep} cannot be casted as a " f"{type(self).__name__}"
                )

    def lower(self) -> tuple[Value]:
        return (self.value,)

    @classmethod
    def lower_class(cls) -> tuple[mlir.Type]:
        return (cls.mlir_type.get(),)

    def _same_type_assertion(self, val):
        if type(self) is not type(val):
            raise TypeError(
                f"{type(self).__name__} cannot be added with "
                f"{type(val).__name__}"
            )

    @classmethod
    def _try_casting(cls, val) -> None:
        return cls(val)

    def op_add(self, rhs: "Float") -> "Float":
        rhs = self._try_casting(rhs)
        return self._try_casting(arith.AddFOp(self.value, rhs.value))

    op_radd = op_add  # commutative

    def op_sub(self, rhs: "Float") -> "Float":
        rhs = self._try_casting(rhs)
        return self._try_casting(arith.SubFOp(self.value, rhs.value))

    def op_rsub(self, rhs: "Float") -> "Float":
        lhs = self._try_casting(rhs)
        # note that operators are reversed
        return self._try_casting(arith.SubFOp(lhs.value, self.value))

    def op_mul(self, rhs: "Float") -> "Float":
        rhs = self._try_casting(rhs)
        return self._try_casting(arith.MulFOp(self.value, rhs.value))

    op_rmul = op_mul  # commutative

    def op_truediv(self, rhs: "Float") -> "Float":
        rhs = self._try_casting(rhs)
        return self._try_casting(arith.DivFOp(self.value, rhs.value))

    def op_rtruediv(self, lhs: "Float") -> "Float":
        lhs = self._try_casting(lhs)
        return self._try_casting(arith.DivFOp(lhs.value, self.value))

    def op_neg(self) -> "Float":
        return self._try_casting(arith.NegFOp(self.value))

    def op_pow(self, rhs: "Float") -> "Float":
        rhs = self._try_casting(rhs)
        return self._try_casting(mlirmath.PowFOp(self.value))

    def _compare_with_pred(
        self, rhs: SupportsFloat, pred: arith.CmpFPredicate
    ):
        return Bool(arith.CmpFOp(pred, self.value, type(self)(rhs).value))

    def op_lt(self, rhs: SupportsInt) -> "Bool":
        rhs = self._try_casting(rhs)
        return self._compare_with_pred(rhs, arith.CmpFPredicate.OLT)

    def op_le(self, rhs: SupportsInt) -> "Bool":
        rhs = self._try_casting(rhs)
        return self._compare_with_pred(rhs, arith.CmpFPredicate.OLE)

    def op_eq(self, rhs: SupportsInt) -> "Bool":
        rhs = self._try_casting(rhs)
        return self._compare_with_pred(rhs, arith.CmpFPredicate.OEQ)

    def op_ne(self, rhs: SupportsInt) -> "Bool":
        rhs = self._try_casting(rhs)
        return self._compare_with_pred(rhs, arith.CmpFPredicate.ONE)

    def op_gt(self, rhs: SupportsInt) -> "Bool":
        rhs = self._try_casting(rhs)
        return self._compare_with_pred(rhs, arith.CmpFPredicate.OGT)

    def op_ge(self, rhs: SupportsInt) -> "Bool":
        rhs = self._try_casting(rhs)
        return self._compare_with_pred(rhs, arith.CmpFPredicate.OGE)

    # TODO: floordiv cannot be implemented so far. float -> int
    # needs floor ops.

    @classmethod
    def CType(cls) -> tuple[type]:
        ctypes_map = {
            32: ctypes.c_float,
            64: ctypes.c_double,
            80: ctypes.c_longdouble,
        }

        if (key := cls.width) in ctypes_map:
            return (ctypes_map[key],)

        raise TypeError(f"{cls.__name__} does not have a corresponding ctype.")

    out_CType = CType

    @classmethod
    def to_CType(cls, pyval: float | int | bool):
        try:
            pyval = float(pyval)
        except Exception as e:
            raise TypeError(
                f"{pyval} cannot be converted into a "
                f"{cls.__name__} ctype. Reason: {e}"
            )

        return (pyval,)

    @classmethod
    def from_CType(cls, cval: "CTypeTree"):
        return float(cval[0])

    @CallMacro.generate(is_member=True)
    def on_Call(
        cls: type[Self], visitor: "ToMLIRBase", rep: Uncompiled
    ) -> Any:
        match rep:
            case ast.Constant():
                return cls(rep.value)
            case _:
                return cls(visitor.visit(rep))

    F = typing.TypeVar("F", bound="Float")

    def Float(self, target_type: type[F]) -> F:
        if target_type.width > self.width:
            return target_type(
                arith.extf(lower_single(target_type), lower_single(self))
            )

        if target_type.width == self.width:
            return target_type(self.value)

        if target_type.width < self.width:
            return target_type(
                arith.truncf(lower_single(target_type), lower_single(self))
            )


class F16(Float, width=16, mlir_type=F16Type):
    pass


class F32(Float, width=32, mlir_type=F32Type):
    pass


class F64(Float, width=64, mlir_type=F64Type):
    pass


# TODO: make this aware of compilation target rather than always making
# the target the current machine this runs on
def get_index_width() -> int:
    s = log2(sys.maxsize + 1) + 1
    assert (
        s.is_integer()
    ), f"the compiler cannot determine the index size of the current "
    f"system. sys.maxsize yielded {sys.maxsize}"

    return int(s)


@runtime_checkable
class SupportsIndex(Protocol):
    def Index(self) -> "Index": ...


# TODO: for now, you can only do limited math on Index
# division requires knowledge of whether Index is signed or unsigned
# everything will be assumed to be unsigned for now...
class Index(Int, width=get_index_width(), sign=Sign.UNSIGNED):
    supporter = SupportsIndex

    def caster(cls, rep):
        return rep.Index()

    @classmethod
    def lower_class(cls) -> tuple[mlir.Type]:
        return (IndexType.get(),)

    I = typing.TypeVar("I", bound="Int")
    def Int(self, cls: type[I]) -> I:
        if self.sign != cls.sign:
            raise TypeError(
                "attempt to cast Index to an Int of different sign"
            )

        op = {
            Sign.SIGNED: arith.index_cast,
            Sign.UNSIGNED: arith.index_castui,
        }

        return cls(
            op[cls.sign](IntegerType.get_signless(cls.width), self.value)
        )

    def Index(self) -> "Index":
        return self
    

    def _init_from_mlir_value(self, rep) -> None:
        if (rep_type := type(rep.type)) is not IndexType:
            raise TypeError(
                f"{rep_type.__name__} cannot be casted as a "
                f"{type(self).__name__}"
            )

        self.value = rep

    def _same_type_assertion(self, val):
        if type(self) is not type(val):
            raise TypeError(
                f"{type(self).__name__} cannot be added with "
                f"{type(val).__name__}"
            )

    def _try_casting(self, val) -> None:
        return type(self)(val)

    def op_add(self, rhs: SupportsIndex) -> "Index":
        rhs = self._try_casting(rhs)
        return type(self)(mlir_index.AddOp(self.value, rhs.value))

    op_radd = op_add  # commutative

    def op_sub(self, rhs: SupportsIndex) -> "Index":
        rhs = self._try_casting(rhs)
        return type(self)(mlir_index.SubOp(self.value, rhs.value))

    def op_rsub(self, lhs: SupportsIndex) -> "Index":
        lhs = self._try_casting(lhs)
        return type(self)(mlir_index.SubOp(lhs.value, self.value))

    def op_mul(self, rhs: SupportsIndex) -> "Index":
        rhs = self._try_casting(rhs)
        return type(self)(mlir_index.MulOp(self.value, rhs.value))

    op_rmul = op_mul  # commutative

    def op_truediv(self, rhs: SupportsIndex) -> "Index":
        raise NotImplementedError()  # TODO

    def op_rtruediv(self, rhs: SupportsIndex) -> "Index":
        raise NotImplementedError()  # TODO

    def op_floordiv(self, rhs: SupportsIndex) -> "Index":
        rhs = self._try_casting(rhs)
        return type(self)(mlir_index.FloorDivSOp(self.value, rhs.value))

    def op_rfloordiv(self, lhs: SupportsIndex) -> "Index":
        lhs = self._try_casting(lhs)
        return type(self)(mlir_index.FloorDivSOp(lhs.value, self.value))

    @classmethod
    def CType(cls) -> tuple[type]:
        # TODO: this needs to be different depending on the platform.
        # On Python, you use sys.maxsize. However, interpreter

        return (ctypes.c_size_t,)

    @classmethod
    def PolyCType(cls) -> tuple[type]:
        return (ctypes.c_int,)

    @classmethod
    def to_CType(cls, pyval: float | int | bool) -> tuple[int]:
        try:
            pyval = int(pyval)
        except Exception as e:
            raise TypeError(
                f"{pyval} cannot be converted into a {cls.__name__} "
                f"ctype. Reason: {e}"
            )

        return (pyval,)

    @classmethod
    def from_CType(cls, cval: "CTypeTree") -> int:
        return int(cval[0])

    @CallMacro.generate(is_member=True)
    def on_Call(
        cls: type[Self], visitor: "ToMLIRBase", rep: Uncompiled
    ) -> Any:
        match rep:
            case ast.Constant():
                return cls(rep.value)
            case _:
                return cls(visitor.visit(rep))


class AnyOp:
    def __init__(self, *_):
        raise NotImplementedError()

    def lower(self):
        raise NotImplementedError()

    @classmethod
    def lower_class(cls) -> tuple[mlir.Type]:
        return (transform.AnyOpType.get(),)


NumberLike: typing.TypeAlias = typing.Union["Number", Int, Float, Index]


class Number:
    """
    A class that represents a generic number constant whose exact
    representation at runtime is evaluated lazily. As long as this type isn't
    used by an MLIR operator, it will only exist at compile-time.

    This type supports any value that is an instance of numbers.Number.

    All numeric literals in pydsl evaluates to this type.

    See _NumberMeta for how its dunder functions are dynamically generated.
    """

    value: numbers.Number
    """
    The internal representation of the number.
    """

    def __init__(self, rep: numbers.Number):
        self.value = rep

    I = typing.TypeVar("I", bound="Int")

    def Int(self, target_type: type[I]) -> I:
        return target_type(self.value)

    F = typing.TypeVar("F", bound="Float")

    def Float(self, target_type: type[F]) -> F:
        return target_type(self.value)

    def Index(self) -> "Index":
        return Index(self.value)


# These are for unary operators in Number class
un_number_op = {
    "op_neg",
    "op_pos",
    "op_abs",
    "op_trunc",
    "op_floor",
    "op_ceil",
    "op_round",
    "op_invert",
}

for op in un_number_op:

    def method_gen(op):
        """
        This function exists simply to allow a unique generic_unary_op to be
        generated whose variables are bound to the arguments of this function
        rather than the variable of the for loop.
        """

        # perform the unary operation on the underlying value
        def generic_unary_op(
            self: "Number", *args, **kwargs
        ) -> numbers.Number:
            return Number(getattr(self.value, op)(*args, **kwargs))

        return generic_unary_op

    setattr(Number, op, method_gen(op))

# These are for binary operators in Number
BinNumberOp = namedtuple(
    "BinNumberOp", "ldunder_name, internal_op, rdunder_name, default_ret_type"
)
bin_number_op = {
    BinNumberOp("op_add", operator.add, "op_radd", Number),
    BinNumberOp("op_sub", operator.sub, "op_rsub", Number),
    BinNumberOp("op_mul", operator.mul, "op_rmul", Number),
    BinNumberOp("op_truediv", operator.truediv, "op_rtruediv", Number),
    BinNumberOp("op_pow", operator.pow, "op_rpow", Number),
    BinNumberOp("op_divmod", divmod, "op_rdivmod", Number),
    BinNumberOp("op_floordiv", operator.floordiv, "op_rfloordiv", Number),
    BinNumberOp("op_mod", operator.mod, "op_rmod", Number),
    BinNumberOp("op_lshift", operator.lshift, "op_rlshift", Number),
    BinNumberOp("op_rshift", operator.rshift, "op_rrshift", Number),
    BinNumberOp("op_and", operator.and_, "op_rand", Number),
    BinNumberOp("op_xor", operator.xor, "op_rxor", Number),
    BinNumberOp("op_xor", operator.or_, "op_ror", Number),
    BinNumberOp("op_lt", operator.lt, "op_gt", Bool),
    BinNumberOp("op_le", operator.le, "op_ge", Bool),
    BinNumberOp("op_eq", operator.le, "op_eq", Bool),
    BinNumberOp("op_ge", operator.ge, "op_le", Bool),
    BinNumberOp("op_gt", operator.gt, "op_lt", Bool),
}


for tup in bin_number_op:
    """
    This dynamically add left-hand dunder operations to Number without
    repeatedly writing the code in generic_op.

    In order for this to work, new methods must be generated by returning
    a function where all of its variables are bound to arguments of its nested
    function (in this case, method_gen).
    """

    def method_gen(tup):
        """
        This function exists simply to allow a unique generic_bin_op to be
        generated whose variables are bound to the arguments of this function
        rather than the variable of the for loop.
        """
        _, internal_op, rdunder_name, default_ret_type = tup

        def generic_bin_op(self, rhs: NumberLike) -> NumberLike:
            # if RHS is also a Number
            if isinstance(rhs, Number):
                # perform the binary operation on the underlying values
                return default_ret_type(internal_op(self.value, rhs.value))
            return getattr(rhs, rdunder_name)(self)

        return generic_bin_op

    ldunder_name, internal_op, rdunder_name, default_ret_type = tup
    setattr(Number, ldunder_name, method_gen(tup))
