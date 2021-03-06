from pypy.interpreter.typedef import TypeDef
from pypy.interpreter.baseobjspace import W_Root
from pypy.interpreter.gateway import interp2app, unwrap_spec, WrappedDefault
from pypy.interpreter.error import OperationError, wrap_oserror
from rpython.rlib import rpoll
import errno

defaultevents = rpoll.POLLIN | rpoll.POLLOUT | rpoll.POLLPRI

class Cache:
    def __init__(self, space):
        self.w_error = space.new_exception_class("select.error")

def poll(space):
    """Returns a polling object, which supports registering and
unregistering file descriptors, and then polling them for I/O events."""
    return Poll()

class Poll(W_Root):
    def __init__(self):
        self.fddict = {}

    @unwrap_spec(events=int)
    def register(self, space, w_fd, events=defaultevents):
        fd = space.c_filedescriptor_w(w_fd)
        self.fddict[fd] = events

    @unwrap_spec(events=int)
    def modify(self, space, w_fd, events):
        fd = space.c_filedescriptor_w(w_fd)
        if fd not in self.fddict:
            raise wrap_oserror(space, OSError(errno.ENOENT, "poll.modify"),
                               exception_name='w_IOError')
        self.fddict[fd] = events

    def unregister(self, space, w_fd):
        fd = space.c_filedescriptor_w(w_fd)
        try:
            del self.fddict[fd]
        except KeyError:
            raise OperationError(space.w_KeyError,
                                 space.wrap(fd)) # XXX should this maybe be w_fd?

    @unwrap_spec(w_timeout = WrappedDefault(None))
    def poll(self, space, w_timeout):
        if space.is_w(w_timeout, space.w_None):
            timeout = -1
        else:
            # we want to be compatible with cpython and also accept things
            # that can be casted to integer (I think)
            try:
                # compute the integer
                timeout = space.int_w(space.int(w_timeout))
            except (OverflowError, ValueError):
                raise OperationError(space.w_ValueError,
                                     space.wrap("math range error"))

        try:
            retval = rpoll.poll(self.fddict, timeout)
        except rpoll.PollError, e:
            w_errortype = space.fromcache(Cache).w_error
            message = e.get_msg()
            raise OperationError(w_errortype,
                                 space.newtuple([space.wrap(e.errno),
                                                 space.wrap(message)]))

        retval_w = []
        for fd, revents in retval:
            retval_w.append(space.newtuple([space.wrap(fd),
                                            space.wrap(revents)]))
        return space.newlist(retval_w)

pollmethods = {}
for methodname in 'register modify unregister poll'.split():
    pollmethods[methodname] = interp2app(getattr(Poll, methodname))
Poll.typedef = TypeDef('select.poll', **pollmethods)

# ____________________________________________________________


from rpython.rlib import _rsocket_rffi as _c
from rpython.rtyper.lltypesystem import lltype, rffi


def _build_fd_set(space, list_w, ll_list, nfds):
    _c.FD_ZERO(ll_list)
    fdlist = []
    for w_f in list_w:
        fd = space.c_filedescriptor_w(w_f)
        if fd > nfds:
            nfds = fd
        _c.FD_SET(fd, ll_list)
        fdlist.append(fd)
    return fdlist, nfds
_build_fd_set._always_inline_ = True    # get rid of the tuple result

def _unbuild_fd_set(space, list_w, fdlist, ll_list, reslist_w):
    for i in range(len(fdlist)):
        fd = fdlist[i]
        if _c.FD_ISSET(fd, ll_list):
            reslist_w.append(list_w[i])

def _call_select(space, iwtd_w, owtd_w, ewtd_w,
                 ll_inl, ll_outl, ll_errl, ll_timeval):
    fdlistin  = None
    fdlistout = None
    fdlisterr = None
    nfds = -1
    if ll_inl:
        fdlistin, nfds = _build_fd_set(space, iwtd_w, ll_inl, nfds)
    if ll_outl:
        fdlistout, nfds = _build_fd_set(space, owtd_w, ll_outl, nfds)
    if ll_errl:
        fdlisterr, nfds = _build_fd_set(space, ewtd_w, ll_errl, nfds)

    res = _c.select(nfds + 1, ll_inl, ll_outl, ll_errl, ll_timeval)

    if res < 0:
        errno = _c.geterrno()
        msg = _c.socket_strerror_str(errno)
        w_errortype = space.fromcache(Cache).w_error
        raise OperationError(w_errortype, space.newtuple([
            space.wrap(errno), space.wrap(msg)]))

    resin_w = []
    resout_w = []
    reserr_w = []
    if res > 0:
        if fdlistin is not None:
            _unbuild_fd_set(space, iwtd_w, fdlistin,  ll_inl,  resin_w)
        if fdlistout is not None:
            _unbuild_fd_set(space, owtd_w, fdlistout, ll_outl, resout_w)
        if fdlisterr is not None:
            _unbuild_fd_set(space, ewtd_w, fdlisterr, ll_errl, reserr_w)
    return space.newtuple([space.newlist(resin_w),
                           space.newlist(resout_w),
                           space.newlist(reserr_w)])

@unwrap_spec(w_timeout = WrappedDefault(None))
def select(space, w_iwtd, w_owtd, w_ewtd, w_timeout):
    """Wait until one or more file descriptors are ready for some kind of I/O.
The first three arguments are sequences of file descriptors to be waited for:
rlist -- wait until ready for reading
wlist -- wait until ready for writing
xlist -- wait for an ``exceptional condition''
If only one kind of condition is required, pass [] for the other lists.
A file descriptor is either a socket or file object, or a small integer
gotten from a fileno() method call on one of those.

The optional 4th argument specifies a timeout in seconds; it may be
a floating point number to specify fractions of seconds.  If it is absent
or None, the call will never time out.

The return value is a tuple of three lists corresponding to the first three
arguments; each contains the subset of the corresponding file descriptors
that are ready.

*** IMPORTANT NOTICE ***
On Windows, only sockets are supported; on Unix, all file descriptors.
"""

    iwtd_w = space.listview(w_iwtd)
    owtd_w = space.listview(w_owtd)
    ewtd_w = space.listview(w_ewtd)

    if space.is_w(w_timeout, space.w_None):
        timeout = -1.0
    else:
        timeout = space.float_w(w_timeout)

    ll_inl  = lltype.nullptr(_c.fd_set.TO)
    ll_outl = lltype.nullptr(_c.fd_set.TO)
    ll_errl = lltype.nullptr(_c.fd_set.TO)
    ll_timeval = lltype.nullptr(_c.timeval)

    try:
        if len(iwtd_w) > 0:
            ll_inl = lltype.malloc(_c.fd_set.TO, flavor='raw')
        if len(owtd_w) > 0:
            ll_outl = lltype.malloc(_c.fd_set.TO, flavor='raw')
        if len(ewtd_w) > 0:
            ll_errl = lltype.malloc(_c.fd_set.TO, flavor='raw')
        if timeout >= 0.0:
            ll_timeval = rffi.make(_c.timeval)
            i = int(timeout)
            rffi.setintfield(ll_timeval, 'c_tv_sec', i)
            rffi.setintfield(ll_timeval, 'c_tv_usec', int((timeout-i)*1000000))

        # Call this as a separate helper to avoid a large piece of code
        # in try:finally:.  Needed for calling further _always_inline_
        # helpers like _build_fd_set().
        return _call_select(space, iwtd_w, owtd_w, ewtd_w,
                            ll_inl, ll_outl, ll_errl, ll_timeval)
    finally:
        if ll_timeval: lltype.free(ll_timeval, flavor='raw')
        if ll_errl:    lltype.free(ll_errl, flavor='raw')
        if ll_outl:    lltype.free(ll_outl, flavor='raw')
        if ll_inl:     lltype.free(ll_inl, flavor='raw')
