from rpython.jit.backend.llsupport.test.ztranslation_test import TranslationRemoveTypePtrTest
from rpython.translator.translator import TranslationContext
from rpython.config.translationoption import DEFL_GC

class TestTranslationRemoveTypePtrX86(TranslationRemoveTypePtrTest):
    def _get_TranslationContext(self):
        t = TranslationContext()
        t.config.translation.gc = DEFL_GC   # 'hybrid' or 'minimark'
        t.config.translation.gcrootfinder = 'asmgcc'
        t.config.translation.list_comprehension_operations = True
        t.config.translation.gcremovetypeptr = True
        return t
