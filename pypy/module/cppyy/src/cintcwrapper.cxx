#include "cppyy.h"
#include "cintcwrapper.h"

#include "Api.h"

#include "TROOT.h"
#include "TError.h"
#include "TList.h"
#include "TSystem.h"

#include "TApplication.h"
#include "TInterpreter.h"
#include "Getline.h"

#include "TBaseClass.h"
#include "TClass.h"
#include "TClassEdit.h"
#include "TClassRef.h"
#include "TDataMember.h"
#include "TFunction.h"
#include "TGlobal.h"
#include "TMethod.h"
#include "TMethodArg.h"

#include <assert.h>
#include <string.h>
#include <map>
#include <sstream>
#include <string>
#include <utility>


/*  CINT internals (some won't work on Windows) -------------------------- */
extern long G__store_struct_offset;
extern "C" void* G__SetShlHandle(char*);
extern "C" void G__LockCriticalSection();
extern "C" void G__UnlockCriticalSection();

/* ROOT meta internals ---------------------------------------------------- */
namespace {

class Cppyy_OpenedTClass : public TDictionary {
public:
   mutable TObjArray *fStreamerInfo;    //Array of TVirtualStreamerInfo
   mutable std::map<std::string, TObjArray*> *fConversionStreamerInfo; //Array of the streamer infos derived from another class.
   TList             *fRealData;        //linked list for persistent members including base classes
   TList             *fBase;            //linked list for base classes
   TList             *fData;            //linked list for data members
   TList             *fMethod;          //linked list for methods
   TList             *fAllPubData;      //all public data members (including from base classes)
   TList             *fAllPubMethod;    //all public methods (including from base classes)
};

} // unnamed namespace


/* data for life time management ------------------------------------------ */
#define GLOBAL_HANDLE 1l

typedef std::vector<TClassRef> ClassRefs_t;
static ClassRefs_t g_classrefs(1);

typedef std::map<std::string, ClassRefs_t::size_type> ClassRefIndices_t;
static ClassRefIndices_t g_classref_indices;

class ClassRefsInit {
public:
    ClassRefsInit() {   // setup dummy holders for global and std namespaces
        assert(g_classrefs.size() == (ClassRefs_t::size_type)GLOBAL_HANDLE);
        g_classref_indices[""] = (ClassRefs_t::size_type)GLOBAL_HANDLE;
        g_classrefs.push_back(TClassRef(""));
        g_classref_indices["std"] = g_classrefs.size();
        g_classrefs.push_back(TClassRef(""));    // CINT ignores std
        g_classref_indices["::std"] = g_classrefs.size();
        g_classrefs.push_back(TClassRef(""));    // id.
    }
};
static ClassRefsInit _classrefs_init;

typedef std::vector<TFunction> GlobalFuncs_t;
static GlobalFuncs_t g_globalfuncs;

typedef std::vector<TGlobal> GlobalVars_t;
static GlobalVars_t g_globalvars;


/* initialization of th ROOT system (debatable ... ) ---------------------- */
namespace {

class TCppyyApplication : public TApplication {
public:
    TCppyyApplication(const char* acn, Int_t* argc, char** argv, Bool_t do_load = kTRUE)
           : TApplication(acn, argc, argv) {

       // Explicitly load libMathCore as CINT will not auto load it when using one
       // of its globals. Once moved to Cling, which should work correctly, we
       // can remove this statement.
       gSystem->Load("libMathCore");

       if (do_load) {
            // follow TRint to minimize differences with CINT
            ProcessLine("#include <iostream>", kTRUE);
            ProcessLine("#include <_string>",  kTRUE); // for std::string iostream.
            ProcessLine("#include <DllImport.h>", kTRUE);// Defined R__EXTERN
            ProcessLine("#include <vector>",   kTRUE); // needed because they're used within the
            ProcessLine("#include <pair>",     kTRUE); //  core ROOT dicts and CINT won't be able
                                                       //  to properly unload these files
        }

        // save current interpreter context
        gInterpreter->SaveContext();
        gInterpreter->SaveGlobalsContext();

        // prevent crashes on accessing history
        Gl_histinit((char*)"-");

        // prevent ROOT from exiting python
        SetReturnFromRun(kTRUE);

        // enable auto-loader
        gInterpreter->EnableAutoLoading();
    }
};

static const char* appname = "pypy-cppyy";

class ApplicationStarter {
public:
    ApplicationStarter() {
        if (!gApplication) {
            int argc = 1;
            char* argv[1]; argv[0] = (char*)appname;
            gApplication = new TCppyyApplication(appname, &argc, argv, kTRUE);
        }
    }
} _applicationStarter;

} // unnamed namespace


/* local helpers ---------------------------------------------------------- */
static inline char* cppstring_to_cstring(const std::string& name) {
    char* name_char = (char*)malloc(name.size() + 1);
    strcpy(name_char, name.c_str());
    return name_char;
}

static inline char* type_cppstring_to_cstring(const std::string& tname) {
    G__TypeInfo ti(tname.c_str());
    std::string true_name = ti.IsValid() ? ti.TrueName() : tname;
    return cppstring_to_cstring(true_name);
}

static inline TClassRef type_from_handle(cppyy_type_t handle) {
    return g_classrefs[(ClassRefs_t::size_type)handle];
}

static inline TFunction* type_get_method(cppyy_type_t handle, int method_index) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass())
        return (TFunction*)cr->GetListOfMethods()->At(method_index);
    return &g_globalfuncs[method_index];
}


static inline void fixup_args(G__param* libp) {
    for (int i = 0; i < libp->paran; ++i) {
        libp->para[i].ref = libp->para[i].obj.i;
        const char partype = libp->para[i].type;
        if (partype == 'p')
            libp->para[i].obj.i = (long)&libp->para[i].ref;
        else if (partype == 'r')
            libp->para[i].ref = (long)&libp->para[i].obj.i;
        else if (partype == 'f') {
            assert(sizeof(float) <= sizeof(long));
            long val = libp->para[i].obj.i;
            void* pval = (void*)&val;
            libp->para[i].obj.d = *(float*)pval;
        }
    }
}


/* name to opaque C++ scope representation -------------------------------- */
char* cppyy_resolve_name(const char* cppitem_name) {
    if (strcmp(cppitem_name, "") == 0)
        return cppstring_to_cstring(cppitem_name);
    G__TypeInfo ti(cppitem_name);
    if (ti.IsValid()) {
        if (ti.Property() & G__BIT_ISENUM)
            return cppstring_to_cstring("unsigned int");
        return cppstring_to_cstring(ti.TrueName());
    }
    return cppstring_to_cstring(cppitem_name);
}

cppyy_scope_t cppyy_get_scope(const char* scope_name) {
    ClassRefIndices_t::iterator icr = g_classref_indices.find(scope_name);
    if (icr != g_classref_indices.end())
        return (cppyy_type_t)icr->second;

    // use TClass directly, to enable auto-loading
    TClassRef cr(TClass::GetClass(scope_name, kTRUE, kTRUE));
    if (!cr.GetClass())
        return (cppyy_type_t)NULL;

    if (!cr->GetClassInfo())
        return (cppyy_type_t)NULL;

    if (!G__TypeInfo(scope_name).IsValid())
        return (cppyy_type_t)NULL;

    ClassRefs_t::size_type sz = g_classrefs.size();
    g_classref_indices[scope_name] = sz;
    g_classrefs.push_back(TClassRef(scope_name));
    return (cppyy_scope_t)sz;
}

cppyy_type_t cppyy_get_template(const char* template_name) {
    ClassRefIndices_t::iterator icr = g_classref_indices.find(template_name);
    if (icr != g_classref_indices.end())
        return (cppyy_type_t)icr->second;

    if (!G__defined_templateclass((char*)template_name))
        return (cppyy_type_t)NULL;

    // the following yields a dummy TClassRef, but its name can be queried
    ClassRefs_t::size_type sz = g_classrefs.size();
    g_classref_indices[template_name] = sz;
    g_classrefs.push_back(TClassRef(template_name));
    return (cppyy_type_t)sz;
}

cppyy_type_t cppyy_get_object_type(cppyy_type_t klass, cppyy_object_t obj) {
    TClassRef cr = type_from_handle(klass);
    TClass* clActual = cr->GetActualClass( (void*)obj );
    if (clActual && clActual != cr.GetClass()) {
        // TODO: lookup through name should not be needed
        return (cppyy_type_t)cppyy_get_scope(clActual->GetName());
    }
    return klass;
}

/* memory management ------------------------------------------------------ */
cppyy_object_t cppyy_allocate(cppyy_type_t handle) {
    TClassRef cr = type_from_handle(handle);
    return (cppyy_object_t)malloc(cr->Size());
}

void cppyy_deallocate(cppyy_type_t /*handle*/, cppyy_object_t instance) {
    free((void*)instance);
}

void cppyy_destruct(cppyy_type_t handle, cppyy_object_t self) {
    TClassRef cr = type_from_handle(handle);
    cr->Destructor((void*)self, true);
}


/* method/function dispatching -------------------------------------------- */
static inline G__value cppyy_call_T(cppyy_method_t method,
        cppyy_object_t self, int nargs, void* args) {

    G__InterfaceMethod meth = (G__InterfaceMethod)method;
    G__param* libp = (G__param*)((char*)args - offsetof(G__param, para));
    assert(libp->paran == nargs);
    fixup_args(libp);

    G__value result;
    G__setnull(&result);

    G__LockCriticalSection();      // is recursive lock

    // TODO: access to store_struct_offset won't work on Windows
    long store_struct_offset = G__store_struct_offset;
    if (self)
        G__store_struct_offset = (long)self;

    meth(&result, 0, libp, 0);

    if (self)
        G__store_struct_offset = store_struct_offset;

    if (G__get_return(0) > G__RETURN_NORMAL)
        G__security_recover(0);    // 0 ensures silence

    G__UnlockCriticalSection();

    return result;
}

void cppyy_call_v(cppyy_method_t method, cppyy_object_t self, int nargs, void* args) {
    cppyy_call_T(method, self, nargs, args);
}

int cppyy_call_b(cppyy_method_t method, cppyy_object_t self, int nargs, void* args) {
    G__value result = cppyy_call_T(method, self, nargs, args);
    return (bool)G__int(result);
}

char cppyy_call_c(cppyy_method_t method, cppyy_object_t self, int nargs, void* args) {
    G__value result = cppyy_call_T(method, self, nargs, args);
    return (char)G__int(result);
}

short cppyy_call_h(cppyy_method_t method, cppyy_object_t self, int nargs, void* args) {
    G__value result = cppyy_call_T(method, self, nargs, args);
    return (short)G__int(result);
}

int cppyy_call_i(cppyy_method_t method, cppyy_object_t self, int nargs, void* args) {
    G__value result = cppyy_call_T(method, self, nargs, args);
    return (int)G__int(result);
}

long cppyy_call_l(cppyy_method_t method, cppyy_object_t self, int nargs, void* args) {
    G__value result = cppyy_call_T(method, self, nargs, args);
    return G__int(result);
}

double cppyy_call_f(cppyy_method_t method, cppyy_object_t self, int nargs, void* args) {
    G__value result = cppyy_call_T(method, self, nargs, args);
    return G__double(result);
}

double cppyy_call_d(cppyy_method_t method, cppyy_object_t self, int nargs, void* args) {
    G__value result = cppyy_call_T(method, self, nargs, args);
    return G__double(result);
}

void* cppyy_call_r(cppyy_method_t method, cppyy_object_t self, int nargs, void* args) {
    G__value result = cppyy_call_T(method, self, nargs, args);
    return (void*)result.ref;
}

char* cppyy_call_s(cppyy_method_t method, cppyy_object_t self, int nargs, void* args) {
    G__value result = cppyy_call_T(method, self, nargs, args);
    G__pop_tempobject_nodel();
    if (result.ref && *(long*)result.ref) {
        char* charp = cppstring_to_cstring(*(std::string*)result.ref);
        delete (std::string*)result.ref;
        return charp;
    }
    return cppstring_to_cstring("");
}

void cppyy_constructor(cppyy_method_t method, cppyy_object_t self, int nargs, void* args) {
    G__setgvp((long)self);
    cppyy_call_T(method, self, nargs, args);
    G__setgvp((long)G__PVOID);
}

cppyy_object_t cppyy_call_o(cppyy_type_t method, cppyy_object_t self, int nargs, void* args,
                  cppyy_type_t /*result_type*/ ) {
    G__value result = cppyy_call_T(method, self, nargs, args);
    G__pop_tempobject_nodel();
    return G__int(result);
}

cppyy_methptrgetter_t cppyy_get_methptr_getter(cppyy_type_t /*handle*/, int /*method_index*/) {
    return (cppyy_methptrgetter_t)NULL;
}


/* handling of function argument buffer ----------------------------------- */
void* cppyy_allocate_function_args(size_t nargs) {
    assert(sizeof(CPPYY_G__value) == sizeof(G__value));
    G__param* libp = (G__param*)malloc(
        offsetof(G__param, para) + nargs*sizeof(CPPYY_G__value));
    libp->paran = (int)nargs;
    for (size_t i = 0; i < nargs; ++i)
        libp->para[i].type = 'l';
    return (void*)libp->para;
}

void cppyy_deallocate_function_args(void* args) {
    free((char*)args - offsetof(G__param, para));
}

size_t cppyy_function_arg_sizeof() {
    return sizeof(CPPYY_G__value);
}

size_t cppyy_function_arg_typeoffset() {
    return offsetof(CPPYY_G__value, type);
}


/* scope reflection information ------------------------------------------- */
int cppyy_is_namespace(cppyy_scope_t handle) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass() && cr->GetClassInfo())
        return cr->Property() & G__BIT_ISNAMESPACE;
    if (strcmp(cr.GetClassName(), "") == 0)
        return true;
    return false;
}

int cppyy_is_enum(const char* type_name) {
    G__TypeInfo ti(type_name);
    return (ti.Property() & G__BIT_ISENUM);
}


/* type/class reflection information -------------------------------------- */
char* cppyy_final_name(cppyy_type_t handle) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass() && cr->GetClassInfo()) {
        std::string true_name = G__TypeInfo(cr->GetName()).TrueName();
        std::string::size_type pos = true_name.rfind("::");
        if (pos != std::string::npos)
            return cppstring_to_cstring(true_name.substr(pos+2, std::string::npos));
        return cppstring_to_cstring(true_name);
    }
    return cppstring_to_cstring(cr.GetClassName());
}

char* cppyy_scoped_final_name(cppyy_type_t handle) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass() && cr->GetClassInfo()) {
        std::string true_name = G__TypeInfo(cr->GetName()).TrueName();
        return cppstring_to_cstring(true_name);
    }
    return cppstring_to_cstring(cr.GetClassName());
}

int cppyy_has_complex_hierarchy(cppyy_type_t handle) {
// as long as no fast path is supported for CINT, calculating offsets (which
// are cached by the JIT) is not going to hurt 
    return 1;
}

int cppyy_num_bases(cppyy_type_t handle) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass() && cr->GetListOfBases() != 0)
        return cr->GetListOfBases()->GetSize();
    return 0;
}

char* cppyy_base_name(cppyy_type_t handle, int base_index) {
    TClassRef cr = type_from_handle(handle);
    TBaseClass* b = (TBaseClass*)cr->GetListOfBases()->At(base_index);
    return type_cppstring_to_cstring(b->GetName());
}

int cppyy_is_subtype(cppyy_type_t derived_handle, cppyy_type_t base_handle) {
    TClassRef derived_type = type_from_handle(derived_handle);
    TClassRef base_type = type_from_handle(base_handle);
    return derived_type->GetBaseClass(base_type) != 0;
}

size_t cppyy_base_offset(cppyy_type_t derived_handle, cppyy_type_t base_handle, cppyy_object_t address) {
    TClassRef derived_type = type_from_handle(derived_handle);
    TClassRef base_type = type_from_handle(base_handle);

    size_t offset = 0;

    if (derived_type && base_type) {
        G__ClassInfo* base_ci    = (G__ClassInfo*)base_type->GetClassInfo();
        G__ClassInfo* derived_ci = (G__ClassInfo*)derived_type->GetClassInfo();

        if (base_ci && derived_ci) {
#ifdef WIN32
            // Windows cannot cast-to-derived for virtual inheritance
            // with CINT's (or Reflex's) interfaces.
            long baseprop = derived_ci->IsBase(*base_ci);
            if (!baseprop || (baseprop & G__BIT_ISVIRTUALBASE))
                offset = (size_t)derived_type->GetBaseClassOffset(base_type);
            else
#endif
                offset = G__isanybase(base_ci->Tagnum(), derived_ci->Tagnum(), (long)address);
         } else {
             offset = (size_t)derived_type->GetBaseClassOffset(base_type);
         }
    }

    if (offset < 0)      // error return of G__isanybase()
        return 0;

    return offset;
}


/* method/function reflection information --------------------------------- */
int cppyy_num_methods(cppyy_scope_t handle) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass() && cr->GetListOfMethods())
        return cr->GetListOfMethods()->GetSize();
    else if (strcmp(cr.GetClassName(), "") == 0) {
    // NOTE: the updated list of global funcs grows with 5 "G__ateval"'s just
    // because it is being updated => infinite loop! Apply offset to correct ...
        static int ateval_offset = 0;
        TCollection* funcs = gROOT->GetListOfGlobalFunctions(kTRUE);
        ateval_offset += 5;
	if (g_globalfuncs.size() <= (GlobalFuncs_t::size_type)funcs->GetSize() - ateval_offset) {
            g_globalfuncs.clear();
	    g_globalfuncs.reserve(funcs->GetSize());

            TIter ifunc(funcs);

            TFunction* func = 0;
            while ((func = (TFunction*)ifunc.Next())) {
                if (strcmp(func->GetName(), "G__ateval") == 0)
                    ateval_offset += 1;
                else
                    g_globalfuncs.push_back(*func);
            }
        }
	return (int)g_globalfuncs.size();
    }
    return 0;
}

char* cppyy_method_name(cppyy_scope_t handle, int method_index) {
    TFunction* f = type_get_method(handle, method_index);
    return cppstring_to_cstring(f->GetName());
}

char* cppyy_method_result_type(cppyy_scope_t handle, int method_index) {
    TFunction* f = type_get_method(handle, method_index);
    return type_cppstring_to_cstring(f->GetReturnTypeName());
}

int cppyy_method_num_args(cppyy_scope_t handle, int method_index) {
    TFunction* f = type_get_method(handle, method_index);
    return f->GetNargs();
}

int cppyy_method_req_args(cppyy_scope_t handle, int method_index) {
    TFunction* f = type_get_method(handle, method_index);
    return f->GetNargs() - f->GetNargsOpt();
}

char* cppyy_method_arg_type(cppyy_scope_t handle, int method_index, int arg_index) {
    TFunction* f = type_get_method(handle, method_index);
    TMethodArg* arg = (TMethodArg*)f->GetListOfMethodArgs()->At(arg_index);
    return type_cppstring_to_cstring(arg->GetFullTypeName());
}

char* cppyy_method_arg_default(cppyy_scope_t, int, int) {
    /* unused: libffi does not work with CINT back-end */
    return cppstring_to_cstring("");
}

char* cppyy_method_signature(cppyy_scope_t handle, int method_index) {
    TFunction* f = type_get_method(handle, method_index);
    TClassRef cr = type_from_handle(handle);
    std::ostringstream sig;
    if (cr.GetClass() && cr->GetClassInfo()
        && strcmp(f->GetName(), ((G__ClassInfo*)cr->GetClassInfo())->Name()) != 0)
        sig << f->GetReturnTypeName() << " ";
    sig << cr.GetClassName() << "::" << f->GetName() << "(";
    int nArgs = f->GetNargs();
    for (int iarg = 0; iarg < nArgs; ++iarg) {
        sig << ((TMethodArg*)f->GetListOfMethodArgs()->At(iarg))->GetFullTypeName();
        if (iarg != nArgs-1)
            sig << ", ";
    }
    sig << ")" << std::ends;
    return cppstring_to_cstring(sig.str());
}

int cppyy_method_index(cppyy_scope_t handle, const char* name) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass()) {
        gInterpreter->UpdateListOfMethods(cr.GetClass());
        int imeth = 0;
        TFunction* func;
        TIter next(cr->GetListOfMethods());
        while ((func = (TFunction*)next())) {
            if (strcmp(name, func->GetName()) == 0) {
                if (func->Property() & G__BIT_ISPUBLIC)
                    return imeth;
                return -1;
            }
            ++imeth;
        }
    }
    TFunction* func = gROOT->GetGlobalFunction(name, NULL, kTRUE);
    if (!func)
        return -1;
    int idx = g_globalfuncs.size();
    g_globalfuncs.push_back(*func);
    return idx;
}

cppyy_method_t cppyy_get_method(cppyy_scope_t handle, int method_index) {
    TFunction* f = type_get_method(handle, method_index);
    return (cppyy_method_t)f->InterfaceMethod();
}


/* method properties -----------------------------------------------------  */
int cppyy_is_constructor(cppyy_type_t handle, int method_index) {
    TClassRef cr = type_from_handle(handle);
    TMethod* m = (TMethod*)cr->GetListOfMethods()->At(method_index);
    return strcmp(m->GetName(), ((G__ClassInfo*)cr->GetClassInfo())->Name()) == 0;
}

int cppyy_is_staticmethod(cppyy_type_t handle, int method_index) {
    TClassRef cr = type_from_handle(handle);
    TMethod* m = (TMethod*)cr->GetListOfMethods()->At(method_index);
    return m->Property() & G__BIT_ISSTATIC;
}


/* data member reflection information ------------------------------------- */
int cppyy_num_data_members(cppyy_scope_t handle) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass() && cr->GetListOfDataMembers())
        return cr->GetListOfDataMembers()->GetSize();
    else if (strcmp(cr.GetClassName(), "") == 0) {
        TCollection* vars = gROOT->GetListOfGlobals(kTRUE);
       	if (g_globalvars.size() != (GlobalVars_t::size_type)vars->GetSize()) {
            g_globalvars.clear();
	    g_globalvars.reserve(vars->GetSize());

            TIter ivar(vars);

            TGlobal* var = 0;
            while ((var = (TGlobal*)ivar.Next()))
                g_globalvars.push_back(*var);

        }
	return (int)g_globalvars.size();
    }
    return 0;
}

char* cppyy_data_member_name(cppyy_scope_t handle, int data_member_index) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass()) {
        TDataMember* m = (TDataMember*)cr->GetListOfDataMembers()->At(data_member_index);
        return cppstring_to_cstring(m->GetName());
    }
    TGlobal& gbl = g_globalvars[data_member_index];
    return cppstring_to_cstring(gbl.GetName());
}

char* cppyy_data_member_type(cppyy_scope_t handle, int data_member_index) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass())  {
        TDataMember* m = (TDataMember*)cr->GetListOfDataMembers()->At(data_member_index);
        std::string fullType = m->GetFullTypeName();
        if ((int)m->GetArrayDim() > 1 || (!m->IsBasic() && m->IsaPointer()))
            fullType.append("*");
        else if ((int)m->GetArrayDim() == 1) {
            std::ostringstream s;
            s << '[' << m->GetMaxIndex(0) << ']' << std::ends;
            fullType.append(s.str());
        }
        return cppstring_to_cstring(fullType);
    }
    TGlobal& gbl = g_globalvars[data_member_index];
    return cppstring_to_cstring(gbl.GetFullTypeName());
}

size_t cppyy_data_member_offset(cppyy_scope_t handle, int data_member_index) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass()) {
        TDataMember* m = (TDataMember*)cr->GetListOfDataMembers()->At(data_member_index);
        return (size_t)m->GetOffsetCint();
    }
    TGlobal& gbl = g_globalvars[data_member_index];
    return (size_t)gbl.GetAddress();
}

int cppyy_data_member_index(cppyy_scope_t handle, const char* name) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass()) {
        // called from updates; add a hard reset as the code itself caches in
        // Class (TODO: by-pass ROOT/meta)
        Cppyy_OpenedTClass* c = (Cppyy_OpenedTClass*)cr.GetClass();
        if (c->fData) {
            c->fData->Delete();
            delete c->fData; c->fData = 0;
            delete c->fAllPubData; c->fAllPubData = 0;
        }
        // the following appears dumb, but TClass::GetDataMember() does a linear
        // search itself, so there is no gain
        int idm = 0;
        TDataMember* dm;
        TIter next(cr->GetListOfDataMembers());
        while ((dm = (TDataMember*)next())) {
            if (strcmp(name, dm->GetName()) == 0) {
                if (dm->Property() & G__BIT_ISPUBLIC)
                    return idm;
                return -1;
            }
            ++idm;
        }
    }
    TGlobal* gbl = (TGlobal*)gROOT->GetListOfGlobals(kTRUE)->FindObject(name);
    if (!gbl)
        return -1;
    int idx = g_globalvars.size();
    g_globalvars.push_back(*gbl);
    return idx;
}


/* data member properties ------------------------------------------------  */
int cppyy_is_publicdata(cppyy_scope_t handle, int data_member_index) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass()) {
        TDataMember* m = (TDataMember*)cr->GetListOfDataMembers()->At(data_member_index);
        return m->Property() & G__BIT_ISPUBLIC;
    }
    return 1;  // global data is always public
}

int cppyy_is_staticdata(cppyy_scope_t handle, int data_member_index) {
    TClassRef cr = type_from_handle(handle);
    if (cr.GetClass()) {
        TDataMember* m = (TDataMember*)cr->GetListOfDataMembers()->At(data_member_index);
        return m->Property() & G__BIT_ISSTATIC;
    }
    return 1;  // global data is always static
}


/* misc helpers ----------------------------------------------------------- */
long long cppyy_strtoll(const char* str) {
    return strtoll(str, NULL, 0);
}

extern "C" unsigned long long cppyy_strtoull(const char* str) {
    return strtoull(str, NULL, 0);
}

void cppyy_free(void* ptr) {
    free(ptr);
}

cppyy_object_t cppyy_charp2stdstring(const char* str) {
    return (cppyy_object_t)new std::string(str);
}

cppyy_object_t cppyy_stdstring2stdstring(cppyy_object_t ptr) {
    return (cppyy_object_t)new std::string(*(std::string*)ptr);
}

void cppyy_free_stdstring(cppyy_object_t ptr) {
    delete (std::string*)ptr;
}

void* cppyy_load_dictionary(const char* lib_name) {
    if (0 <= gSystem->Load(lib_name))
        return (void*)1;
    return (void*)0;
}
