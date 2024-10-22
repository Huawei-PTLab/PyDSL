import ast
from ast import AST
from dataclasses import dataclass
import dataclasses
import inspect
import subprocess
import textwrap
import types
import typing
from collections.abc import Callable
from ctypes import POINTER, Structure, cdll
from functools import cache
from logging import warning
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory, mkdtemp
from typing import IO, Any, Protocol, Type, runtime_checkable, Union
from abc import ABC, abstractmethod

from pydsl.compiler import CompilationError, ToMLIR


def compose(funcs):
    def payload(x):
        y = x
        for f in funcs:
            y = f(y)

        return y

    return payload


CTypeTreeType: typing.TypeAlias = tuple[Union[type[Any], "CTypeTreeType"]]
CTypeTree: typing.TypeAlias = tuple[Union[Any, "CTypeTree"]]


@runtime_checkable
class SupportsCType(Protocol):
    @classmethod
    def CType(cls) -> CTypeTreeType:
        """
        Returns the class represented as a tuple tree of ctypes

        NOTE: Structures are not allowed. Represent them instead as a tuple.
        """
        ...

    @classmethod
    def to_CType(cls: type, pyval: Any) -> CTypeTree:
        """
        Take a Python value and convert it to match the types of CType
        """
        ...

    @classmethod
    def from_CType(cls: type, ct: CTypeTree) -> Any:
        """
        Take a tuple tree of ctypes value and convert it into a Python value
        """
        ...


@runtime_checkable
class SupportsPolyCType(Protocol):
    """
    Special case for Poly CTypes. Poly's CType convention is inconsistent with
    the LLVM convention.

    If this protocol is not specified, the regular SupportsCType should be
    used instead
    """

    @classmethod
    def PolyCType(cls) -> CTypeTreeType:
        """
        Returns the class represented as a tuple tree of ctypes

        NOTE: Structures are not allowed. Represent them instead as a tuple.
        """
        ...

    @classmethod
    def to_PolyCType(cls: type, pyval: Any) -> CTypeTree:
        """
        Take a Python value and convert it to match the types of CType
        """
        ...

    @classmethod
    def from_PolyCType(cls: type, ct: CTypeTree) -> Any:
        """
        Take a tuple tree of ctypes value and convert it into a Python value
        """
        ...


@cache
def CTypeTreeType_to_Structure(ct: CTypeTreeType) -> type[Structure] | Any:
    """
    Convert nested tuple tree of ctypes into a dynmaically-generated nested
    subclass of ctypes.Structure.

    If tuple or any child tuple is of length 1, return the ctype as-is without
    creating a Structure type at that node.
    """
    if not isinstance(ct, tuple):
        raise TypeError("ct is not a CTypeTreeType")

    # tuple is flattened if it's only one element
    if len(ct) == 1:
        return (
            CTypeTreeType_to_Structure(ct[0])
            if isinstance(ct[0], tuple)
            else ct[0]
        )

    # perform this operation recursively for all sub-tuples
    ct_recursive = [
        (CTypeTreeType_to_Structure(t) if isinstance(t, tuple) else t)
        for t in ct
    ]

    class AnonymousStructure(Structure):
        f"""
        This structure is dynamically generated by CTypeTreeType_to_Structure
        from {ct}
        """

    AnonymousStructure._fields_ = [
        (f"anonymous_field_{i}", t) for (i, t) in enumerate(ct_recursive)
    ]

    return AnonymousStructure


def CTypeTree_to_Structure(ct: CTypeTreeType, c: CTypeTree):
    """
    Convert nested tuple tree of ctypes and its corresponding tree of instances
    into an instance of a dynmaically-generated nested subclass of
    ctypes.Structure.

    If ct and c's structure don't correspond, an error is thrown.

    If tuple or any child tuple is of length 1, return the ctype as-is without
    creating a Structure type at that node.

    lambda x: CTypeTree_from_Structure(ct, x) o
    lambda x: CTypeTree_to_Structure(ct, x) should yield identity.
    """
    # TODO: this function cannot yet capture cases where shape of the 2 trees
    # matches but the types don't

    if not (isinstance(ct, tuple) and isinstance(c, tuple)):
        raise TypeError(
            "CTypeTree and CTypeTreeType mismatch in terms of " "depth"
        )

    if not len(ct) == len(c):
        raise TypeError(
            "CTypeTree and CTypeTreeType mismatch in terms of " "length"
        )

    # tuple is flattened if it's only one element
    if len(ct) == 1:
        ret = (
            CTypeTree_to_Structure(ct[0], c[0])
            if (isinstance(ct[0], tuple) or isinstance(c[0], tuple))
            else c[0]
        )

        return ret

    c_recursive = [
        (
            CTypeTree_to_Structure(t, val)
            if (isinstance(t, tuple) or isinstance(val, tuple))
            else val
        )
        for t, val in zip(ct, c, strict=False)
    ]

    return CTypeTreeType_to_Structure(ct)(*c_recursive)


def CTypeTree_from_Structure(ct: CTypeTreeType, s: Structure) -> CTypeTree:
    """
    Take a structure and turn it back into tuple representation. The returned
    value will have the same tuple structure as ct.
    """

    if not isinstance(s, Structure):
        raise TypeError("s must be a Structure")

    if not isinstance(ct, tuple):
        raise TypeError("ct must be a CTypeTreeType")

    # this catches the edge case where CTypeTreeType has multiple nested tuple
    # we count the shells to be added back later and strip ct so we can
    # iterate the fields properly
    num_tuple_shell = 0
    while len(ct) == 1:
        num_tuple_shell += 1
        if isinstance(ct[0], tuple):
            ct = ct[0]

    if len(ct) != len(s._fields_):
        raise TypeError(
            f"length mismatch when converting back from Structure: expected"
            f"{ct}, got {len(s._fields_)}"
        )

    ret = []
    for t, (sname, st) in zip(ct, s._fields_, strict=False):
        num_member_tuple_shell = 0
        while isinstance(t, tuple) and len(t) == 1:
            num_member_tuple_shell += 1
            t = t[0]

        val = (
            CTypeTree_from_Structure(t, getattr(s, sname))
            if issubclass(st, Structure)
            else getattr(s, sname)
        )

        if (not issubclass(st, Structure)) and t != st:
            raise TypeError(
                f"type mismatch when converting back from Structure: expected"
                f"{t}, got {st}"
            )

        for _ in range(num_member_tuple_shell):
            val = (val,)

        ret.append(val)

    ret = tuple(ret)
    # re-add the tuple shells lost during the Structure conversion process
    for _ in range(num_tuple_shell):
        ret = (ret,)

    return tuple(ret)


class CompilationTarget(ABC):
    src: str
    settings: "CompilationSetting"
    binpath: Path

    def __init__(self, src: str, settings: "CompilationSetting"):
        self.src = src
        self.settings = settings

        bin_prefix = "pydsl_bin_"

        # Due to TemporaryDirectory not having delete keyword param before 3.12
        # we have to use this very hacky solution
        bin: str = (
            # delete after process end
            TemporaryDirectory(prefix=bin_prefix).name
            if self.settings.clean_temp
            # doesn't delete after process end
            else mkdtemp(prefix=bin_prefix)
        )

        self.binpath = Path(bin)

        if self.settings.dump_mlir:
            print(self.emit_mlir())

        if self.settings.auto_build:
            self.build(src)

    @abstractmethod
    def build(self, src: str) -> None: ...

    @abstractmethod
    def emit_mlir(self) -> str: ...

    @abstractmethod
    def call_function(self, f, *args) -> Any: ...


class CTarget(CompilationTarget):
    _so: NamedTemporaryFile

    _flags = [
        "-eliminate-empty-tensors",
        "-scf-bufferize",
        "-empty-tensor-to-alloc-tensor",
        "-one-shot-bufferize='allow-unknown-ops'",
        "-func-bufferize",
        "-canonicalize",
        "-finalizing-bufferize",
        "-buffer-deallocation",
        "-convert-linalg-to-affine-loops",
        "-cse",
        "-expand-strided-metadata",
        "-lower-affine",
        "-convert-scf-to-cf",
        "-convert-index-to-llvm",
        "-convert-math-to-llvm",
        "-finalize-memref-to-llvm",
        "-llvm-request-c-wrappers",
        "-convert-math-to-libm",
        "-convert-func-to-llvm",
        "-reconcile-unrealized-casts",
    ]

    def cmds_to_str(self, cmds) -> str:
        return " ".join([str(c) for c in cmds])

    def log_stderr(self, cmds, result) -> None:
        if result.stderr:
            warning(
                f"""The following error is caused by this command:

{self.cmds_to_str(cmds)}

Depending on the severity of the message,
compilation may fail entirely.
{"*" * 20}
{result.stderr.decode("utf-8")}{"*" * 20}"""
            )

    def run_and_get_output(self, cmds):
        result = subprocess.run(
            self.cmds_to_str(cmds),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            check=False,
        )
        self.log_stderr(cmds, result)
        return result.stdout.decode("utf-8") if result.stdout else None

    def run_and_pipe_output(self, cmds, stdout: IO):
        result = subprocess.run(
            self.cmds_to_str(cmds),
            stdout=stdout,
            stderr=subprocess.PIPE,
            shell=True,
            check=False,
        )
        self.log_stderr(cmds, result)
        return result.stdout.decode("utf-8") if result.stdout else None

    def check_cmd(self, cmd: str) -> None:
        ...
        # TODO something should be done to ensure that the command exists at
        # all, and should use logging.info to inform which executable is being
        # used

    def mlir_passes(
        self, src: Path, flags, cmd="mlir-opt"
    ) -> NamedTemporaryFile:
        file = NamedTemporaryFile(
            dir=self.binpath, suffix=".mlir", delete=False
        )

        if self.settings.dump_mlir_passes:
            flags.append("-mlir-print-ir-before-all")

        self.run_and_pipe_output([cmd, *flags, src], file)

        return file

    def mlir_to_ll(
        self, src: Path, cmd="mlir-translate"
    ) -> NamedTemporaryFile:
        file = NamedTemporaryFile(dir=self.binpath, suffix=".ll", delete=False)
        self.run_and_get_output([
            cmd,
            "--mlir-to-llvmir",
            src,
            "-o",
            file.name,
        ])

        if self.settings.dump_llvmir:
            print(file.read().decode("utf8"))

        return file

    def ll_to_so(self, src: Path, cmd="clang") -> NamedTemporaryFile:
        file = NamedTemporaryFile(dir=self.binpath, suffix=".so", delete=False)
        self.run_and_get_output([
            cmd,
            "-O3",
            "-shared",
            "-target",
            "aarch64-unknown-linux-gnu",
            src,
            "-o",
            file.name,
        ])

        return file

    @cache
    def load_function(self, f):
        ret_struct: type[Structure] | type = CTypeTreeType_to_Structure(
            self.get_return_ctypes(f)
        )
        # all structs are passed by pointer
        if issubclass(ret_struct, Structure):
            ret_struct = POINTER(ret_struct)

        args_struct: list[type[Structure] | type] = [
            CTypeTreeType_to_Structure(t) for t in self.get_args_ctypes(f)
        ]
        # all structs are passed by pointer
        args_struct = [
            POINTER(t) if issubclass(t, Structure) else t for t in args_struct
        ]

        """
        Manage LLVM C-wrapper calling convention.

        When -llvm-request-c-wrappers gets passed or the function has the unit
        attribute `llvm.emit_c_interface` prior to -convert-func-to-llvm, the
        lowering process:
        - Will create another version of the function with the name
          prepended with `_mlir_ciface_`.
        - All types that are represented in a composite manner, such as MemRef
          or complex types, will be passed into the function through struct
          pointers.
        - If the return type of the function is "composite" in any way, such as
          -> (i32, i32) or -> memref<?x?xi16>, the wrapper function will have
          void return type. Instead of returning the return value directly, it
          writes the return value to the first argument passed into the
          function as a struct pointer.

        Example: If the return type is (i32, memref<?x?xi16>), then when it's
        lowered, the first argument will be expected to be a !llvm.ptr where
        the return type is written to as !llvm.struct<(i32, struct<(ptr, ptr,
        i64, array<2 x i64>, array<2 x i64>)>)>. The function itself is void.

        See more info here:
        https://mlir.llvm.org/docs/TargetLLVMIR/#c-compatible-wrapper-emission
        """

        loaded_so = cdll.LoadLibrary(self._so.name)
        so_f = getattr(  # TODO: this may throw an error
            loaded_so, f"_mlir_ciface_{f.__name__}"
        )

        if self.has_composite_return(f):
            so_f.restype = None  # void function
            args_struct.insert(0, ret_struct)  # instead write to first arg
        else:
            so_f.restype = ret_struct

        so_f.argtypes = tuple(args_struct)

        return so_f

    @cache
    def get_function_signature(self, f) -> inspect.Signature:
        if not isinstance(f, types.FunctionType):
            raise TypeError(f"{f} is not a function")

        match tree := ast.parse(textwrap.dedent(inspect.getsource(f))):
            case ast.Module(body=[ast.FunctionDef(), *_]):
                funcdef = tree.body[0]
                visitor = ToMLIR(self.settings.locals)
                return visitor.get_func_signature(funcdef)

            case _:
                raise AssertionError("malformed function source")

    @cache
    def get_return_type(self, f) -> type:
        ret_t = self.get_function_signature(f).return_annotation

        match ret_t:
            case None | inspect.Signature.empty:
                return ()
            case t if typing.get_origin(t) is None:
                return (ret_t,)
            case t if typing.get_origin(t) is tuple:
                return typing.get_args(t)
            case _:
                raise TypeError(
                    f"return type for {f.__qualname__} is not supported"
                )

    @cache
    def get_return_ctypes(self, f) -> CTypeTreeType:
        ret_t = self.get_return_type(f)

        if not all([isinstance(t, SupportsCType) for t in ret_t]):
            raise TypeError(
                f"return types {ret_t} of {f.__qualname__} cannot be "
                "converted into ctypes. Not all elements implement "
                "SupportsCType"
            )

        # Turn it into a tuple tree of ctypes
        return tuple(self.type_to_CType(t) for t in ret_t)

    @cache
    def get_args_ctypes(self, f) -> CTypeTreeType:
        sig = self.get_function_signature(f)
        args_t = [sig.parameters[key].annotation for key in sig.parameters]

        if not all([isinstance(t, SupportsCType) for t in args_t]):
            raise TypeError(
                f"argument types {args_t} of {f.__qualname__} cannot be "
                "converted into ctypes. Not all elements implement "
                "SupportsCType"
            )

        return tuple(self.type_to_CType(t) for t in args_t)

    def has_composite_return(self, f) -> bool:
        flattened = sum(self.get_return_ctypes(f), ())
        return len(flattened) > 1

    def has_void_return(self, f) -> bool:
        flattened = sum(self.get_return_ctypes(f), ())
        return len(flattened) == 0

    @classmethod
    def type_to_CType(cls, typ: SupportsCType) -> tuple[type]:
        return typ.CType()

    @classmethod
    def val_to_CType(cls, typ: SupportsCType, val: Any) -> tuple[type]:
        return typ.to_CType(val)

    @classmethod
    def val_from_CType(cls, typ: SupportsCType, val: Any) -> tuple[type]:
        return typ.from_CType(val)

    @cache
    def emit_mlir(self) -> str:
        src_ast = ast.parse(self.src)
        transform_seq_ast = (
            ast.parse(inspect.getsource(self.settings.transform_seq))
            if self.settings.transform_seq is not None
            else None
        )
        try:
            return self._emit_mlir(src_ast, transform_seq_ast)
        except CompilationError as ce:
            # add source info and raise it further up the call stack
            ce.src = self.src
            raise ce

    def _emit_mlir(self, src_ast: AST, trans_ast: AST) -> str:
        to_mlir = ToMLIR(self.settings.locals)

        if trans_ast is not None:
            warning("in CTarget debugging mode, transform sequence is ignored")

        return to_mlir.compile(src_ast)

    def build(self, src: str) -> None:
        mlir = self.emit_mlir()

        file = NamedTemporaryFile(dir=self.binpath, suffix=".mlir")

        with open(file.name, "w") as f:
            f.write(mlir)

        def temp_file_to_path(tempf):
            return Path(tempf.name)

        self._so = compose([
            temp_file_to_path,
            lambda path: self.mlir_passes(path, self._flags),
            temp_file_to_path,
            self.mlir_to_ll,
            temp_file_to_path,
            self.ll_to_so,
        ])(file)

    def call_function(self, f, *args) -> Any:
        if not hasattr(self, "_so"):
            raise RuntimeError(
                f"function {f.__name__} is called before it is compiled"
            )

        so_f = self.load_function(f)
        sig = self.get_function_signature(f)
        if not len(sig.parameters) == len(args):
            raise TypeError(
                f"{f.__qualname__} takes {len(sig.parameters)} positional "
                f"argument{'s' if len(sig.parameters) > 1 else ''} "
                f"but {len(args)} were given"
            )

        mapped_args_ct = [
            (ct, self.val_to_CType(sig.parameters[key].annotation, a))
            for ct, key, a in zip(
                self.get_args_ctypes(f), sig.parameters, args, strict=False
            )
        ]

        mapped_args = [CTypeTree_to_Structure(*i) for i in mapped_args_ct]

        if self.has_void_return(f):
            so_f(*mapped_args)
            return None

        if not self.has_composite_return(f):
            return self.val_from_CType(
                self.get_return_type(f)[0],
                # this tuple construct is essential!
                # .from_CType expects a tuple
                (so_f(*mapped_args),),
            )

        # instantiate an empty return structure type
        retval = CTypeTreeType_to_Structure(self.get_return_ctypes(f))()
        mapped_args.insert(0, retval)
        so_f(*mapped_args)
        retval_ct = CTypeTree_from_Structure(self.get_return_ctypes(f), retval)

        return tuple(
            self.val_from_CType(t, ct)
            for t, ct in zip(self.get_return_type(f), retval_ct, strict=False)
        )


class PolyCTarget(CTarget):
    """
    Subclasses CTarget. Basically the same behavior with a few exceptions.

    - Different compilation pipeline
    - Use Poly calling convention. If that's not defined, use the typical LLVM
    C calling convention.
    - Transform sequence is not ignored.
    """

    @classmethod
    def type_to_CType(
        cls, typ: Type[SupportsPolyCType] | Type[SupportsCType]
    ) -> tuple[type]:
        if hasattr(typ, "PolyCType"):
            return typ.PolyCType()

        return typ.CType()

    @classmethod
    def val_to_CType(
        cls, typ: SupportsPolyCType | SupportsCType, val: Any
    ) -> tuple[type]:
        if hasattr(typ, "to_PolyCType"):
            return typ.to_PolyCType(val)

        return typ.to_CType(val)

    @classmethod
    def val_from_CType(
        cls, typ: SupportsPolyCType | SupportsCType, val: Any
    ) -> tuple[type]:
        if hasattr(typ, "from_PolyCType"):
            return typ.from_PolyCType(val)

        return typ.from_CType(val)

    @cache
    def get_return_ctypes(self, f) -> CTypeTreeType:
        ret_t = self.get_return_type(f)

        if not all([
            isinstance(t, SupportsCType) or isinstance(t, SupportsPolyCType)
            for t in ret_t
        ]):
            raise TypeError(
                f"return types {ret_t} of {self._f.__qualname__} cannot be "
                "converted into ctypes. Not all elements implement "
                "SupportsCType"
            )

        # Turn it into a tuple tree of ctypes
        return tuple(self.type_to_CType(t) for t in ret_t)

    @cache
    def get_args_ctypes(self, f) -> CTypeTreeType:
        sig = self.get_function_signature(f)
        args_t = [sig.parameters[key].annotation for key in sig.parameters]

        if not all([
            isinstance(t, SupportsCType) or isinstance(t, SupportsPolyCType)
            for t in args_t
        ]):
            raise TypeError(
                f"argument types {args_t} of {f.__qualname__} cannot be "
                "converted into ctypes. Not all elements implement "
                "SupportsCType"
            )

        return tuple(self.type_to_CType(t) for t in args_t)

    def _emit_mlir(self, src_ast: AST, trans_ast: AST) -> str:
        to_mlir = ToMLIR(self.settings.locals)

        return to_mlir.compile(src_ast, transform_seq=trans_ast)

    def build(self, src: str) -> None:
        raise NotImplementedError()

    @cache
    def load_function(self, f):
        ret_struct: type[Structure] | type = CTypeTreeType_to_Structure(
            self.get_return_ctypes(f)
        )
        # all structs are passed by pointer
        if issubclass(ret_struct, Structure):
            ret_struct = POINTER(ret_struct)

        args_struct: list[type[Structure] | type] = [
            CTypeTreeType_to_Structure(t) for t in self.get_args_ctypes(f)
        ]
        # all structs are passed by pointer
        args_struct = [
            POINTER(t) if issubclass(t, Structure) else t for t in args_struct
        ]

        """
        Manage LLVM C-wrapper calling convention.

        When -llvm-request-c-wrappers gets passed or the function has the unit
        attribute `llvm.emit_c_interface` prior to -convert-func-to-llvm, the
        lowering process:
        - Will create another version of the function with the name
          prepended with `_mlir_ciface_`.
        - All types that are represented in a composite manner, such as MemRef
          or complex types, will be passed into the function through struct
          pointers.
        - If the return type of the function is "composite" in any way, such as
          -> (i32, i32) or -> memref<?x?xi16>, the wrapper function will have
          void return type. Instead of returning the return value directly, it
          writes the return value to the first argument passed into the
          function as a struct pointer.

        Example: If the return type is (i32, memref<?x?xi16>), then when it's
        lowered, the first argument will be expected to be a !llvm.ptr where
        the return type is written to as !llvm.struct<(i32, struct<(ptr, ptr,
        i64, array<2 x i64>, array<2 x i64>)>)>. The function itself is void.

        See more info here:
        https://mlir.llvm.org/docs/TargetLLVMIR/#c-compatible-wrapper-emission
        """

        loaded_so = cdll.LoadLibrary(self._so.name)
        so_f = getattr(loaded_so, f.__name__)  # TODO: this may throw an error

        if self.has_composite_return(f):
            so_f.restype = None  # void function
            args_struct.insert(0, ret_struct)  # instead write to first arg
        else:
            so_f.restype = ret_struct

        so_f.argtypes = tuple(args_struct)

        return so_f

    def call_function(self, f, *args) -> Any:
        if self.has_composite_return(f):
            raise RuntimeError(
                f"PolyCTarget cannot call {f.__name__} because it has "
                f"composite return type"
            )

        if not hasattr(self, "_so"):
            raise RuntimeError(
                f"function {f.__name__} is called before it is compiled"
            )

        so_f = self.load_function(f)
        sig = self.get_function_signature(f)
        if not len(sig.parameters) == len(args):
            raise TypeError(
                f"{f.__qualname__} takes {len(sig.parameters)} positional "
                f"argument{'s' if len(sig.parameters) > 1 else ''} "
                f"but {len(args)} were given"
            )

        mapped_args_ct = [
            (ct, self.val_to_CType(sig.parameters[key].annotation, a))
            for ct, key, a in zip(
                self.get_args_ctypes(f), sig.parameters, args, strict=False
            )
        ]

        mapped_args = [CTypeTree_to_Structure(*i) for i in mapped_args_ct]

        if self.has_void_return(f):
            so_f(*mapped_args)
            return None

        if not self.has_composite_return(f):
            return self.val_from_CType(
                self.get_return_type(f)[0],
                # this tuple construct is essential!
                # .from_CType expects a tuple
                (so_f(*mapped_args),),
            )

        # instantiate an empty return structure type
        retval = CTypeTreeType_to_Structure(self.get_return_ctypes(f))()
        mapped_args.insert(0, retval)
        so_f(*mapped_args)
        retval_ct = CTypeTree_from_Structure(self.get_return_ctypes(f), retval)

        return tuple(
            self.val_from_PolyCType(t, ct)
            for t, ct in zip(self.get_return_type(f), retval_ct, strict=False)
        )


@dataclass
class CompilationSetting:
    # TODO: complete documentation here
    transform_seq: typing.Optional[typing.Callable[[Any], None]] = None
    dump_mlir: bool = False
    dump_mlir_passes: bool = False
    """
    Whether or not for `mlir-opt` to log the IR between each pass performed.

    This is equivalent to passing `-mlir-print-ir-before-all` to `mlir-opt`.
    """
    dump_llvmir: bool = False
    """
    Whether or not to log the resulting LLVM IR program during compilation
    """
    auto_build: bool = True
    clean_temp: bool = False
    target_class: Type["CompilationTarget"] = CTarget
    locals: dict[str, Any] = dataclasses.field(default_factory=dict)
    dataset: str = "DEFAULT_DATASET"


class CompiledFunction:
    _f: Callable[..., Any]
    _settings: CompilationSetting
    _target: CompilationTarget

    def __init__(
        self, f: Callable, locals: dict[str, Any], **settings
    ) -> None:
        # These variables must be immutable because emit_mlir relies on them
        # and caches its result
        self._f = f
        self._settings = CompilationSetting(**settings)
        self._settings.locals = locals

        src = textwrap.dedent(inspect.getsource(self._f))
        self._target = self._settings.target_class(src, self._settings)

    def emit_mlir(self) -> str:
        return self._target.emit_mlir()

    def __call__(self, *args) -> Any:
        return self._target.call_function(self._f, *args)


def compile(
    f_vars: typing.Optional[dict[str, Any]] = None, **settings
) -> Callable[..., CompiledFunction]:
    """
    Compile the function into MLIR and lower it to a temporary shared library
    object.

    The lowered function is a CompiledFunction object which may be called
    directly to run the respective function in the library.

    Refer to `CompilationSetting` for a full list of keyword arguments
    passable to settings.

    While the compile decorator will try its best to determine what variables
    you need when f_vars is not provided, it cannot determine everything.
    It will try to include globals(), builtin variables, and locals(), but
    free variables in outer nested functions from where the decorated function
    is defined cannot be attained.
    This is an API gap of Python's metaprogramming features and
    nothing can be done about it:
    https://stackoverflow.com/questions/1041639/get-a-dict-of-all-variables-currently-in-scope-and-their-values.

    f_vars: a dictionary of local variables you want the function to have
    access to. Typically passing in Python's built-in `locals()` is sufficient.
    transform_seq: the function acting as the transform sequence that is
    intended to transform this function.
    dump_mlir: whether or not to print out the MLIR output to standard output.
    This is helpful if you want to pipe the MLIR to your own toolchain.
    """

    if f_vars is None:
        # get the frame that called this function
        f_back = inspect.currentframe().f_back

        # make a dictionary starting with builtins, then update it with
        # globals, then update with locals of the last calling frame
        #
        # globals overwrites any repeated keys from builtins, locals overwrites
        # any repeated keys from globals and builtins
        f_vars = dict(
            dict(f_back.f_builtins, **f_back.f_globals), **f_back.f_locals
        )

    def compile_payload(f: Callable) -> CompiledFunction:
        cf = CompiledFunction(
            f,
            f_vars,
            **settings,
        )

        return cf

    return compile_payload
