
""" Only the JIT
"""

from rpython.rlib import jit
from rpython.jit.codewriter.policy import JitPolicy

driver = jit.JitDriver(greens = [], reds = 'auto')
driver2 = jit.JitDriver(greens = [], reds = 'auto')

def main(count):
    i = 0
    l = []
    while i < count:
        driver.jit_merge_point()
        l.append(i)
        i += 1
    l = main2(l, count)
    return l

def main2(l, count):
    i = 0
    while i < count:
        driver2.jit_merge_point()
        l.pop()
        i += 1
    return l

def entry_point(argv):
    if len(argv) < 3:
        print "Usage: jitstandalone <number> <another number>"
    count1 = int(argv[1])
    count2 = int(argv[2])
    s = 0
    for i in range(count1):
        s += len(main(count2))
    print s
    return 0

def target(*args):
    return entry_point, None

def jitpolicy(driver):
    return JitPolicy()
