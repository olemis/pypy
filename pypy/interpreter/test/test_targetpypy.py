from pypy.goal.targetpypystandalone import get_entry_point, create_entry_point
from pypy.config.pypyoption import get_pypy_config
from rpython.rtyper.lltypesystem import rffi, lltype

class TestTargetPyPy(object):
    def test_run(self):
        config = get_pypy_config(translating=False)
        entry_point = get_entry_point(config)[0]
        entry_point(['pypy-c' , '-S', '-c', 'print 3'])

def test_exeucte_source(space):
    _, d = create_entry_point(space, None)
    execute_source = d['pypy_execute_source']
    lls = rffi.str2charp("import sys; sys.modules['xyz'] = 3")
    execute_source(lls)
    lltype.free(lls, flavor='raw')
    x = space.int_w(space.getitem(space.getattr(space.builtin_modules['sys'],
                                                space.wrap('modules')),
                                                space.wrap('xyz')))
    assert x == 3
    lls = rffi.str2charp("sys")
    execute_source(lls)
    lltype.free(lls, flavor='raw')
    # did not crash - the same globals
    pypy_setup_home = d['pypy_setup_home']
    lls = rffi.str2charp(__file__)
    pypy_setup_home(lls, 1)
    lltype.free(lls, flavor='raw')
