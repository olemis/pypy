from rpython.rtyper.lltypesystem import rffi, lltype

# shared ll definitions
_C_OPAQUE_PTR = rffi.LONG
_C_OPAQUE_NULL = lltype.nullptr(rffi.LONGP.TO)# ALT: _C_OPAQUE_PTR.TO

C_SCOPE = _C_OPAQUE_PTR
C_NULL_SCOPE = rffi.cast(C_SCOPE, _C_OPAQUE_NULL)

C_TYPE = C_SCOPE
C_NULL_TYPE = C_NULL_SCOPE

C_OBJECT = _C_OPAQUE_PTR
C_NULL_OBJECT = rffi.cast(C_OBJECT, _C_OPAQUE_NULL)

C_METHOD = _C_OPAQUE_PTR
C_INDEX = rffi.LONG
C_INDEX_ARRAY = rffi.LONGP
WLAVC_INDEX = rffi.LONG

C_METHPTRGETTER = lltype.FuncType([C_OBJECT], rffi.VOIDP)
C_METHPTRGETTER_PTR = lltype.Ptr(C_METHPTRGETTER)
