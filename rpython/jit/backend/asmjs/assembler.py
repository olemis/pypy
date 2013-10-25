
from rpython.rlib import rgc
from rpython.rlib.rarithmetic import r_uint
from rpython.jit.backend.llsupport import symbolic, jitframe, rewrite
from rpython.jit.backend.llsupport.regalloc import compute_vars_longevity
from rpython.jit.backend.llsupport.descr import (unpack_fielddescr,
                                                 unpack_arraydescr,
                                                 unpack_interiorfielddescr,
                                                 ArrayDescr, CallDescr,
                                                 FieldDescr)
from rpython.jit.codewriter import longlong
from rpython.rtyper.lltypesystem import lltype, rffi, rstr
from rpython.rtyper.annlowlevel import cast_instance_to_gcref, llhelper
from rpython.jit.backend.model import CompiledLoopToken
from rpython.jit.metainterp.resoperation import rop
from rpython.jit.metainterp.history import (AbstractFailDescr, ConstInt,
                                            ConstPtr, Box, TargetToken, INT,
                                            REF, FLOAT, BoxInt, BoxFloat,
                                            BoxPtr, JitCellToken, ConstFloat)

from rpython.jit.backend.asmjs import support
from rpython.jit.backend.asmjs.arch import WORD, DEBUGMODE
from rpython.jit.backend.asmjs.jsbuilder import (ASMJSBuilder, IntBinOp,
                                                 IntUnaryOp, ClassPtrTypeID,
                                                 HeapData, IntScaleOp,
                                                 HeapType, Int8, Int32,
                                                 UIntCast, DblCast, DblBinOp,
                                                 DblUnaryOp,
                                                 IntCast, IntCallFunc,
                                                 JitFrameAddr,
                                                 JitFrameAddr_base,
                                                 JitFrameAddr_gcmap,
                                                 JitFrameAddr_guard_exc,
                                                 JitFrameAddr_force_descr,
                                                 JitFrameAddr_descr)


CONST_ZERO = ConstInt(0)

GILFUNCPTR = lltype.Ptr(lltype.FuncType([], lltype.Void))


@rgc.no_collect
def release_gil_shadowstack():
    before = rffi.aroundstate.before
    if before:
        before()


@rgc.no_collect
def reacquire_gil_shadowstack():
    after = rffi.aroundstate.after
    if after:
        after()


INVALIDATION_COUNTER = lltype.Struct(
    "INVALIDATIONCOUNTER",
    ("counter", lltype.Signed)
)


class CompiledLoopTokenASMJS(CompiledLoopToken):
    """CompiledLoopToken with extra fields for asmjs backend."""

    def __init__(self, cpu, number):
        CompiledLoopToken.__init__(self, cpu, number)
        self.all_gcrefs = []
        self.compiled_function_id = 0
        self.compiled_guard_funcs = []
        self.compiled_gcmaps = []
        self.invalidation = lltype.malloc(INVALIDATION_COUNTER, flavor="raw")
        self.invalidation.counter = 0

    def __del__(self):
        CompiledLoopToken.__del__(self)
        lltype.free(self.invalidation, flavor="raw")


class AssemblerASMJS(object):
    """Class for assembling a Trace into a compiled ASMJS function."""

    def __init__(self, cpu):
        self.cpu = cpu

    def set_debug(self, v):
        return False

    def setup_once(self):
        self.execute_trampoline_addr = self.cpu.get_execute_trampoline_adr()
        release_gil_func = llhelper(GILFUNCPTR,
                                    release_gil_shadowstack)
        self.release_gil_addr = self.cpu.cast_ptr_to_int(release_gil_func)
        reacquire_gil_func = llhelper(GILFUNCPTR,
                                      reacquire_gil_shadowstack)
        self.reacquire_gil_addr = self.cpu.cast_ptr_to_int(reacquire_gil_func)
        gc_ll_descr = self.cpu.gc_ll_descr
        gc_ll_descr.initialize()
        if gc_ll_descr.get_malloc_slowpath_addr is not None:
            nm = "malloc_nursery"
            self.gc_malloc_nursery_addr = gc_ll_descr.get_malloc_fn_addr(nm)
            nm = "malloc_array"
            self.gc_malloc_array_addr = gc_ll_descr.get_malloc_fn_addr(nm)
        if hasattr(gc_ll_descr, "malloc_str"):
            nm = "malloc_str"
            self.gc_malloc_str_addr = gc_ll_descr.get_malloc_fn_addr(nm)
        else:
            self.gc_malloc_str_addr = None
        if hasattr(gc_ll_descr, "malloc_unicode"):
            nm = "malloc_unicode"
            self.gc_malloc_unicode_addr = gc_ll_descr.get_malloc_fn_addr(nm)
        else:
            self.gc_malloc_unicode_addr = None

    def finish_once(self):
        pass

    def setup(self, looptoken):
        self.current_clt = looptoken.compiled_loop_token
        self.js = ASMJSBuilder(self.cpu)
        self.pending_target_tokens = []
        self.has_unimplemented_ops = False
        self.longevity = {}
        self.required_frame_depth = 0
        self.spilled_frame_locations = {}
        self.spilled_frame_values = {}
        self.spilled_frame_offset = 0

    def teardown(self):
        if self.has_unimplemented_ops:
            raise RuntimeError("TRACE CONTAINS UNIMPLEMENTED OPERATIONS")
        self.current_clt = None
        self.js = None
        self.pending_target_tokens = None
        self.has_unimplemented_ops = False
        self.longevity = None
        self.required_frame_depth = 0
        self.spilled_frame_locations = None
        self.spilled_frame_values = None
        self.spilled_frame_offset = 0

    def assemble_loop(self, loopname, inputargs, operations, looptoken, log):
        """Assemble and compile a new loop function from the given trace.

        This method takes the recorded trace, generates asmjs source code from
        it, then invokes an external helper to compile that source into a new
        function object.  It attached a "function id" to the CompiledLoopToken
        which can be used to invoke the new function.
        """
        clt = CompiledLoopTokenASMJS(self.cpu, looptoken.number)
        looptoken.compiled_loop_token = clt
        self.setup(looptoken)
        operations = self._prepare_loop(inputargs, operations, looptoken)
        self._assemble(inputargs, operations)
        clt.frame_info = self._allocate_frame_info(self.required_frame_depth)
        clt.compiled_function_id = self._compile(self.js)
        self._finalize_pending_target_tokens(clt.compiled_function_id)
        self.teardown()

    def assemble_bridge(self, faildescr, inputargs, operations,
                        original_loop_token, log):
        """Assemble, compile and link a new bridge from the given trace.

        This method takes the recorded trace, generates asmjs source code
        from it, then invokes and external helper to compile it over the top
        of the existing function id for the bridged guard.
        """
        assert isinstance(faildescr, AbstractFailDescr)
        self.setup(original_loop_token)
        operations = self._prepare_bridge(inputargs, operations,
                                          original_loop_token)
        self._assemble(inputargs, operations)
        self._update_frame_info(self.current_clt.frame_info,
                                self.required_frame_depth)
        self._recompile(faildescr._asmjs_funcid, self.js)
        self._finalize_pending_target_tokens(faildescr._asmjs_funcid)
        self.teardown()

    def free_loop_and_bridges(self, compiled_loop_token):
        for gcmap in compiled_loop_token.compiled_gcmaps:
            lltype.free(gcmap, flavor="raw")
        for funcid in compiled_loop_token.compiled_guard_funcs:
            support.jitFree(funcid)
        self._free_frame_info(compiled_loop_token.frame_info)
        support.jitFree(compiled_loop_token.compiled_function_id)

    def invalidate_loop(self, looptoken):
        looptoken.compiled_loop_token.invalidation.counter += 1

    def _allocate_frame_info(self, required_depth):
        baseofs = self.cpu.get_baseofs_of_frame_field()
        frame_info = lltype.malloc(jitframe.JITFRAMEINFO, flavor="raw")
        frame_info_ptr = rffi.cast(jitframe.JITFRAMEINFOPTR, frame_info)
        frame_info_ptr.clear()
        frame_info_ptr.update_frame_depth(baseofs, required_depth)
        return frame_info_ptr

    def _update_frame_info(self, frame_info_ptr, required_depth):
        baseofs = self.cpu.get_baseofs_of_frame_field()
        frame_info_ptr.update_frame_depth(baseofs, required_depth)
        return frame_info_ptr

    def _free_frame_info(self, frame_info_ptr):
        lltype.free(frame_info_ptr, flavor="raw")

    def _ensure_frame_depth(self, required_depth):
        if self.required_frame_depth < required_depth:
            self.required_frame_depth = required_depth

    def _prepare_loop(self, inputargs, operations, looptoken):
        operations = self._prepare(inputargs, operations, looptoken)
        locations = self._get_frame_locations(inputargs)
        # Store the list of locs on the token, so that the calling
        # code knows where to write them to and the GCRewriter can find refs.
        looptoken.compiled_loop_token._ll_initial_locs = locations
        return operations

    def _prepare_bridge(self, inputargs, operations, looptoken):
        operations = self._prepare(inputargs, operations, looptoken)
        return operations

    def _prepare(self, inputargs, operations, looptoken):
        cpu = self.cpu
        allgcrefs = looptoken.compiled_loop_token.all_gcrefs
        return cpu.gc_ll_descr.rewrite_assembler(cpu, operations, allgcrefs)

    def _find_loop_entry_token(self, operations):
        """Find the looptoken targetted by the final JUMP, if any.

        This method finds and marks the target of the backwards jump at the
        end of the loop.  It costs us an iteration over the ops list, but it
        allows the js to more efficiently generate its looping structure
        by knowing this information up-front.
        """
        local_targets = []
        for i in range(len(operations) - 1):
            op = operations[i]
            assert op.getopnum() != rop.JUMP
            if op.getopnum() == rop.LABEL:
                descr = op.getdescr()
                assert isinstance(descr, TargetToken)
                local_targets.append(descr)
        final_op = operations[-1]
        if final_op.getopnum() == rop.JUMP:
            descr = final_op.getdescr()
            assert isinstance(descr, TargetToken)
            if descr in local_targets:
                self.js.set_loop_entry_token(descr)

    def _assemble(self, inputargs, operations):
        """Generate the body of the asmjs function for the given trace."""
        self.longevity, _ = compute_vars_longevity(inputargs, operations)
        self._find_loop_entry_token(operations)
        # Load input arguments from frame if we are entering from the top.
        self.js.emit("if((goto|0) == (0|0)){\n")
        self._genop_load_input_args(inputargs)
        self.js.emit("}\n")
        # Check that the frame is big enough, and re-allocate it if not.
        # XXX TODO: this is silly to do at the entry to every function
        # but greatly simplifies things for now. 
        p_frame_depth = self.js.allocate_placeholder()
        descrs = self.cpu.gc_ll_descr.getframedescrs(self.cpu)
        offset = self.cpu.unpack_fielddescr(descrs.arraydescr.lendescr)
        cur_frame_depth = HeapData(Int32, IntBinOp("+", JitFrameAddr(),
                                                        ConstInt(offset)))
        self.js.emit("if(")
        self.js.emit_value(IntBinOp("<", cur_frame_depth, p_frame_depth))
        self.js.emit("){\n")
        newframe = BoxPtr()
        realloc_frame = self.cpu.realloc_frame
        self.js.emit_dyncall(ConstInt(realloc_frame), "iii",
                             [JitFrameAddr(), p_frame_depth], newframe)
        self.js.emit_assign_jitframe(newframe)
        self.js.free_variable(newframe)
        self.js.emit("}\n")
        # Walk the list of operations, emitting code for each.
        # We expend minimal effort on optimizing the generated code, on the
        # expectation that the host JIT will do low-level optimization for us.
        i = 0
        while i < len(operations):
            op = operations[i]
            # Emit code for the operation.
            # Most can be implemented stand-alone, but a few require
            # knowledge of the following guard.  A few can even be ignored
            # completely because they don't actually *do* anything!
            if op.has_no_side_effect() and op.result not in self.longevity:
                self.js.emit_comment("OMMITTED USELESS JIT OP: %s" % (op,))
            else:
                self.js.emit_comment("BEGIN JIT OP: %s" % (op,))
                if not self._op_needs_guard(op):
                    genop_list[op.getopnum()](self, op)
                else:
                    i += 1
                    if DEBUGMODE:
                        assert i < len(operations)
                    guardop = operations[i]
                    if DEBUGMODE:
                        assert guardop.is_guard()
                    self.js.emit_comment("  WITH GUARD OP: %s" % (guardop,))
                    genop_withguard_list[op.getopnum()](self, op, guardop)
                self.js.emit_comment("DONE JIT OP")
            # Free vars for boxes that are no longer needed.
            for j in range(op.numargs()):
                box = op.getarg(j)
                if self._is_dead_box(box, i):
                    self.js.free_variable(box)
            if self._is_dead_box(op.result, i):
                self.js.free_variable(op.result)
            i += 1
        # Back-patch the required frame depth into the above check.
        self.js.fill_placeholder(p_frame_depth, self.required_frame_depth)

    def _is_dead_box(self, box, i):
        """Check if the given box is no longer needed after position i."""
        if box is None or not isinstance(box, Box):
            return False
        try:
            if self.longevity[box][1] > i:
                return False
        except KeyError:
            pass
        return True

    def _genop_load_input_args(self, inputargs):
        locations = self._get_frame_locations(inputargs)
        assert len(inputargs) == len(locations)
        for i in xrange(len(inputargs)):
            box = inputargs[i]
            if not box:
                continue
            offset = locations[i]
            addr = IntBinOp("+", JitFrameAddr_base(), ConstInt(offset))
            typ = HeapType.from_box(box)
            self.js.emit_load(box, addr, typ)
        return locations

    def _genop_write_output_args(self, inputargs):
        locations = self._get_frame_locations(inputargs)
        assert len(inputargs) == len(locations)
        with ctx_spill_to_frame(self) as f:
            for i in xrange(len(inputargs)):
                box = inputargs[i]
                offset = locations[i]
                curval = self.spilled_frame_values.get(offset, None)
                if curval is not None:
                    assert curval == box
                else:
                    f.genop_spill_to_frame(box, offset)
            self._genop_store_gcmap()
        return locations

    def _get_frame_locations(self, arguments):
        """Allocate locations in the frame for all the given arguments."""
        locations = [-1] * len(arguments)
        offset = 0
        for i in xrange(len(arguments)):
            box = arguments[i]
            typ = HeapType.from_box(box)
            alignment = offset % typ.size
            if alignment:
                offset += alignment
            locations[i] = offset
            offset += typ.size
        self._ensure_frame_depth(offset)
        return locations

    def _op_needs_guard(self, op):
        """Check if the given op must be implemented together with a guard.

        Some operations only make sense when followed immediately by a specific
        guard op, and we can only emit correct and efficient code when we
        consider the operation and its guard as a single unit.
        """
        # Possibly-overflowing ops must have an overflow-checking guard.
        if op.is_ovf():
            return True
        # Calls that may force must be followed by a force-checking guard.
        opnum = op.getopnum()
        if opnum == rop.CALL_MAY_FORCE:
            return True
        elif opnum == rop.CALL_ASSEMBLER:
            return True
        elif opnum == rop.CALL_RELEASE_GIL:
            return True
        # All other operations can be implemented stand-alone.
        return False

    def _compile(self, js):
        """Compile the generated source into an invokable asmjs function.

        This method passes the generated source to an external javascript
        helper, which compiles it into a function and returns an opaque
        integer "function id" that can be used to invoke the code.
        """
        jssrc = js.finish()
        assert not self.has_unimplemented_ops
        funcid = support.jitCompile(jssrc)
        return funcid

    def _recompile(self, function_id, js):
        """Compile generated source as replacement for an existing function.

        This method passes the generated source to an external javascript
        helper, which compiles it into a function and uses that to replace
        any existing function associated with the given function id.
        """
        jssrc = js.finish()
        assert not self.has_unimplemented_ops
        support.jitRecompile(function_id, jssrc)

    def _finalize_pending_target_tokens(self, function_id):
        """Attach the given function id to any pending target tokens.

        The target tokens have to be annotated with their compiled function
        id before other code can generate jump to them.
        """
        for token in self.pending_target_tokens:
            token._asmjs_funcid = function_id
            # Let the argboxes be GC'd, we can't generate any
            # more local jumps after compilation.
            token._asmjs_argboxes = None

    #
    #  Code-Generating dispatch methods.
    #  There's a method here for every resop we support.
    #  They are built into a jump-table by code at the end of this file.
    #

    def genop_label(self, op):
        descr = op.getdescr()
        assert isinstance(descr, TargetToken)
        # Attach the argument boxes to the descr, so that
        # jumps to this label know where to put their arguments.
        inputargs = op.getarglist()
        argboxes = [None] * len(inputargs)
        for i in range(len(inputargs)):
            argbox = inputargs[i]
            assert isinstance(argbox, Box)
            # XXX TODO: do this by fiddling with longevity, not immortality
            self.js.allocate_variable(argbox, immortal=True)
            argboxes[i] = argbox
        descr._asmjs_argboxes = argboxes
        descr._asmjs_label = self.js.emit_token_label(descr)
        descr._asmjs_funcid = 0  # placeholder; this gets set after compilation
        self.pending_target_tokens.append(descr)
        # Load input arguments from frame if we are entering at this label.
        self.js.emit("if((goto|0) == (%d|0)){\n" % (descr._asmjs_label,))
        self._genop_load_input_args(inputargs)
        self.js.emit("}\n")

    def genop_strgetitem(self, op):
        base = op.getarg(0)
        offset = op.getarg(1)
        arraytoken = symbolic.get_array_token(rstr.STR,
                                              self.cpu.translate_support_code)
        basesize, itemsize, len_offset = arraytoken
        assert itemsize == 1
        addr = IntBinOp("+", base, IntBinOp("+", ConstInt(basesize), offset))
        self.js.emit_load(op.result, addr, Int8)

    def genop_strsetitem(self, op):
        base = op.getarg(0)
        offset = op.getarg(1)
        value = op.getarg(2)
        arraytoken = symbolic.get_array_token(rstr.STR,
                                              self.cpu.translate_support_code)
        basesize, itemsize, len_offset = arraytoken
        assert itemsize == 1
        addr = IntBinOp("+", base, IntBinOp("+", ConstInt(basesize), offset))
        self.js.emit_store(value, addr, Int8)

    def genop_strlen(self, op):
        base = op.getarg(0)
        arraytoken = symbolic.get_array_token(rstr.STR,
                                              self.cpu.translate_support_code)
        basesize, itemsize, len_offset = arraytoken
        addr = IntBinOp("+", base, ConstInt(len_offset))
        self.js.emit_load(op.result, addr, Int32)

    def genop_copystrcontent(self, op):
        arraytoken = symbolic.get_array_token(rstr.STR,
                                              self.cpu.translate_support_code)
        self._genop_copy_array(op, arraytoken)

    def genop_unicodegetitem(self, op):
        base = op.getarg(0)
        offset = op.getarg(1)
        arraytoken = symbolic.get_array_token(rstr.UNICODE,
                                              self.cpu.translate_support_code)
        basesize, itemsize, len_offset = arraytoken
        typ = HeapType.from_size(itemsize)
        addr = IntBinOp("+", base, IntBinOp("+", ConstInt(basesize),
                                                 IntScaleOp(offset, itemsize)))
        self.js.emit_load(op.result, addr, typ)

    def genop_unicodesetitem(self, op):
        base = op.getarg(0)
        offset = op.getarg(1)
        value = op.getarg(2)
        arraytoken = symbolic.get_array_token(rstr.UNICODE,
                                              self.cpu.translate_support_code)
        basesize, itemsize, len_offset = arraytoken
        typ = HeapType.from_size(itemsize)
        addr = IntBinOp("+", base, IntBinOp("+", ConstInt(basesize), 
                                                 IntScaleOp(offset, itemsize)))
        self.js.emit_store(value, addr, typ)

    def genop_unicodelen(self, op):
        base = op.getarg(0)
        arraytoken = symbolic.get_array_token(rstr.UNICODE,
                                              self.cpu.translate_support_code)
        basesize, itemsize, len_offset = arraytoken
        addr = IntBinOp("+", base, ConstInt(len_offset))
        self.js.emit_load(op.result, addr, Int32)

    def genop_copyunicodecontent(self, op):
        arraytoken = symbolic.get_array_token(rstr.UNICODE,
                                              self.cpu.translate_support_code)
        self._genop_copy_array(op, arraytoken)

    def _genop_copy_array(self, op, arraytoken):
        basesize, itemsize, _ = arraytoken
        srcbase = op.getarg(0)
        dstbase = op.getarg(1)
        srcoffset = op.getarg(2)
        dstoffset = op.getarg(3)
        lengthbox = op.getarg(4)
        assert srcbase != dstbase
        # Calculate offset into source array.
        srcaddr = IntScaleOp(srcoffset, itemsize)
        srcaddr = IntBinOp("+", srcaddr, ConstInt(basesize))
        srcaddr = IntBinOp("+", srcbase, srcaddr)
        # Calculate offset into destination array.
        dstaddr = IntScaleOp(dstoffset, itemsize)
        dstaddr = IntBinOp("+", dstaddr, ConstInt(basesize))
        dstaddr = IntBinOp("+", dstbase, dstaddr)
        # Memcpy required number of bytes.
        nbytes = IntScaleOp(lengthbox, itemsize)
        self.js.emit_call("memcpy", "iiii", [dstaddr, srcaddr, nbytes], None)

    def genop_getfield_gc(self, op):
        base = op.getarg(0)
        fielddescr = op.getdescr()
        assert isinstance(fielddescr, FieldDescr)
        offset, fieldsize, signed = unpack_fielddescr(op.getdescr())
        addr = IntBinOp("+", base, ConstInt(offset))
        typ = HeapType.from_size_and_sign(fieldsize, signed)
        self.js.emit_load(op.result, addr, typ)

    genop_getfield_raw = genop_getfield_gc
    genop_getfield_raw_pure = genop_getfield_gc
    genop_getfield_gc_pure = genop_getfield_gc

    def genop_setfield_gc(self, op):
        base = op.getarg(0)
        value = op.getarg(1)
        offset, fieldsize, signed = unpack_fielddescr(op.getdescr())
        addr = IntBinOp("+", base, ConstInt(offset))
        typ = HeapType.from_size_and_sign(fieldsize, signed)
        self.js.emit_store(value, addr, typ)

    genop_setfield_raw = genop_setfield_gc

    def genop_getinteriorfield_gc(self, op):
        t = unpack_interiorfielddescr(op.getdescr())
        offset, itemsize, fieldsize, signed = t
        base = op.getarg(0)
        which = op.getarg(1)
        addr = IntBinOp("+", base, ConstInt(offset))
        addr = IntBinOp("+", addr, IntScaleOp(which, itemsize))
        typ = HeapType.from_size_and_sign(fieldsize, signed)
        self.js.emit_load(op.result, addr, typ)

    def genop_setinteriorfield_gc(self, op):
        t = unpack_interiorfielddescr(op.getdescr())
        offset, itemsize, fieldsize, signed = t
        base = op.getarg(0)
        which = op.getarg(1)
        value = op.getarg(2)
        addr = IntBinOp("+", base, ConstInt(offset))
        addr = IntBinOp("+", addr, IntScaleOp(which, itemsize))
        typ = HeapType.from_size_and_sign(fieldsize, signed)
        self.js.emit_store(value, addr, typ)

    genop_setinteriorfield_raw = genop_setinteriorfield_gc

    def genop_arraylen_gc(self, op):
        descr = op.getdescr()
        assert isinstance(descr, ArrayDescr)
        len_offset = descr.lendescr.offset
        base = op.getarg(0)
        addr = IntBinOp("+", base, ConstInt(len_offset))
        self.js.emit_load(op.result, addr, Int32)

    def genop_getarrayitem_gc(self, op):
        itemsize, offset, signed = unpack_arraydescr(op.getdescr())
        base = op.getarg(0)
        which = op.getarg(1)
        addr = IntBinOp("+", base, ConstInt(offset))
        addr = IntBinOp("+", addr, IntScaleOp(which, itemsize))
        typ = HeapType.from_size_and_sign(itemsize, signed)
        self.js.emit_load(op.result, addr, typ)

    genop_getarrayitem_gc_pure = genop_getarrayitem_gc
    genop_getarrayitem_raw = genop_getarrayitem_gc
    genop_getarrayitem_raw_pure = genop_getarrayitem_gc
    genop_raw_load = genop_getarrayitem_gc

    def genop_setarrayitem_gc(self, op):
        itemsize, offset, signed = unpack_arraydescr(op.getdescr())
        base = op.getarg(0)
        where = op.getarg(1)
        value = op.getarg(2)
        addr = IntBinOp("+", base, ConstInt(offset))
        addr = IntBinOp("+", addr, IntScaleOp(where, itemsize))
        typ = HeapType.from_size_and_sign(itemsize, signed)
        self.js.emit_store(value, addr, typ)

    genop_setarrayitem_raw = genop_setarrayitem_gc
    genop_raw_store = genop_setarrayitem_gc

    def genop_int_is_true(self, op):
        self.js.emit_assignment(op.result, op.getarg(0))

    def _genop_int_unaryop(operator):
        def genop_int_unaryop(self, op):
            val = IntUnaryOp(operator, op.getarg(0))
            self.js.emit_assignment(op.result, val)
        return genop_int_unaryop

    genop_int_is_zero = _genop_int_unaryop("!")
    genop_int_neg = _genop_int_unaryop("~")
    genop_int_invert = _genop_int_unaryop("!")

    def _genop_int_binop(binop):
        def genop_int_binop(self, op):
            val = IntBinOp(binop, op.getarg(0), op.getarg(1))
            self.js.emit_assignment(op.result, val)
        return genop_int_binop

    genop_int_lt = _genop_int_binop("<")
    genop_int_le = _genop_int_binop("<=")
    genop_int_eq = _genop_int_binop("==")
    genop_int_ne = _genop_int_binop("!=")
    genop_int_gt = _genop_int_binop(">")
    genop_int_ge = _genop_int_binop(">=")
    genop_int_add = _genop_int_binop("+")
    genop_int_sub = _genop_int_binop("-")
    genop_int_mul = _genop_int_binop("imul")
    genop_int_floordiv = _genop_int_binop("/")
    genop_int_and = _genop_int_binop("&")
    genop_int_or = _genop_int_binop("|")
    genop_int_mod = _genop_int_binop("%")
    genop_int_xor = _genop_int_binop("^")
    genop_int_lshift = _genop_int_binop("<<")
    genop_int_rshift = _genop_int_binop(">>")
    genop_uint_rshift = _genop_int_binop(">>>")

    def _genop_uint_binop(binop):
        def genop_uint_binop(self, op):
            lhs = UIntCast(op.getarg(0))
            rhs = UIntCast(op.getarg(1))
            val = IntBinOp(binop, lhs, rhs)
            self.js.emit_assignment(op.result, val)
        return genop_uint_binop

    genop_uint_lt = _genop_uint_binop("<")
    genop_uint_le = _genop_uint_binop("<=")
    genop_uint_gt = _genop_uint_binop(">")
    genop_uint_ge = _genop_uint_binop(">=")

    def genop_uint_floordiv(self, op):
        lhs = UIntCast(op.getarg(0))
        rhs = UIntCast(op.getarg(1))
        val = UIntCast(IntBinOp("/", lhs, rhs))
        self.js.emit_assignment(op.result, val)

    def _genop_withguard_int_binop_ovf(dblop, intop):
        def genop_withguard_int_binop_ovf(self, op, guardop):
            assert guardop.is_guard_overflow()
            lhs = op.getarg(0)
            rhs = op.getarg(1)
            resbox = op.result
            # Temporary box to hold intermediate double result.
            dblresbox = BoxFloat()
            # XXX TODO: better way to detect overflow in asmjs?
            # We cast the operands to doubles, do the operation as a double,
            # then see if it remains unchanged when we round-trip through int.
            # The tricky case is multiplication, which we have to do twice
            # because double-mul is not the same as int-mul.
            dblres = DblBinOp(dblop, DblCast(lhs), DblCast(rhs))
            self.js.emit_assignment(dblresbox, dblres)
            if dblop == intop:
                intres = IntUnaryOp("~~", dblresbox)
            else:
                intres = IntBinOp(intop, lhs, rhs)
            self.js.emit_assignment(resbox, intres)
            # If the res makes it back to dbl unchanged, there was no overflow.
            self._prepare_guard_descr(guardop)
            if guardop.getopnum() == rop.GUARD_NO_OVERFLOW:
                test = IntBinOp("==", dblresbox, DblCast(resbox))
            else:
                assert guardop.getopnum() == rop.GUARD_OVERFLOW
                test = IntBinOp("!=", dblresbox, DblCast(resbox))
            self._genop_guard(test, guardop)
            # Discard temporary box.
            self.js.free_variable(dblresbox)
        return genop_withguard_int_binop_ovf

    genop_withguard_int_add_ovf = _genop_withguard_int_binop_ovf("+", "+")
    genop_withguard_int_sub_ovf = _genop_withguard_int_binop_ovf("-", "-")
    genop_withguard_int_mul_ovf = _genop_withguard_int_binop_ovf("*", "imul")

    def genop_int_force_ge_zero(self, op):
        arg = op.getarg(0)
        less_than_zero = IntBinOp("<", arg, CONST_ZERO)
        self.js.emit("if(")
        self.js.emit_value(less_than_zero)
        self.js.emit("){\n")
        self.js.emit_assignment(op.result, CONST_ZERO)
        self.js.emit("}else{\n")
        self.js.emit_assignment(op.result, arg)
        self.js.emit("}\n")

    genop_ptr_eq = genop_int_eq
    genop_ptr_ne = genop_int_ne
    genop_instance_ptr_eq = genop_ptr_eq
    genop_instance_ptr_ne = genop_ptr_ne

    def genop_same_as(self, op):
        self.js.emit_assignment(op.result, op.getarg(0))

    genop_cast_ptr_to_int = genop_same_as
    genop_cast_int_to_ptr = genop_same_as

    def _genop_float_unaryop(operator):
        def genop_float_unaryop(self, op):
            val = DblUnaryOp(operator, op.getarg(0))
            self.js.emit_assignment(op.result, val)
        return genop_float_unaryop

    genop_float_neg = _genop_float_unaryop("-")

    def genop_float_abs(self, op):
        zero = ConstFloat(longlong.getfloatstorage(0.0))
        self.js.emit("if(")
        self.js.emit_value(IntBinOp("<", op.getarg(0), zero))
        self.js.emit("){\n")
        self.js.emit_assignment(op.result, DblUnaryOp("-", op.getarg(0)))
        self.js.emit("} else {\n")
        self.js.emit_assignment(op.result, op.getarg(0))
        self.js.emit("}\n")

    def _genop_float_binop(binop):
        def genop_float_binop(self, op):
            val = DblBinOp(binop, op.getarg(0), op.getarg(1))
            self.js.emit_assignment(op.result, val)
        return genop_float_binop

    genop_float_add = _genop_float_binop("+")
    genop_float_sub = _genop_float_binop("-")
    genop_float_mul = _genop_float_binop("*")
    genop_float_truediv = _genop_float_binop("/")

    genop_float_le = _genop_int_binop("<=")  # float args, but int result
    genop_float_eq = _genop_int_binop("==")
    genop_float_ne = _genop_int_binop("!=")
    genop_float_gt = _genop_int_binop(">")
    genop_float_ge = _genop_int_binop(">=")

    def genop_convert_float_bytes_to_longlong(self, op):
        self.js.emit_assignment(op.result, op.getarg(0))

    def genop_convert_longlong_bytes_to_float(self, op):
        self.js.emit_assignment(op.result, op.getarg(0))

    def genop_cast_float_to_int(self, op):
        self.js.emit_assignment(op.result, IntCast(op.getarg(0)))

    def genop_cast_int_to_float(self, op):
        self.js.emit_assignment(op.result, DblCast(op.getarg(0)))

    def genop_read_timestamp(self, op):
        # Simulate processor time using gettimeofday().
        # XXX TODO: Probably this is all sorts of technically incorrect.
        # It needs to write into the heap, so we use the frame as scratch.
        addr = JitFrameAddr_base()
        self._ensure_frame_depth(2)
        self.js.emit_call("gettimeofday", "ii", [addr], None)
        secs = HeapData(Int32, addr)
        micros = HeapData(Int32, IntBinOp("+", addr, ConstInt(WORD)))
        millis = IntBinOp("/", micros, ConstInt(1000))
        millis = IntBinOp("+", millis, IntBinOp("imul", secs, ConstInt(1000)))
        self.js.emit_assignment(op.result, millis)

    #
    # Calls and Jumps and Exits, Oh My!
    #

    def genop_call(self, op):
        descr = op.getdescr()
        assert isinstance(descr, CallDescr)
        assert op.numargs() == len(descr.arg_classes) + 1
        addr = op.getarg(0)
        args = []
        i = 1
        while i < op.numargs():
            args.append(op.getarg(i))
            i += 1
        self._genop_call(op, descr, addr, args)

    def genop_call_malloc_gc(self, op):
        descr = op.getdescr()
        assert isinstance(descr, CallDescr)
        assert op.numargs() == len(descr.arg_classes) + 1
        addr = op.getarg(0)
        args = []
        i = 1
        while i < op.numargs():
            args.append(op.getarg(i))
            i += 1
        self._genop_call(op, descr, addr, args)
        self.js.emit("if(")
        self.js.emit_value(IntBinOp("==", op.result, CONST_ZERO))
        self.js.emit("){\n")
        self._genop_check_and_propagate_exception()
        self.js.emit("}\n")

    def genop_cond_call(self, op):
        descr = op.getdescr()
        assert isinstance(descr, CallDescr)
        assert op.numargs() == len(descr.arg_classes) + 2
        cond = op.getarg(0)
        addr = op.getarg(1)
        args = []
        i = 2
        while i < op.numargs():
            args.append(op.getarg(i))
            i += 1
        self.js.emit("if(")
        self.js.emit_value(cond)
        self.js.emit("){\n")
        self._genop_call(op, descr, addr, args)
        self.js.emit("}\n")

    def genop_withguard_call_may_force(self, op, guardop):
        descr = op.getdescr()
        assert isinstance(descr, CallDescr)
        assert op.numargs() == len(descr.arg_classes) + 1
        addr = op.getarg(0)
        args = []
        i = 1
        while i < op.numargs():
            args.append(op.getarg(i))
            i += 1
        with ctx_guard_not_forced(self, guardop):
            self._genop_call(op, descr, addr, args)

    def genop_withguard_call_release_gil(self, op, guardop):
        descr = op.getdescr()
        assert isinstance(descr, CallDescr)
        assert op.numargs() == len(descr.arg_classes) + 1
        addr = op.getarg(0)
        args = []
        i = 1
        while i < op.numargs():
            args.append(op.getarg(i))
            i += 1
        with ctx_guard_not_forced(self, guardop):
            self.js.emit_dyncall(ConstInt(self.release_gil_addr), "v", [])
            self._genop_call(op, descr, addr, args)
            self.js.emit_dyncall(ConstInt(self.reacquire_gil_addr), "v", [])

    def genop_withguard_call_assembler(self, op, guardop):
        descr = op.getdescr()
        assert isinstance(descr, JitCellToken)
        frame = op.getarg(0)
        if op.numargs() == 2:
            virtref = op.getarg(1)
        else:
            virtref = CONST_ZERO
        jd = descr.outermost_jitdriver_sd
        assert jd is not None
        with ctx_guard_not_forced(self, guardop):
            # The GC-rewrite pass has allocated a frame and populated it.
            # Use the execute-trampoline helper to execute things to completion.
            # This may produce a new frame object, capture it in a temp box.
            exeaddr = self.execute_trampoline_addr
            funcid = descr.compiled_loop_token.compiled_function_id
            args = [ConstInt(funcid), frame]
            resbox = BoxPtr()
            with ctx_allow_gc(self, exclude=[resbox]):
                self.js.emit_dyncall(ConstInt(exeaddr), "iii", args, resbox)
            # Load the descr resulting from that call.
            offset = self.cpu.get_ofs_of_frame_field('jf_descr')
            resdescr = HeapData(Int32, IntBinOp("+", resbox, ConstInt(offset)))
            # Check if it's equal to done-with-this-frame.
            # The particular brand of DWTF depends on the result type.
            if op.result is None:
                dwtf = self.cpu.done_with_this_frame_descr_void
            else:
                kind = op.result.type
                if kind == INT:
                    dwtf = self.cpu.done_with_this_frame_descr_int
                elif kind == REF:
                    dwtf = self.cpu.done_with_this_frame_descr_ref
                elif kind == FLOAT:
                    dwtf = self.cpu.done_with_this_frame_descr_float
                else:
                    raise AssertionError(kind)
            gcref = cast_instance_to_gcref(dwtf)
            rgc._make_sure_does_not_move(gcref)
            dwtf = rffi.cast(lltype.Signed, gcref)
            self.js.emit("if(")
            self.js.emit_value(IntBinOp("==", resdescr, ConstInt(dwtf)))
            self.js.emit("){\n")
            # If so, then we're on the happy fast path.
            # Reset the vable token  (whatever the hell that means...)
            # and return the result from the frame.
            if jd.index_of_virtualizable >= 0:
                fielddescr = jd.vable_token_descr
                assert isinstance(fielddescr, FieldDescr)
                fieldaddr = IntBinOp("+", virtref, ConstInt(fielddescr.offset)) 
                self.js.emit_store(CONST_ZERO, fieldaddr, Int32)
            if op.result is not None:
                kind = op.result.type
                descr = self.cpu.getarraydescr_for_frame(kind)
                offset = self.cpu.unpack_arraydescr(descr)
                addr = IntBinOp("+", resbox, ConstInt(offset))
                self.js.emit_load(op.result, addr, HeapType.from_kind(kind))
            self.js.emit("}else{\n")
            # If not, then we need to invoke a helper function.
            if op.result is None:
                callsig = "vii"
            elif op.result.type == FLOAT:
                callsig = "fii"
            else:
                callsig = "iii"
            args = [resbox, virtref]
            helpaddr = self.cpu.cast_adr_to_int(jd.assembler_helper_adr)
            helpaddr = ConstInt(helpaddr)
            with ctx_allow_gc(self, exclude=[op.result]):
                self.js.emit_dyncall(helpaddr, callsig, args, op.result)
            self.js.emit("}\n")
            self.js.free_variable(resbox)

    def _genop_call(self, op, descr, addr, args):
        assert isinstance(descr, CallDescr)
        assert len(descr.arg_classes) == len(args)
        # Map CallDescr type tags into dynCall type tags.
        sigmap = {"i": "i", "r": "i", "f": "f", "v": "v"}
        callsig = sigmap[descr.result_type]
        i = 0
        while i < len(args):
            callsig += sigmap[descr.arg_classes[i]]
            i += 1
        with ctx_allow_gc(self, exclude=[op.result]):
            self.js.emit_dyncall(addr, callsig, args, op.result)
        # If the result is a less-than-full-sized integer,
        # mask off just the bits we expect.
        # XXX TODO: is this only required for the ctypes testing setup?
        if descr.result_type == "i" and descr.result_size < WORD:
            mask = ConstInt((2 << (8 * descr.result_size - 1)) - 1)
            self.js.emit_assignment(op.result, IntBinOp("&", op.result, mask))

    def genop_force_token(self, op):
        self.js.emit_assignment(op.result, JitFrameAddr())

    def genop_jump(self, op):
        descr = op.getdescr()
        assert isinstance(descr, TargetToken)
        # For jumps to local loop, we can send arguments via boxes.
        if descr == self.js.loop_entry_token:
            argboxes = descr._asmjs_argboxes
            assert len(argboxes) == op.numargs()
            # We're going to use a bunch of temporary boxes to swap the
            # the new values of the variables into the old, so that we don't
            # mess with the state of any boxes that we might need for a future
            # assignment.  This is the only place where a box can change value.
            tempboxes = [None] * op.numargs()
            for i in range(op.numargs()):
                box = op.getarg(i)
                if argboxes[i] != box:
                    if box.type == FLOAT:
                        tempboxes[i] = BoxFloat()
                    else:
                        tempboxes[i] = BoxInt()
                    self.js.emit_assignment(tempboxes[i], box)
            # Now copy each tempbox into the target variable for the argbox.
            for i in range(op.numargs()):
                box = op.getarg(i)
                if argboxes[i] != box:
                    self.js.emit_assignment(argboxes[i], tempboxes[i])
        # For jumps to a different function, we spill to the frame.
        # Each label has logic to load from frame when it's the jump target.
        else:
            arguments = op.getarglist()
            self._genop_write_output_args(arguments)
        self.js.emit_jump(descr)

    def genop_finish(self, op):
        descr = op.getdescr()
        fail_descr = cast_instance_to_gcref(descr)
        rgc._make_sure_does_not_move(fail_descr)
        # If there's a return value, write it into the frame.
        if op.numargs() == 1:
            self._genop_write_output_args(op.getarglist())
        # Write the descr into the frame slot.
        addr = JitFrameAddr_descr()
        self.js.emit_store(ConstPtr(fail_descr), addr, Int32)
        self.js.emit_exit()

    #
    # Guard-related things.
    #

    def genop_guard_true(self, op):
        self._prepare_guard_descr(op)
        test = op.getarg(0)
        self._genop_guard(test, op)

    def genop_guard_isnull(self, op):
        self._prepare_guard_descr(op)
        test = IntBinOp("==", op.getarg(0), CONST_ZERO)
        self._genop_guard(test, op)

    def genop_guard_nonnull(self, op):
        self._prepare_guard_descr(op)
        test = op.getarg(0)
        self._genop_guard(test, op)

    def genop_guard_false(self, op):
        self._prepare_guard_descr(op)
        test = IntUnaryOp("!", op.getarg(0))
        self._genop_guard(test, op)

    def genop_guard_value(self, op):
        self._prepare_guard_descr(op)
        test = IntBinOp("==", op.getarg(0), op.getarg(1))
        self._genop_guard(test, op)

    def genop_guard_class(self, op):
        self._prepare_guard_descr(op)
        objptr = op.getarg(0)
        clsptr = op.getarg(1)
        test = self._genval_has_class(objptr, clsptr)
        self._genop_guard(test, op)

    def _genval_has_class(self, objptr, clsptr):
        # If compiled without type pointers, we have to read the "typeid"
        # from the first half-word of the object and compare it to the
        # expected typeid for the class.
        offset = self.cpu.vtable_offset
        if offset is not None:
            objcls = HeapData(Int32, IntBinOp("+", objptr, ConstInt(offset)))
            test = IntBinOp("==", objcls, clsptr)
        else:
            typeid = IntBinOp("&", HeapData(Int32, objptr), ConstInt(0xFFFF))
            test = IntBinOp("==", typeid, ClassPtrTypeID(clsptr))
        return test

    def genop_guard_nonnull_class(self, op):
        self._prepare_guard_descr(op)
        # This is essentially short-circuiting logical or: the guard must
        # fail if the ref is null or it does not have the appropriate class.
        # Since asmjs doesn't have short-circuiting logical operators, we
        # simulate it with a temporary variable.
        objptr = op.getarg(0)
        clsptr = op.getarg(1)
        testbox = BoxInt()
        self.js.emit_assignment(testbox, objptr)
        self.js.emit("if(")
        self.js.emit_value(testbox)
        self.js.emit("){\n")
        has_class = self._genval_has_class(objptr, clsptr)
        self.js.emit_assignment(testbox, has_class)
        self.js.emit("}\n")
        self._genop_guard(testbox, op)
        self.js.free_variable(testbox)

    def genop_guard_exception(self, op):
        self._prepare_guard_descr(op)
        pos_exctyp = ConstInt(self.cpu.pos_exception())
        pos_excval = ConstInt(self.cpu.pos_exc_value())
        exctyp = HeapData(Int32, pos_exctyp)
        excval = HeapData(Int32, pos_excval)
        test = IntBinOp("==", exctyp, op.getarg(0))
        self._genop_guard(test, op)
        if op.result is None:
            self.js.emit_store(excval, JitFrameAddr_guard_exc(), Int32)
        else:
            self.js.emit_assignment(op.result, excval)
        self.js.emit_store(CONST_ZERO, pos_exctyp, Int32)
        self.js.emit_store(CONST_ZERO, pos_excval, Int32)

    def genop_guard_no_exception(self, op):
        self._prepare_guard_descr(op)
        pos_exctyp = ConstInt(self.cpu.pos_exception())
        exctyp = HeapData(Int32, pos_exctyp)
        test = IntBinOp("==", exctyp, CONST_ZERO)
        self._genop_guard(test, op)

    def genop_guard_not_invalidated(self, op):
        self._prepare_guard_descr(op)
        clt = self.current_clt
        offset, size = symbolic.get_field_token(INVALIDATION_COUNTER,
                                                "counter",
                                                self.cpu.translate_support_code)
        assert size == Int32.size
        invalidation = rffi.cast(lltype.Signed, clt.invalidation)
        cur_val = HeapData(Int32, IntBinOp("+", ConstInt(invalidation), ConstInt(offset)))
        orig_val = ConstInt(clt.invalidation.counter)
        test = IntBinOp("==", cur_val, orig_val)
        self._genop_guard(test, op)

    def _prepare_guard_descr(self, op):
        descr = op.getdescr()
        assert isinstance(descr, AbstractFailDescr)
        # Allocate a new function for this guard.
        # The initial implementation just fails back to the runner,
        # but it might get re-compiled into a bridge that does other
        # interesting things.
        guardjs = ASMJSBuilder(self.cpu)
        # Indenting generation of guard js, for visual clarity.
        if True:
            # If there might be an exception, capture it to the frame.
            if self._guard_might_have_exception(op):
                pos_exctyp = ConstInt(self.cpu.pos_exception())
                pos_excval = ConstInt(self.cpu.pos_exc_value())
                exctyp = HeapData(Int32, pos_exctyp)
                excval = HeapData(Int32, pos_excval)
                guardjs.emit("if(")
                guardjs.emit_value(exctyp)
                guardjs.emit("){\n")
                guardjs.emit_store(excval, JitFrameAddr_guard_exc(), Int32)
                guardjs.emit_store(CONST_ZERO, pos_exctyp, Int32)
                guardjs.emit_store(CONST_ZERO, pos_excval, Int32)
                guardjs.emit("}\n")
            # Write the fail_descr into the frame.
            fail_descr = cast_instance_to_gcref(descr)
            addr = JitFrameAddr_descr()
            guardjs.emit_store(ConstPtr(fail_descr), addr, Int32)
            # That's it!
            funcid = self._compile(guardjs)
        # Store it on the guard, so we can re-compile it in future.
        descr._asmjs_funcid = funcid
        descr._asmjs_clt = self.current_clt
        self.current_clt.compiled_guard_funcs.append(descr._asmjs_funcid)
        return descr

    def _genop_guard(self, test, op):
        descr = op.getdescr()
        assert isinstance(descr, AbstractFailDescr)
        assert descr._asmjs_funcid
        # Check guard expression, execute guard function if it's false.
        self.js.emit("if(!(")
        self.js.emit_value(test)
        self.js.emit(")){\n")
        # Write the failargs into the frame.
        # This is the only mechanism we have available for arg passing.
        locations = self._genop_write_output_args(op.getfailargs())
        descr._asmjs_faillocs = locations
        # Execute the separately-compiled function implementing this guard.
        self.js.emit_goto(descr._asmjs_funcid)
        # Close the if-statement.
        self.js.emit("}\n")

    def _guard_might_have_exception(self, op):
        opnum = op.getopnum()
        if opnum == rop.GUARD_EXCEPTION:
            return True
        if opnum == rop.GUARD_NO_EXCEPTION:
            return True
        if opnum == rop.GUARD_NOT_FORCED:
            return True
        return False

    def _genop_check_and_propagate_exception(self):
        if not self.cpu.propagate_exception_descr:
            return
        pos_exctyp = ConstInt(self.cpu.pos_exception())
        pos_excval = ConstInt(self.cpu.pos_exc_value())
        exctyp = HeapData(Int32, pos_exctyp)
        excval = HeapData(Int32, pos_excval)
        self.js.emit("if(")
        self.js.emit_value(exctyp)
        self.js.emit("){\n")
        # Store the exception on the frame, and clear it.
        self.js.emit_store(excval, JitFrameAddr_guard_exc(), Int32)
        self.js.emit_store(CONST_ZERO, pos_exctyp, Int32)
        self.js.emit_store(CONST_ZERO, pos_excval, Int32)
        # Store the special propagate-exception descr on the frame.
        descr = cast_instance_to_gcref(self.cpu.propagate_exception_descr)
        self.js.emit_store(ConstPtr(descr), JitFrameAddr_descr(), Int32)
        # Bail back to the invoking code to deal with it.
        self.js.emit_exit()
        self.js.emit("}\n")

    #
    #  GC-related things.
    #

    def genop_call_malloc_nursery(self, op):
        sizebox = op.getarg(0)
        # Sanity-check for correct alignment.
        assert isinstance(sizebox, ConstInt)
        size = sizebox.getint()
        assert size & (WORD-1) == 0
        self._genop_malloc_nursery(op, sizebox)

    def genop_call_malloc_nursery_varsize_frame(self, op):
        sizebox = op.getarg(0)
        self._genop_malloc_nursery(op, sizebox)

    def _genop_malloc_nursery(self, op, sizebox):
        gc_ll_descr = self.cpu.gc_ll_descr
        # This is essentially an in-lining of MiniMark.malloc_fixedsize_clear()
        nfree_addr = ConstInt(gc_ll_descr.get_nursery_free_addr())
        ntop_addr = ConstInt(gc_ll_descr.get_nursery_top_addr())
        nfree = HeapData(Int32, nfree_addr)
        ntop = HeapData(Int32, ntop_addr)
        # Optimistically, we can just use the space at nursery_free.
        self.js.emit_assignment(op.result, nfree)
        new_nfree = BoxInt()
        self.js.emit_assignment(new_nfree, IntBinOp("+", op.result, sizebox))
        # But we have to check whether we overflowed nursery_top.
        self.js.emit("if(")
        self.js.emit_value(IntBinOp("<=", new_nfree, ntop))
        self.js.emit("){\n")
        # If we didn't, we're all good, just increment nursery_free.
        self.js.emit_store(new_nfree, nfree_addr, Int32)
        self.js.emit("}else{\n")
        # If we did, we have to call into the GC for a collection.
        mallocfn = ConstInt(self.gc_malloc_nursery_addr)
        with ctx_allow_gc(self, exclude=[op.result]):
            self.js.emit_dyncall(mallocfn, "ii", [sizebox], op.result)
        self._genop_check_and_propagate_exception()
        self.js.emit("}\n")
        # Cleanup temp boxes.
        self.js.free_variable(new_nfree)

    def genop_call_malloc_nursery_varsize(self, op):
        gc_ll_descr = self.cpu.gc_ll_descr
        arraydescr = op.getdescr()
        assert isinstance(arraydescr, ArrayDescr)
        if hasattr(gc_ll_descr, 'minimal_size_in_nursery'):
            assert arraydescr.basesize >= gc_ll_descr.minimal_size_in_nursery
        kind = op.getarg(0).getint()
        itemsize = op.getarg(1).getint()
        lengthbox = op.getarg(2)
        assert isinstance(lengthbox, BoxInt)
        # Figure out the total size to be allocated.
        # It's gcheader + basesize + length*itemsize, rounded up to wordsize.
        if hasattr(gc_ll_descr, 'gcheaderbuilder'):
            size_of_header = gc_ll_descr.gcheaderbuilder.size_gc_header
        else:
            size_of_header = WORD 
        constsize = size_of_header + arraydescr.basesize
        if itemsize % WORD == 0:
            num_items = lengthbox
        else:
            assert itemsize < WORD
            items_per_word = ConstInt(WORD / itemsize)
            padding = IntBinOp("%", lengthbox, items_per_word)
            padding = IntBinOp("-", items_per_word, padding)
            num_items = IntBinOp("+", lengthbox, padding)
        calc_totalsize = IntBinOp("imul", num_items, ConstInt(itemsize))
        calc_totalsize = IntBinOp("+", ConstInt(constsize), calc_totalsize)
        totalsize = BoxInt()
        self.js.emit_assignment(totalsize, calc_totalsize)
        # This is essentially an in-lining of MiniMark.malloc_fixedsize_clear()
        nfree_addr = ConstInt(gc_ll_descr.get_nursery_free_addr())
        ntop_addr = ConstInt(gc_ll_descr.get_nursery_top_addr())
        nfree = HeapData(Int32, nfree_addr)
        ntop = HeapData(Int32, ntop_addr)
        maxsize = ConstInt(gc_ll_descr.max_size_of_young_obj - WORD * 2)
        # Optimistically, we can just use the space at nursery_free.
        self.js.emit_assignment(op.result, nfree)
        new_nfree = BoxInt()
        self.js.emit_assignment(new_nfree, IntBinOp("+", op.result, totalsize))
        # But we have to check whether we overflowed nursery_top,
        # or created an object too large for the nursery.
        self.js.emit("if(")
        self.js.emit_value(IntBinOp("&", IntBinOp("<=", new_nfree, ntop),
                                         IntBinOp("<", totalsize, maxsize)))
        self.js.emit("){\n")
        # If we fit in the nursery, we're all good!
        # Increment nursery_free and set type flags on the object.
        self.js.emit_store(new_nfree, nfree_addr, Int32)
        self.js.emit_store(ConstInt(arraydescr.tid), op.result, Int32)
        self.js.emit("}else{\n")
        # If it didn't fit in the nursery, we have to call out to malloc.
        if kind == rewrite.FLAG_ARRAY:
            args = [ConstInt(WORD), ConstInt(arraydescr.tid), lengthbox]
            callsig = "iiii"
            mallocfn = self.gc_malloc_array_addr
        else:
            args = [lengthbox]
            callsig = "ii"
            if kind == rewrite.FLAG_STR:
                mallocfn = self.gc_malloc_str_addr
            else:
                assert kind == rewrite.FLAG_UNICODE
                mallocfn = self.gc_malloc_unicode_addr
        mallocfn = ConstInt(mallocfn)
        with ctx_allow_gc(self, exclude=[op.result]):
            self.js.emit_dyncall(mallocfn, callsig, args, op.result)
        self._genop_check_and_propagate_exception()
        self.js.emit("}\n")
        # That's it!  Cleanup temp boxes.
        self.js.free_variable(new_nfree)
        self.js.free_variable(totalsize)

    def genop_cond_call_gc_wb(self, op):
        assert op.result is None
        self._genop_write_barrier(op.getarglist())

    def genop_cond_call_gc_wb_array(self, op):
        assert op.result is None
        self._genop_write_barrier(op.getarglist(), array=True)

    def _genop_write_barrier(self, arguments, array=False):
        # Decode and grab the necessary function pointer.
        # If it's zero, the GC doesn't need a write barrier here.
        wbdescr = self.cpu.gc_ll_descr.write_barrier_descr
        if not wbdescr:
            return
        if not array:
            wbfunc = wbdescr.get_write_barrier_fn(self.cpu)
        else:
            wbfunc = wbdescr.get_write_barrier_from_array_fn(self.cpu)
        if wbfunc == 0:
            return
        # Here we are inlining a bunch of code from the write-barrier,
        # in a similar way to how it gets inlined in the non-jitted code.
        # The structure of the generated code looks like this for plain
        # objects:
        #
        #    if (obj has JIT_WB_IF_FLAG) {
        #      dynCall(write_barrier, obj)
        #    }
        #
        # And like this for arrays with potential card-marking:
        #
        #    if (obj has JIT_WB_IF_FLAG) {
        #      if (obj doesn't have JIT_WB_CARDS_SET) {
        #        dynCall(write_barrier, obj)
        #        // this might have set JIT_WB_CARDS_SET flag
        #      }
        #      if (obj has JIT_WB_CARDS_SET) {
        #        do the card marking
        #      }
        #    }
        #
        # XXX TODO: would this be neater if split into separate functions?
        #
        card_marking = False
        if array and wbdescr.jit_wb_cards_set != 0:
            assert (wbdescr.jit_wb_cards_set_byteofs ==
                    wbdescr.jit_wb_if_flag_byteofs)
            card_marking = True
        obj = arguments[0]
        flagaddr = IntBinOp("+", obj, ConstInt(wbdescr.jit_wb_if_flag_byteofs))
        flagbyte = HeapData(Int8, flagaddr)
        chk_flag_byte = ConstInt(wbdescr.jit_wb_if_flag_singlebyte)
        flag_needs_wb = IntBinOp("&", flagbyte, chk_flag_byte)
        chk_card_byte = CONST_ZERO
        flag_has_cards = CONST_ZERO
        if card_marking:
            chk_card_byte = ConstInt(wbdescr.jit_wb_cards_set_singlebyte)
            flag_has_cards = IntBinOp("&", flagbyte, chk_card_byte)
            flag_needs_wb = IntBinOp("&", flagbyte,
                                  IntBinOp("|", chk_flag_byte, chk_card_byte))
        # Check if we actually need a WB at all.
        self.js.emit("if(")
        self.js.emit_value(flag_needs_wb)
        self.js.emit("){\n")
        if card_marking:
            # Check whether it's already using card-marking.
            self.js.emit("if(!(")
            self.js.emit_value(flag_has_cards)
            self.js.emit(")){\n")
        # Call the selected write-barrier helper.
        # For arrays, this might set the has-cards flag.
        self.js.emit_dyncall(ConstInt(wbfunc), "vi", [obj], None)
        if card_marking:
            # Closes check of !flag_has_cards
            self.js.emit("}\n")
            # If we are now using card-marking, actually do the marking.
            self.js.emit("if(")
            self.js.emit_value(flag_has_cards)
            self.js.emit("){\n")
            # This is how we decode the array index into a card bit to set.
            # Logic cargo-culted from x86 backend.
            which = arguments[1]
            card_page_shift = ConstInt(wbdescr.jit_wb_card_page_shift)
            byte_index = IntBinOp(">>", which, card_page_shift)
            byte_ofs = IntBinOp(">>", byte_index, ConstInt(3))
            byte_mask = IntBinOp("<<", ConstInt(1),
                                       IntBinOp("&", byte_index, ConstInt(7)))
            # NB: the card area is before the pointer, hence subtraction.
            byte_addr = IntBinOp("-", obj, byte_ofs)
            old_byte_data = HeapData(Int8, byte_addr)
            new_byte_data = IntBinOp("|", old_byte_data, byte_mask)
            self.js.emit_store(new_byte_data, byte_addr, Int8)
            # Closes check of flag_has_cards
            self.js.emit("}\n")
        # Closes check of flag_needs_wb
        self.js.emit("}\n")

    def _genop_store_gcmap(self):
        # If there's nothing spilled, no gcmap is needed.
        if self.spilled_frame_offset == 0:
            self.js.emit_store(CONST_ZERO, JitFrameAddr_gcmap(), Int32)
            return
        # Make a new gcmap sized to match current size of frame.
        # Remember, our offsets are in bytes but the gcmap indexes whole words.
        frame_size = self.spilled_frame_offset // WORD
        gcmap_size = (frame_size // WORD // 8) + 1
        gcmap = lltype.malloc(jitframe.GCMAP, gcmap_size, flavor="raw")
        gcmap = rffi.cast(lltype.Ptr(jitframe.GCMAP), gcmap)
        for i in xrange(gcmap_size):
            gcmap[i] = r_uint(0)
        # Set a bit for every REF that has been spilled.
        num_refs = 0
        for pos, box in self.spilled_frame_values.iteritems():
            if box and box.type == REF:
                pos = pos // WORD
                gcmap[pos // WORD // 8] |= r_uint(1) << (pos % (WORD * 8))
                num_refs += 1
        # Do we actually have any refs?
        # Generate code to store it to the frame, and keep it alive
        # by attaching it to the clt.
        if not num_refs:
            lltype.free(gcmap, flavor="raw")
            self.js.emit_store(CONST_ZERO, JitFrameAddr_gcmap(), Int32)
        else:
            self.current_clt.compiled_gcmaps.append(gcmap)
            gcmapref = ConstInt(self.cpu.cast_ptr_to_int(gcmap))
            self.js.emit_store(gcmapref, JitFrameAddr_gcmap(), Int32)
        # We might have just stored some young pointers into the frame.
        # Emit a write barrier just in case.
        # XXX TODO: x86 backend only does that when reloading after a gc. Why?
        self._genop_write_barrier([JitFrameAddr()])

    def genop_debug_merge_point(self, op):
        pass

    def genop_jit_debug(self, op):
        pass

    def genop_keepalive(self, op):
        pass

    def not_implemented_op_withguard(self, op, guardop):
        self.not_implemented_op(op)
        self.not_implemented_op(guardop)

    def not_implemented_op(self, op):
        """Insert code for an as-yet-unimplemented operation.

        This inserts a comment into the generated code to mark where
        the unimplemented op would have gone, and sets a flag to let us
        know that the trace is incomplete.  It's useful for debugging
        but would never be triggered in the final version.
        """
        self._print_op(op)
        self.has_unimplemented_ops = True
        self.js.emit_comment("NOT IMPLEMENTED: %s" % (op,))
        for i in range(op.numargs()):
            arg = op.getarg(i)
            if isinstance(arg, Box):
                self.js.allocate_variable(arg)
            self.js.emit_comment("    ARG: %s" % (arg,))
        if op.result is not None and isinstance(op.result, Box):
            self.js.allocate_variable(op.result)
        self.js.emit_comment("    RESULT: %s" % (op.result,))

    def _print_op(self, op):
        print "OPERATION:", op
        for i in range(op.numargs()):
            print "  ARG:", op.getarg(i)
        print "  RES:", op.result


class ctx_spill_to_frame(object):

    def __init__(self, assembler):
        self.assembler = assembler
        self.orig_spilled_frame_offset = 0

    def __enter__(self):
        # Remember the current offset, so we can pop all items
        # after this when we exit the context.
        self.orig_spilled_frame_offset = self.assembler.spilled_frame_offset
        return self

    def __exit__(self, exc_typ, exc_val, exc_tb):
        # Pop any items that were pushed in this context.
        orig_offset = self.orig_spilled_frame_offset
        self.assembler._ensure_frame_depth(self.assembler.spilled_frame_offset)
        for pos, box in self.assembler.spilled_frame_values.items():
            if pos >= orig_offset:
                del self.assembler.spilled_frame_values[pos]
                self.assembler.spilled_frame_locations[box].remove(pos)
                if not self.assembler.spilled_frame_locations[box]:
                    del self.assembler.spilled_frame_locations[box]
        # Reset frame metadata to point to restored offset.
        self.assembler.spilled_frame_offset = orig_offset
        self.assembler._genop_store_gcmap()

    def is_spilled(self, box):
        try:
            self.assembler.spilled_frame_locations[box]
        except KeyError:
            return False
        else:
            return True

    def genop_spill_to_frame(self, box, offset=-1):
        typ = HeapType.from_box(box)
        # Allocate it a position at the next available offset.
        # Align each value to a multiple of its size.
        if offset == -1:
            offset = self.assembler.spilled_frame_offset
            alignment = offset % typ.size
            if alignment:
                offset += alignment
        if offset >= self.assembler.spilled_frame_offset:
            self.assembler.spilled_frame_offset = offset + typ.size
        # Generate code to write the value into the frame.
        addr = IntBinOp("+", JitFrameAddr_base(), ConstInt(offset))
        self.assembler.js.emit_store(box, addr, typ)
        # Record where we spilled it.
        if box not in self.assembler.spilled_frame_locations:
            self.assembler.spilled_frame_locations[box] = []
        self.assembler.spilled_frame_locations[box].append(offset)
        self.assembler.spilled_frame_values[offset] = box
        return offset


class ctx_guard_not_forced(ctx_spill_to_frame):

    def __init__(self, assembler, guardop):
        ctx_spill_to_frame.__init__(self, assembler)
        self.guardop = guardop

    def __enter__(self):
        ctx_spill_to_frame.__enter__(self)
        js = self.assembler.js
        # Store the force-descr where forcing code can find it.
        descr = self.assembler._prepare_guard_descr(self.guardop)
        faildescr = ConstPtr(cast_instance_to_gcref(descr))
        js.emit_store(faildescr, JitFrameAddr_force_descr(), Int32)
        # Write the potential failargs into the frame.
        # We have to spill them here because the forcing logic might
        # need to read them out to populate the virtualizable.
        assert self.orig_spilled_frame_offset == 0
        failargs = self.guardop.getfailargs()
        locations = self.assembler._get_frame_locations(failargs)
        assert len(failargs) == len(locations)
        for i in xrange(len(failargs)):
            failarg = failargs[i]
            # Careful, some boxes may not have a value yet.
            if failarg and failarg not in js.box_variables:
                continue
            self.genop_spill_to_frame(failarg, locations[i])
        self.assembler._genop_store_gcmap()
        return self

    def __exit__(self, exc_typ, exc_val, exc_tb):
        # Emit the guard check, testing for whether jf_descr has been set.
        descr = HeapData(Int32, JitFrameAddr_descr())
        test = IntBinOp("==", descr, CONST_ZERO)
        self.assembler._genop_guard(test, self.guardop)
        # It's now safe to pop from the frame as usual.
        ctx_spill_to_frame.__exit__(self, exc_typ, exc_val, exc_tb)


class ctx_allow_gc(ctx_spill_to_frame):

    def __init__(self, assembler, exclude=None):
        ctx_spill_to_frame.__init__(self, assembler)
        self.exclude = exclude

    def __enter__(self):
        ctx_spill_to_frame.__enter__(self)
        js = self.assembler.js
        # Spill any active REF boxes into the frame.
        for box in js.box_variables:
            if not box or box.type != REF:
                continue
            if self.exclude is not None and box in self.exclude:
                continue
            if not self.is_spilled(box):
                self.genop_spill_to_frame(box)
        self.assembler._genop_store_gcmap()
        # Push the jitframe itself onto the gc shadowstack.
        # We do the following:
        #   * get a pointer to the pointer to the root-stack top.
        #   * deref it to get the root-stack top, and write the frame there.
        #   * in-place increment root-stack top via its pointer.
        gcrootmap = self.assembler.cpu.gc_ll_descr.gcrootmap
        if gcrootmap and gcrootmap.is_shadow_stack:
            rstaddr = ConstInt(gcrootmap.get_root_stack_top_addr())
            rst = HeapData(Int32, rstaddr)
            js.emit_store(JitFrameAddr(), rst, Int32)
            newrst = IntBinOp("+", rst, ConstInt(WORD))
            js.emit_store(newrst, rstaddr, Int32)
        return self

    def __exit__(self, exc_typ, exc_val, exc_tb):
        js = self.assembler.js
        # Pop the jitframe from the root-stack.
        # This is an in-place decrement of root-stack top.
        gcrootmap = self.assembler.cpu.gc_ll_descr.gcrootmap
        if gcrootmap and gcrootmap.is_shadow_stack:
            rstaddr = ConstInt(gcrootmap.get_root_stack_top_addr())
            rst = HeapData(Int32, rstaddr)
            newrst = IntBinOp("-", rst, ConstInt(WORD))
            js.emit_store(newrst, rstaddr, Int32)
            # For moving GCs, the address of the jitframe may have changed.
            # Read the possibly-updated address out of root-stack top.
            # NB: this instruction re-evaluates the HeapData expression in rst.
            js.emit_assign_jitframe(HeapData(Int32, rst))
        # Similarly, read potential new addresss of any spilled boxes.
        # XXX TODO: don't double-load boxes that appear multiple times.
        for pos, box in self.assembler.spilled_frame_values.iteritems():
            if not box or box.type != REF:
                continue
            if self.exclude is not None and box in self.exclude:
                continue
            addr = IntBinOp("+", JitFrameAddr_base(), ConstInt(pos))
            js.emit_load(box, addr, Int32)
        # It's now safe to pop from the frame as usual.
        ctx_spill_to_frame.__exit__(self, exc_typ, exc_val, exc_tb)
        # XXX TODO x86 backend seems to put a wb here.  Why?
        self.assembler._genop_write_barrier([JitFrameAddr()])


# Build a dispatch table mapping opnums to the method that emits code for them.
# There are two different method signatures, depending on whether we can work
# with a single opcode or require an opcode/guard pair.

genop_list = [AssemblerASMJS.not_implemented_op] * rop._LAST
genop_withguard_list = [AssemblerASMJS.not_implemented_op_withguard] * rop._LAST

for name, value in AssemblerASMJS.__dict__.iteritems():
    if name.startswith('genop_withguard_'):
        opname = name[len('genop_withguard_'):]
        num = getattr(rop, opname.upper())
        genop_withguard_list[num] = value
    elif name.startswith('genop_'):
        opname = name[len('genop_'):]
        num = getattr(rop, opname.upper())
        genop_list[num] = value
