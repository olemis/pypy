import sys
from pypy.rpython.objectmodel import Symbolic, ComputedIntSymbolic
from pypy.rpython.lltypesystem.lltype import *
from pypy.rpython.lltypesystem.llmemory import Address, fakeaddress, \
     AddressOffset, ItemOffset, ArrayItemsOffset, FieldOffset, \
     CompositeOffset, ArrayLengthOffset, WeakGcAddress, fakeweakaddress, \
     GCHeaderOffset
from pypy.rpython.memory.lladdress import NULL
from pypy.translator.c.support import cdecl

# ____________________________________________________________
#
# Primitives

def name_signed(value, db):
    if isinstance(value, Symbolic):
        from pypy.translator.c.gc import REFCOUNT_IMMORTAL
        if isinstance(value, FieldOffset):
            structnode = db.gettypedefnode(value.TYPE)
            return 'offsetof(%s, %s)'%(
                cdecl(db.gettype(value.TYPE), ''),
                structnode.c_struct_field_name(value.fldname))
        elif isinstance(value, ItemOffset):
            return '(sizeof(%s) * %s)'%(
                cdecl(db.gettype(value.TYPE), ''), value.repeat)
        elif isinstance(value, ArrayItemsOffset):
            if isinstance(value.TYPE, FixedSizeArray):
                return '0'
            else:
                return 'offsetof(%s, items)'%(
                    cdecl(db.gettype(value.TYPE), ''))
        elif isinstance(value, ArrayLengthOffset):
            return 'offsetof(%s, length)'%(
                cdecl(db.gettype(value.TYPE), ''))
        elif isinstance(value, CompositeOffset):
            names = [name_signed(item, db) for item in value.offsets]
            return '(%s)' % (' + '.join(names),)
        elif type(value) == AddressOffset:
            return '0'
        elif type(value) == GCHeaderOffset:
            return '0'
        elif type(value) == REFCOUNT_IMMORTAL:
            return 'REFCOUNT_IMMORTAL'
        elif isinstance(value, ComputedIntSymbolic):
            value = value.compute_fn()
        else:
            raise Exception("unimplemented symbolic %r"%value)
    if value == -sys.maxint-1:   # blame C
        return '(-%dL-1L)' % sys.maxint
    else:
        return '%dL' % value

def name_unsigned(value, db):
    assert value >= 0
    return '%dUL' % value

def name_unsignedlonglong(value, db):
    assert value >= 0
    return '%dULL' % value

def name_signedlonglong(value, db):
    return '%dLL' % value

def isinf(x):
    return x != 0.0 and x / 2 == x

def name_float(value, db):
    if isinf(value):
        if value > 0:
            return '(Py_HUGE_VAL)'
        else:
            return '(-Py_HUGE_VAL)'
    else:
        return repr(value)

def name_char(value, db):
    assert type(value) is str and len(value) == 1
    if ' ' <= value < '\x7f':
        return "'%s'" % (value.replace("\\", r"\\").replace("'", r"\'"),)
    else:
        return '%d' % ord(value)

def name_bool(value, db):
    return '%d' % value

def name_void(value, db):
    return '/* nothing */'

def name_unichar(value, db):
    assert type(value) is unicode and len(value) == 1
    return '%d' % ord(value)

def name_address(value, db):
    if value is NULL:
        return 'NULL'
    assert isinstance(value, fakeaddress)
    if value.offset is None:
        if value.ob is None:
            return 'NULL'
        else:
            if isinstance(typeOf(value.ob), ContainerType):
                return db.getcontainernode(value.ob).ptrname
            else:
                return db.get(value.ob)
    else:
        if isinstance(typeOf(value.ob), ContainerType):
            base = db.getcontainernode(value.ob).ptrname
        else:
            base = db.get(value.ob)
        
        return '(void*)(((char*)(%s)) + (%s))'%(base, db.get(value.offset))

def name_weakgcaddress(value, db):
    assert isinstance(value, fakeweakaddress)
    assert value.ref is None # only weak NULL supported
    return 'NULL'


PrimitiveName = {
    Signed:   name_signed,
    SignedLongLong:   name_signedlonglong,
    Unsigned: name_unsigned,
    UnsignedLongLong: name_unsignedlonglong,
    Float:    name_float,
    Char:     name_char,
    UniChar:  name_unichar,
    Bool:     name_bool,
    Void:     name_void,
    Address:  name_address,
    WeakGcAddress:  name_weakgcaddress,
    }

PrimitiveType = {
    Signed:   'long @',
    SignedLongLong:   'long long @',
    Unsigned: 'unsigned long @',
    UnsignedLongLong: 'unsigned long long @',
    Float:    'double @',
    Char:     'char @',
    UniChar:  'unsigned int @',
    Bool:     'char @',
    Void:     'void @',
    Address:  'void* @',
    WeakGcAddress:  'void* @',
    }

PrimitiveErrorValue = {
    Signed:   '-1',
    SignedLongLong:   '-1LL',
    Unsigned: '((unsigned) -1)',
    UnsignedLongLong: '((unsigned long long) -1)',
    Float:    '-1.0',
    Char:     '((char) -1)',
    UniChar:  '((unsigned) -1)',
    Bool:     '((char) -1)',
    Void:     '/* error */',
    Address:  'NULL',
    WeakGcAddress:  'HIDE_POINTER(NULL)',
    }

def define_c_primitive(ll_type, c_name):
    if ll_type in PrimitiveName:
        return
    if ll_type._cast(-1) > 0:
        name_str = '((%s) %%dULL)' % c_name
    else:
        name_str = '((%s) %%dLL)' % c_name
    PrimitiveName[ll_type] = lambda value, db: name_str % value
    PrimitiveType[ll_type] = '%s @'% c_name
    PrimitiveErrorValue[ll_type] = '((%s) -1)'% c_name
    
try:
    import ctypes
except ImportError:
    pass
else:
    from pypy.rpython.rctypes import rcarithmetic as rcarith
    for ll_type, c_name in [(rcarith.CByte, 'signed char'),
                            (rcarith.CUByte, 'unsigned char'),
                            (rcarith.CShort, 'short'),
                            (rcarith.CUShort, 'unsigned short'),
                            (rcarith.CInt, 'int'),
                            (rcarith.CUInt, 'unsigned int'),
                            (rcarith.CLong, 'long'),
                            (rcarith.CULong, 'unsigned long'),
                            (rcarith.CLonglong, 'long long'),
                            (rcarith.CULonglong, 'unsigned long long')]:
        if ll_type in PrimitiveName:
            continue
        PrimitiveName[ll_type] = lambda value, db, c_name=c_name: '((%s) %dULL)' % (c_name, value)
        PrimitiveType[ll_type] = '%s @'% c_name
        PrimitiveErrorValue[ll_type] = '((%s) -1)'% c_name
    
