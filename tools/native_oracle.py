"""Run the Pairlink mesh crypto (libnative-lib.so, arm64) from the ODELIC APK under Unicorn.

The library is loaded straight out of the APK, relocated, and its JNI entry points are
called with a minimal fake JNIEnv. Packets the library wants to transmit (via the Java
callback MeshCommon.sendData) are collected and returned to the caller.
"""
import io
import struct
import zipfile

from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UcError
from unicorn.arm64_const import *

LIB_IN_APK = "lib/arm64-v8a/libnative-lib.so"

BASE = 0x10000000
STUB = 0x20000000          # import stubs / JNI function stubs (each 4 bytes: `ret`)
STUB_SIZE = 0x10000
HEAP = 0x30000000
HEAP_SIZE = 0x400000
TLS = 0x40000000
STACK = 0x50000000
STACK_SIZE = 0x100000
RET_MAGIC = STUB + STUB_SIZE - 4

RET_INSN = struct.pack("<I", 0xD65F03C0)

# JNIEnv function-table indices we implement
JNI_FindClass = 6
JNI_DeleteLocalRef = 23
JNI_GetStaticMethodID = 113
JNI_CallStaticVoidMethod = 141
JNI_GetArrayLength = 171
JNI_NewByteArray = 176
JNI_GetByteArrayElements = 184
JNI_ReleaseByteArrayElements = 192
JNI_GetByteArrayRegion = 200
JNI_SetByteArrayRegion = 208
JNI_ExceptionCheck = 228
# JavaVM (JNIInvokeInterface) indices
VM_AttachCurrentThread = 4
VM_DetachCurrentThread = 5
VM_GetEnv = 6


class PlNative:
    def __init__(self, apk_path, verbose=False):
        self.verbose = verbose
        with zipfile.ZipFile(apk_path) as z:
            blob = z.read(LIB_IN_APK)
        self.elf = ELFFile(io.BytesIO(blob))
        self.blob = blob
        self.uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        self.heap_ptr = HEAP
        self.objects = {}      # handle -> python object (bytearray for byte[])
        self.next_handle = 0x1000
        self.stub_handlers = {}
        self.next_stub = STUB
        self.sent = []         # packets emitted via MeshCommon.sendData
        self.logs = []
        self._load()
        self._setup_jni()
        self._call(self.sym["JNI_OnLoad"], [self.vm_ptr, 0])

    # ---------------- loading ----------------
    def _load(self):
        uc = self.uc
        end = 0
        for seg in self.elf.iter_segments():
            if seg["p_type"] == "PT_LOAD":
                end = max(end, seg["p_vaddr"] + seg["p_memsz"])
        size = (end + 0xFFFF) & ~0xFFFF
        uc.mem_map(BASE, size)
        for seg in self.elf.iter_segments():
            if seg["p_type"] == "PT_LOAD":
                uc.mem_write(BASE + seg["p_vaddr"], seg.data())
        uc.mem_map(STUB, STUB_SIZE)
        uc.mem_write(STUB, RET_INSN * (STUB_SIZE // 4))
        uc.mem_map(HEAP, HEAP_SIZE)
        uc.mem_map(TLS, 0x1000)
        uc.mem_write(TLS + 0x28, struct.pack("<Q", 0x1122334455667788))
        uc.reg_write(UC_ARM64_REG_TPIDR_EL0, TLS)
        uc.mem_map(STACK, STACK_SIZE)
        try:
            uc.reg_write(UC_ARM64_REG_CPACR_EL1, 0x300000)
        except Exception:
            pass

        dynsym = self.elf.get_section_by_name(".dynsym")
        self.sym = {}
        for s in dynsym.iter_symbols():
            if s["st_shndx"] != "SHN_UNDEF" and s.name:
                self.sym[s.name] = BASE + s["st_value"]

        imports = {
            "memcpy": self._memcpy, "memset": self._memset, "memcmp": self._memcmp,
            "rand": self._rand, "__android_log_print": self._log,
            "__stack_chk_fail": self._abort, "__cxa_atexit": self._zero,
            "__cxa_finalize": self._zero,
        }
        import_addr = {name: self._new_stub(fn) for name, fn in imports.items()}

        for sec in self.elf.iter_sections():
            if not isinstance(sec, RelocationSection):
                continue
            symtab = self.elf.get_section(sec["sh_link"])
            for r in sec.iter_relocations():
                rtype = r["r_info_type"]
                where = BASE + r["r_offset"]
                if rtype == 1027:  # R_AARCH64_RELATIVE
                    val = BASE + r["r_addend"]
                elif rtype in (1025, 1026, 257):  # GLOB_DAT, JUMP_SLOT, ABS64
                    s = symtab.get_symbol(r["r_info_sym"])
                    if s["st_shndx"] != "SHN_UNDEF":
                        val = BASE + s["st_value"] + r["r_addend"]
                    elif s.name in import_addr:
                        val = import_addr[s.name]
                    else:
                        raise RuntimeError("unresolved import " + s.name)
                else:
                    raise RuntimeError("unhandled reloc %d" % rtype)
                uc.mem_write(where, struct.pack("<Q", val))

        uc.hook_add(UC_HOOK_CODE, self._hook_stub, begin=STUB, end=STUB + STUB_SIZE - 1)

    def _new_stub(self, fn):
        addr = self.next_stub
        self.next_stub += 4
        self.stub_handlers[addr] = fn
        return addr

    def _hook_stub(self, uc, addr, size, _):
        if addr == RET_MAGIC:
            uc.emu_stop()
            return
        fn = self.stub_handlers.get(addr)
        if fn is None:
            raise RuntimeError("unknown stub hit at 0x%x" % addr)
        ret = fn()
        if ret is not None:
            uc.reg_write(UC_ARM64_REG_X0, ret & 0xFFFFFFFFFFFFFFFF)

    # ---------------- helpers ----------------
    def reg(self, n):
        return self.uc.reg_read(UC_ARM64_REG_X0 + n) if n <= 28 else None

    def malloc(self, n):
        p = self.heap_ptr
        self.heap_ptr += (n + 15) & ~15
        if self.heap_ptr > HEAP + HEAP_SIZE:
            raise RuntimeError("emu heap exhausted")
        return p

    def cstr(self, addr):
        out = b""
        while True:
            c = self.uc.mem_read(addr, 64)
            i = c.find(b"\0")
            if i >= 0:
                return (out + bytes(c[:i])).decode("latin1")
            out += bytes(c)
            addr += 64

    def new_obj(self, obj):
        h = self.next_handle
        self.next_handle += 8
        self.objects[h] = obj
        return h

    def _call(self, addr, args):
        uc = self.uc
        assert len(args) <= 8
        for i, a in enumerate(args):
            uc.reg_write(UC_ARM64_REG_X0 + i, a & 0xFFFFFFFFFFFFFFFF)
        uc.reg_write(UC_ARM64_REG_SP, STACK + STACK_SIZE - 0x1000)
        uc.reg_write(UC_ARM64_REG_LR, RET_MAGIC)
        uc.emu_start(addr, RET_MAGIC)
        return uc.reg_read(UC_ARM64_REG_X0)

    # ---------------- libc ----------------
    def _memcpy(self):
        d, s, n = self.reg(0), self.reg(1), self.reg(2)
        if n:
            self.uc.mem_write(d, bytes(self.uc.mem_read(s, n)))
        return d

    def _memset(self):
        d, c, n = self.reg(0), self.reg(1), self.reg(2)
        if n:
            self.uc.mem_write(d, bytes([c & 0xFF]) * n)
        return d

    def _memcmp(self):
        a, b, n = self.reg(0), self.reg(1), self.reg(2)
        x, y = bytes(self.uc.mem_read(a, n)), bytes(self.uc.mem_read(b, n))
        return 0 if x == y else (-1 if x < y else 1)

    def _rand(self):
        import random
        return random.randint(0, 0x7FFFFFFF)

    def _log(self):
        try:
            tag = self.cstr(self.reg(1))
            fmt = self.cstr(self.reg(2))
        except UcError:
            return 0
        # best-effort printf: integer/str args from x3..x7
        args, ri = [], 3
        import re
        for m in re.finditer(r"%[-0-9.]*(l*)([dxXusc])", fmt):
            if ri > 7:
                break
            v = self.reg(ri)
            ri += 1
            if m.group(2) == "s":
                try:
                    args.append(self.cstr(v))
                except UcError:
                    args.append("?")
            else:
                args.append(v & 0xFFFFFFFF if not m.group(1) else v)
        try:
            msg = re.sub(r"%[-0-9.]*l*([dxXusc])", lambda m: "%" + m.group(0)[1:].replace("l", ""), fmt) % tuple(args)
        except Exception:
            msg = fmt
        self.logs.append("%s: %s" % (tag, msg))
        if self.verbose:
            print("  [native] %s: %s" % (tag, msg.rstrip()))
        return 0

    def _abort(self):
        raise RuntimeError("__stack_chk_fail in native code")

    def _zero(self):
        return 0

    # ---------------- fake JNI ----------------
    def _setup_jni(self):
        n = 240
        # JNIEnv
        env_table = self.malloc(n * 8)
        handlers = {
            JNI_FindClass: self._jni_find_class,
            JNI_DeleteLocalRef: self._zero,
            JNI_GetStaticMethodID: self._jni_get_static_method,
            JNI_CallStaticVoidMethod: self._jni_call_static_void,
            JNI_GetArrayLength: self._jni_array_length,
            JNI_NewByteArray: self._jni_new_byte_array,
            JNI_GetByteArrayElements: self._jni_get_elements,
            JNI_ReleaseByteArrayElements: self._jni_release_elements,
            JNI_GetByteArrayRegion: self._jni_get_region,
            JNI_SetByteArrayRegion: self._jni_set_region,
            JNI_ExceptionCheck: self._zero,
        }
        for i in range(n):
            fn = handlers.get(i) or self._unimpl(i, "JNIEnv")
            self.uc.mem_write(env_table + i * 8, struct.pack("<Q", self._new_stub(fn)))
        self.env_ptr = self.malloc(8)
        self.uc.mem_write(self.env_ptr, struct.pack("<Q", env_table))
        # JavaVM
        vm_table = self.malloc(8 * 8)
        vmh = {VM_AttachCurrentThread: self._vm_get_env, VM_GetEnv: self._vm_get_env,
               VM_DetachCurrentThread: self._zero}
        for i in range(8):
            fn = vmh.get(i) or self._unimpl(i, "JavaVM")
            self.uc.mem_write(vm_table + i * 8, struct.pack("<Q", self._new_stub(fn)))
        self.vm_ptr = self.malloc(8)
        self.uc.mem_write(self.vm_ptr, struct.pack("<Q", vm_table))
        self.elements = {}  # native ptr -> handle

    def _unimpl(self, i, what):
        def f():
            raise RuntimeError("unimplemented %s function #%d" % (what, i))
        return f

    def _vm_get_env(self):
        self.uc.mem_write(self.reg(1), struct.pack("<Q", self.env_ptr))
        return 0

    def _jni_find_class(self):
        return self.new_obj(("class", self.cstr(self.reg(1))))

    def _jni_get_static_method(self):
        return self.new_obj(("method", self.cstr(self.reg(2)), self.cstr(self.reg(3))))

    def _jni_call_static_void(self):
        m = self.objects.get(self.reg(2))
        name = m[1] if m else "?"
        if name == "sendData":
            dev = self.objects[self.reg(3)]
            data = self.objects[self.reg(4)]
            ln = self.reg(5) & 0xFFFFFFFF
            self.sent.append(bytes(data[:ln]))
            if self.verbose:
                print("  [native->sendData] dev=%s %s" % (bytes(dev).hex(), bytes(data[:ln]).hex()))
        elif self.verbose:
            print("  [native->%s] ignored" % name)
        return 0

    def _jni_array_length(self):
        return len(self.objects[self.reg(1)])

    def _jni_new_byte_array(self):
        return self.new_obj(bytearray(self.reg(1) & 0xFFFFFFFF))

    def _jni_get_elements(self):
        h = self.reg(1)
        arr = self.objects[h]
        p = self.malloc(len(arr) + 1)
        self.uc.mem_write(p, bytes(arr))
        self.elements[p] = h
        if self.reg(2):
            self.uc.mem_write(self.reg(2), b"\0")
        return p

    def _jni_release_elements(self):
        h, p, mode = self.reg(1), self.reg(2), self.reg(3) & 0xFFFFFFFF
        arr = self.objects[h]
        if mode != 2:  # JNI_ABORT
            arr[:] = self.uc.mem_read(p, len(arr))
        return 0

    def _jni_get_region(self):
        h, start, ln, buf = self.reg(1), self.reg(2) & 0xFFFFFFFF, self.reg(3) & 0xFFFFFFFF, self.reg(4)
        self.uc.mem_write(buf, bytes(self.objects[h][start:start + ln]))
        return 0

    def _jni_set_region(self):
        h, start, ln, buf = self.reg(1), self.reg(2) & 0xFFFFFFFF, self.reg(3) & 0xFFFFFFFF, self.reg(4)
        self.objects[h][start:start + ln] = self.uc.mem_read(buf, ln)
        return 0

    # ---------------- public API (mirrors MeshCommon natives) ----------------
    def _jcall(self, name, *args):
        self.sent = []
        return self._call(self.sym["Java_com_pairlink_connectedmesh_lib_MeshCommon_" + name],
                          [self.env_ptr, self.new_obj(("this",))] + list(args))

    def _ret_bytes(self, h):
        if not h:
            return None
        return bytes(self.objects.get(h, b""))

    def set_homeid_password(self, homeid4, pwd4):
        self._jcall("setHomeidPassword", self.new_obj(bytearray(homeid4)), self.new_obj(bytearray(pwd4)))

    def process_data(self, mac6, data):
        h = self._jcall("processData", self.new_obj(bytearray(mac6)), self.new_obj(bytearray(data)), len(data))
        return self._ret_bytes(h), list(self.sent)

    def send_encry(self, mac6, data):
        h = self._jcall("sendEncry", self.new_obj(bytearray(mac6)), self.new_obj(bytearray(data)), len(data))
        return self._ret_bytes(h), list(self.sent)

    def gen_create_data(self, data):
        h = self._jcall("genCreateData", self.new_obj(bytearray(data)), len(data))
        return self._ret_bytes(h)

    def device_disconnected(self, mac6):
        self._jcall("deviceDisconnected", self.new_obj(bytearray(mac6)))

    def mesh_exited(self):
        self._jcall("meshExited")
