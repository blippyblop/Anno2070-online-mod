/*
 * upc_r2_loader64.dll — minimal offline Ubisoft Connect (UPC R2) emulator
 * for Anno 2070 (Anno5.exe 3.0.8051, Steam build).
 *
 * Purpose: let the game run fully offline with no Ubisoft Connect client
 * and no Steam. Implements the subset of the UPC R2 API that Anno 2070
 * uses, following the semantics of the (CC-licensed) Goldberg R2 emulator
 * reference implementation:
 *   - UPC_Init               -> Ok (0)
 *   - UPC_ContextCreate      -> static fake context
 *   - UPC_UserGet            -> fake local user, callback fired synchronously
 *   - UPC_TicketGet_Extended -> static offline ticket string
 *   - UPC_ProductListGet     -> base game product owned (addon enables by file presence)
 *   - everything else        -> benign no-ops / "offline" codes
 *
 * Callbacks use the real UPC convention: cb(UPC_Result result, void* data).
 * Every call is logged to upc_stub.log next to the game exe.
 */

#include <windows.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdlib.h>

/* ---------------- logging ---------------- */

static CRITICAL_SECTION g_log_lock;
static LONG g_log_init = 0;
static char g_log_path[MAX_PATH] = {0};

static void log_init(void)
{
    if (!InterlockedCompareExchange(&g_log_init, 1, 0)) {
        InitializeCriticalSection(&g_log_lock);
        char *p = g_log_path + GetModuleFileNameA(NULL, g_log_path, MAX_PATH);
        while (p > g_log_path && *p != '\\' && *p != '/') p--;
        if (p > g_log_path) *p = 0; else *p = '.';
        strcat_s(g_log_path, sizeof(g_log_path), "\\upc_stub.log");
    }
}

static void logf(const char *fmt, ...)
{
    log_init();
    EnterCriticalSection(&g_log_lock);
    FILE *f = fopen(g_log_path, "a");
    if (f) {
        SYSTEMTIME st;
        GetLocalTime(&st);
        fprintf(f, "[%02u:%02u:%02u.%03u] ",
                st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
        va_list ap;
        va_start(ap, fmt);
        vfprintf(f, fmt, ap);
        va_end(ap);
        fputc('\n', f);
        fclose(f);
    }
    LeaveCriticalSection(&g_log_lock);
}

/* ---------------- SDK diagnostics hook ----------------
 *
 * Every ubiservices SDK diagnostic ("Session Created at ...", "Parameters are
 * not ready when trying to get the configured game notification.", error
 * 0x155 ...) is streamed into a std::ostream whose backing buffer is never
 * attached to a file, so every failing check is silent by construction.
 *
 * We inline-hook std::ostream::operator<<(char const*) inside Anno5.exe and
 * append every streamed string to sdk_stream.log together with the caller's
 * RVA, so the exact check that fails becomes visible.
 *
 * Target: Anno5.exe  0x1401479f0  (RVA 0x1479f0, verified prologue below)
 */

#define SDK_HOOK_RVA    0x1479f0u
#define SDK_HOOK_COPY   20u          /* bytes of whole instructions relocated */
#define SDK_LOG_MAX     (16u * 1024u * 1024u)

typedef void *(__cdecl *ostream_fn)(void *self, const char *s);

static unsigned char   *g_sdk_tramp = NULL;
static HANDLE           g_sdk_fh    = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION g_sdk_lock;
static LONG             g_sdk_init  = 0;
static unsigned long long g_sdk_bytes = 0;
static int              g_sdk_capped  = 0;
static int              g_mirror_n    = 0;   /* v44: PROBE lines also mirrored to upc_stub.log */
static void            *g_sdk_lself = NULL;
static void            *g_sdk_lret  = NULL;
static unsigned         g_sdk_lhash = 0;
static unsigned long    g_sdk_rep   = 0;

static void sdk_log_init(void)
{
    if (!InterlockedCompareExchange(&g_sdk_init, 1, 0)) {
        InitializeCriticalSection(&g_sdk_lock);
        char path[MAX_PATH];
        char *p = path + GetModuleFileNameA(NULL, path, MAX_PATH);
        while (p > path && *p != '\\' && *p != '/') p--;
        if (p > path) *p = 0; else *p = '.';
        strcat_s(path, sizeof(path), "\\sdk_stream.log");
        g_sdk_fh = CreateFileA(path, FILE_APPEND_DATA,
                               FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                               OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    }
}

static void sdk_write(const char *buf, int len)
{
    DWORD wrote = 0;
    if (g_sdk_fh == INVALID_HANDLE_VALUE) return;
    WriteFile(g_sdk_fh, buf, (DWORD)len, &wrote, NULL);
    g_sdk_bytes += (unsigned long long)len;
    if (g_sdk_bytes > SDK_LOG_MAX && !g_sdk_capped) {
        static const char msg[] = "\n*** sdk_stream.log size cap reached, logging stops ***\n";
        WriteFile(g_sdk_fh, msg, (DWORD)(sizeof(msg) - 1), &wrote, NULL);
        g_sdk_capped = 1;
    }
}

__attribute__((noinline))
static void sdk_emit(void *self, void *ret, const char *s)
{
    char line[2600];
    int off = 0;
    size_t i, n = 0;
    unsigned h = 2166136261u;
    SYSTEMTIME st;
    unsigned long long base, rva;

    sdk_log_init();
    if (g_sdk_fh == INVALID_HANDLE_VALUE) return;
    if (!s) s = "(null)";
    while (n < 2048 && s[n]) n++;
    for (i = 0; i < n; i++) { h ^= (unsigned char)s[i]; h *= 16777619u; }

    EnterCriticalSection(&g_sdk_lock);
    if (g_sdk_capped) { LeaveCriticalSection(&g_sdk_lock); return; }
    if (self == g_sdk_lself && ret == g_sdk_lret && h == g_sdk_lhash) {
        g_sdk_rep++;
        LeaveCriticalSection(&g_sdk_lock);
        return;
    }
    if (g_sdk_rep) {
        off = snprintf(line, sizeof(line), "        (previous line repeated %lu more times)\n",
                       g_sdk_rep);
        if (off > 0) sdk_write(line, off);
        g_sdk_rep = 0;
    }
    g_sdk_lself = self; g_sdk_lret = ret; g_sdk_lhash = h;

    GetLocalTime(&st);
    base = (unsigned long long)(uintptr_t)GetModuleHandleA(NULL);
    rva  = (unsigned long long)(uintptr_t)ret - base;
    off = snprintf(line, sizeof(line), "[%02u:%02u:%02u.%03u] s=%llx r=%llx ",
                   st.wHour, st.wMinute, st.wSecond, st.wMilliseconds,
                   (unsigned long long)(uintptr_t)self, rva);
    if (off < 0) off = 0;
    for (i = 0; i < n && off < (int)sizeof(line) - 8; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c == '\n')      { line[off++] = '\\'; line[off++] = 'n'; }
        else if (c == '\r') { /* skip */ }
        else if (c < 0x20 || c > 0x7e) line[off++] = '.';
        else                line[off++] = (char)c;
    }
    line[off++] = '\n';
    sdk_write(line, off);
    LeaveCriticalSection(&g_sdk_lock);
}

__attribute__((noinline))
static void *__cdecl sdk_stream_hook(void *self, const char *s)
{
    void *ret = __builtin_return_address(0);
    sdk_emit(self, ret, s);
    return ((ostream_fn)g_sdk_tramp)(self, s);
}


static void bt_format(void **frames, unsigned nf, char *out, int cap);
static unsigned bt_capture(void **frames, unsigned max, unsigned skip);

static int safe_readable(const void *p, size_t n)
{
    MEMORY_BASIC_INFORMATION mbi;
    if (!p) return 0;
    if (VirtualQuery(p, &mbi, sizeof(mbi)) != sizeof(mbi)) return 0;
    if (mbi.State != MEM_COMMIT) return 0;
    if (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD)) return 0;
    return ((const unsigned char *)p + n) <= ((const unsigned char *)mbi.BaseAddress + mbi.RegionSize);
}

/* Resolve a hook target. Normally: Anno5.exe image base + RVA. With
 * GAME_HOOK_TEST=1 set, an export of that name in the main module is used
 * instead, so the trampoline mechanics can be validated outside the game. */
static unsigned char *hook_target(const char *test_export, unsigned long rva)
{
    HMODULE m;
    char flag[8] = {0};
    m = GetModuleHandleA(NULL);
    if (GetEnvironmentVariableA("GAME_HOOK_TEST", flag, sizeof(flag)) > 0 && flag[0] == '1') {
        FARPROC p = GetProcAddress(m, test_export);
        if (p) {
            logf("hook target override: %s -> %p", test_export, (void *)p);
            return (unsigned char *)p;
        }
    }
    return (unsigned char *)m + rva;
}

static void sdk_hook_install(void)
{
    static const unsigned char expect[SDK_HOOK_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x10,   /* mov [rsp+0x10], rbx */
        0x48, 0x89, 0x74, 0x24, 0x18,   /* mov [rsp+0x18], rsi */
        0x48, 0x89, 0x7c, 0x24, 0x20,   /* mov [rsp+0x20], rdi */
        0x48, 0x89, 0x4c, 0x24, 0x08    /* mov [rsp+0x08], rcx */
    };
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *target, *t, patch[16];
    DWORD old = 0;
    char flag[8] = {0};

    if (!base) return;
    /* v26: ON by default. The v16-era "ostream timing" crashes are
     * superseded - the real cause was the conquest STARTUP connect's
     * dangling createSession job, suppressed since v24. With the hook ON
     * the full SDK narrative lands in sdk_stream.log AND (observed
     * 2026-11-03 11:21 run) the createSession completion wins its race
     * and the game survives the conquest chain. Set UPC_SDK_STREAM=0 to
     * disable (A/B). */
    {
        char dis[8] = {0};
        if (GetEnvironmentVariableA("UPC_SDK_STREAM", dis, sizeof(dis)) > 0 && dis[0] == '0') {
            logf("sdk hook: ostream<< hook disabled (UPC_SDK_STREAM=0)");
            return;
        }
    }
    target = hook_target("TestShift", SDK_HOOK_RVA);
    if (!safe_readable(target, SDK_HOOK_COPY)) {
        logf("sdk hook: target %p not readable - skipped", target);
        return;
    }
    if (memcmp(target, expect, SDK_HOOK_COPY) != 0) {
        logf("sdk hook: prologue mismatch at %p (base %p) - skipped", target, base);
        return;
    }
    g_sdk_tramp = (unsigned char *)VirtualAlloc(NULL, 64, MEM_COMMIT | MEM_RESERVE,
                                                PAGE_EXECUTE_READWRITE);
    if (!g_sdk_tramp) { logf("sdk hook: VirtualAlloc failed"); return; }
    memcpy(g_sdk_tramp, target, SDK_HOOK_COPY);
    t = g_sdk_tramp + SDK_HOOK_COPY;
    *t++ = 0xff; *t++ = 0x25;               /* jmp qword ptr [rip+0] */
    *(unsigned int *)t = 0; t += 4;
    *(unsigned long long *)t = (unsigned long long)(uintptr_t)(target + SDK_HOOK_COPY);

    if (!VirtualProtect(target, 16, PAGE_EXECUTE_READWRITE, &old)) {
        logf("sdk hook: VirtualProtect failed (%lu)", GetLastError());
        return;
    }
    memset(patch, 0x90, sizeof(patch));
    patch[0] = 0xff; patch[1] = 0x25;
    *(unsigned int *)(patch + 2) = 0;
    *(unsigned long long *)(patch + 6) = (unsigned long long)(uintptr_t)sdk_stream_hook;
    memcpy(target, patch, sizeof(patch));
    VirtualProtect(target, 16, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    logf("sdk hook: installed ostream<< hook at %p (rva 0x%x) -> sdk_stream.log",
         target, SDK_HOOK_RVA);
}

/* ---------------- store probe ----------------
 *
 * All ~127 "parameters are ready" gates in the SDK test
 *     *(char *)(*(SessionManagerStore + 0x148) + 0x10) != 0
 * with SessionManagerStore = *(connection + 0x28). Nothing in the SDK ever
 * tells us what that byte is, so we capture the store pointer when its
 * constructor runs (Anno5.exe 0x140e8ab60) and dump the field + the subtree
 * header around it whenever it changes.
 *
 * Target prologue (15 bytes, whole instructions):
 *   48 89 5c 24 08  48 89 74 24 10  57  48 83 ec 20
 */

#define STORE_CTOR_RVA  0xe8ab60u
#define STORE_CTOR_COPY 15u

typedef void *(__cdecl *ctor_fn)(void *self, void *arg2);

static void * volatile g_store      = NULL;
static void * volatile g_facade     = NULL; /* full 64-bit SessionManager owner */
static unsigned char      *g_ctor_tramp = NULL;
static HANDLE              g_probe_thread = NULL;

/* The SDK intermittently stores only the low 32 bits of the facade pointer at
 * SessionManagerStore+8 (observed as e.g. 0x837416c8 after the ctor received
 * 0x26a837416c8). Every 64-bit consumer then AVs on handle+8 / handle+0x28.
 * Detect that exact corruption and restore the value the ctor was given. */
static void probe_log(const char *fmt, ...);

static int facade_is_truncated(unsigned long long h)
{
    unsigned long long f = (unsigned long long)(uintptr_t)g_facade;
    return f != 0 && (h >> 32) == 0 && h == (unsigned int)f;
}

static int facade_repair(void *store, const char *who)
{
    unsigned long long h;
    if (!store) return 0;
    h = *(unsigned long long *)((char *)store + 8);
    if (!facade_is_truncated(h)) return 0;
    *(void **)((char *)store + 8) = g_facade;
    probe_log("%s: repaired truncated facade %p -> %p (store=%p)",
              who, (void *)(uintptr_t)h, g_facade, store);
    return 1;
}

static unsigned long long rd64(const void *base, size_t off)
{
    const unsigned char *p = (const unsigned char *)base + off;
    unsigned long long v = 0;
    if (!safe_readable(p, 8)) return 0xdeadbeefULL;
    memcpy(&v, p, 8);
    return v;
}

static void *rdptr(const void *base, size_t off)
{
    return (void *)(uintptr_t)rd64(base, off);
}

static void probe_log(const char *fmt, ...)
{
    char buf[512];
    char body[420];
    int n;
    va_list ap;
    SYSTEMTIME st;

    /* v44: mirror every probe line into upc_stub.log as well.  The
     * sdk_stream.log path (game exe dir) can silently die - when that
     * happens the whole probe channel goes dark and a run looks
     * "clean" (observed 2026-11-04 10:35 run: 0-byte sdk_stream.log,
     * zero PROBE lines although the stub was alive).  The upc_stub.log
     * writer is independent (fopen per call) and always works. */
    va_start(ap, fmt);
    vsnprintf(body, sizeof(body), fmt, ap);
    va_end(ap);
    if (g_mirror_n < 4000) { g_mirror_n++; logf("PROBE %s", body); }

    sdk_log_init();
    if (g_sdk_fh == INVALID_HANDLE_VALUE) return;
    GetLocalTime(&st);
    n = snprintf(buf, sizeof(buf), "[%02u:%02u:%02u.%03u] PROBE ",
                 st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
    va_start(ap, fmt);
    n += vsnprintf(buf + n, sizeof(buf) - n, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if (n > (int)sizeof(buf) - 2) n = (int)sizeof(buf) - 2;
    buf[n++] = '\n';
    EnterCriticalSection(&g_sdk_lock);
    sdk_write(buf, n);
    LeaveCriticalSection(&g_sdk_lock);
}

static void probe_snapshot(const char *why)
{
    void *store = g_store;
    void *p148;
    int b10 = -1;
    if (!store || !safe_readable(store, 0x160)) return;
    p148 = rdptr(store, 0x148);
    if (p148 && safe_readable(p148, 0x18)) b10 = *(unsigned char *)((const unsigned char *)p148 + 0x10);
    probe_log("%s store=%p p148=%p b10=%d  n148=[%llx %llx %llx] sz150=%llx | map10=%p sz18=%llx",
              why, store, p148, b10,
              rd64(p148, 0), rd64(p148, 8), rd64(p148, 0x10),
              rd64(store, 0x150), rdptr(store, 0x10), rd64(store, 0x18));
    /* dump the job-state map (job type : state) */
    {
        void *head = rdptr(store, 0x10);
        void *root = head ? rdptr(head, 8) : NULL;
        void *stack[24];
        void *cur = root;
        int sp = 0, count = 0, off = 0;
        char buf[768];
        buf[0] = 0;
        while ((cur || sp) && count < 24) {
            while (cur && sp < 24) {
                if (!safe_readable(cur, 0x24)) { cur = NULL; break; }
                stack[sp++] = cur;
                cur = rdptr(cur, 0);
            }
            if (!sp) break;
            cur = stack[--sp];
            if (!*(unsigned char *)((const unsigned char *)cur + 0x19)) {
                int k = *(int *)((const unsigned char *)cur + 0x1c);
                int v = *(int *)((const unsigned char *)cur + 0x20);
                off += snprintf(buf + off, sizeof(buf) - off, " %d:%d", k, v);
                count++;
                if (off >= (int)sizeof(buf) - 1) { buf[sizeof(buf) - 1] = 0; break; }
            }
            cur = rdptr(cur, 0x10);
        }
        probe_log("jobs%s", buf[0] ? buf : " (empty)");
    }
}

static DWORD WINAPI probe_thread(LPVOID unused)
{
    unsigned long long last[4] = {0, 0, 0, 0};
    unsigned tick = 0;
    (void)unused;
    probe_log("probe thread started");
    for (;;) {
        void *store = g_store;
        void *p148 = NULL;
        int b10 = -1;
        if (store && safe_readable(store, 0x160)) {
            p148 = rdptr(store, 0x148);
            if (p148 && safe_readable(p148, 0x18))
                b10 = *(unsigned char *)((const unsigned char *)p148 + 0x10);
            if ((unsigned long long)(uintptr_t)p148 != last[0] ||
                (unsigned long long)b10 != last[1] ||
                rd64(store, 0x150) != last[2] ||
                rd64(store, 0x18) != last[3]) {
                last[0] = (unsigned long long)(uintptr_t)p148;
                last[1] = (unsigned long long)b10;
                last[2] = rd64(store, 0x150);
                last[3] = rd64(store, 0x18);
                probe_snapshot("change");
            }
        }
        if (++tick % 40 == 0) probe_snapshot("tick");
        Sleep(250);
    }
    return 0;
}

__attribute__((noinline))
static void *__cdecl store_ctor_hook(void *self, void *arg2)
{
    void *prev = InterlockedExchangePointer((void * volatile *)&g_store, self);
    if (arg2) InterlockedExchangePointer((void * volatile *)&g_facade, arg2);
    if (prev != self)
        probe_log("store ctor this=%p arg2=%p", self, arg2);
    return ((ctor_fn)g_ctor_tramp)(self, arg2);
}

static void store_probe_install(void)
{
    static const unsigned char expect[STORE_CTOR_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x08,   /* mov [rsp+0x08], rbx */
        0x48, 0x89, 0x74, 0x24, 0x10,   /* mov [rsp+0x10], rsi */
        0x57,                            /* push rdi */
        0x48, 0x83, 0xec, 0x20           /* sub rsp, 0x20 */
    };
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *target, *t, patch[16];
    DWORD old = 0;

    if (!base) return;
    target = hook_target("TestCtor", STORE_CTOR_RVA);
    if (!safe_readable(target, STORE_CTOR_COPY)) {
        probe_log("store probe: target %p not readable - skipped", target);
        return;
    }
    if (memcmp(target, expect, STORE_CTOR_COPY) != 0) {
        probe_log("store probe: prologue mismatch at %p - skipped", target);
        return;
    }
    g_ctor_tramp = (unsigned char *)VirtualAlloc(NULL, 64, MEM_COMMIT | MEM_RESERVE,
                                                 PAGE_EXECUTE_READWRITE);
    if (!g_ctor_tramp) { probe_log("store probe: VirtualAlloc failed"); return; }
    memcpy(g_ctor_tramp, target, STORE_CTOR_COPY);
    t = g_ctor_tramp + STORE_CTOR_COPY;
    *t++ = 0xff; *t++ = 0x25;
    *(unsigned int *)t = 0; t += 4;
    *(unsigned long long *)t = (unsigned long long)(uintptr_t)(target + STORE_CTOR_COPY);

    if (!VirtualProtect(target, 16, PAGE_EXECUTE_READWRITE, &old)) {
        probe_log("store probe: VirtualProtect failed (%lu)", GetLastError());
        return;
    }
    memset(patch, 0x90, sizeof(patch));
    patch[0] = 0xff; patch[1] = 0x25;
    *(unsigned int *)(patch + 2) = 0;
    *(unsigned long long *)(patch + 6) = (unsigned long long)(uintptr_t)store_ctor_hook;
    memcpy(target, patch, sizeof(patch));
    VirtualProtect(target, 16, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    probe_log("store probe: installed ctor hook at %p", target);
    /* v19: the 250ms watchdog that walks the job-registry map is OFF by
     * default (it reads garbage entries and adds thread wakeups during the
     * login window). Set UPC_PROBE=1 to re-enable. */
    {
        char f[8] = {0};
        if (GetEnvironmentVariableA("UPC_PROBE", f, sizeof(f)) > 0 && f[0] == '1') {
            g_probe_thread = CreateThread(NULL, 0, probe_thread, NULL, 0, NULL);
            probe_log("store probe: watchdog thread ENABLED");
        } else {
            probe_log("store probe: watchdog thread disabled by default (UPC_PROBE=1 to enable)");
        }
    }
}

/* ---------------- primary-store UAF guard ----------------
 *
 * FUN_140f4ebf0 ("Player's primary store inventory update", called from
 * JobLogin::applyDynamicUpdates on every login) dereferences
 *     handle = *(void **)(this + 8);   conn = *(void **)(handle + 0x28);
 * In some boots that handle is dangling garbage and the game dies with
 * 0xC0000005 at RVA 0xF4F1FD right after "Session Created at" and BEFORE
 * the menu (minidumps 13_31_00 / 14_20_19 / 14_20_44; garbage values
 * 0xEA1CF178 / 0xC2B978A0 / 0xC1D06B28 - all < 4 GiB, while the real
 * process heap on Win x64 lives above 0x140_00000000).
 * Guard: skip the whole call when the handle looks insane. The primary
 * store sync is uplay-inventory fluff (the Anno ARK lives in the RMC
 * PlayerProfileService, not here); skipping it also leaves the T=0x22
 * job-registry gate unset, which the SDK reads as "not in progress".
 * A stale-but-readable handle still passes the guard, so a healthy
 * primary-store update is unaffected. */

#define STOREUPD_RVA   0xf4ebf0u
#define STOREUPD_COPY  16u

typedef void *(__cdecl *storeupd_fn)(void *self, void *a2, void *a3);

static unsigned char *g_storeupd_tramp = NULL;

__attribute__((noinline))
static void *__cdecl storeupd_hook(void *self, void *a2, void *a3)
{
    unsigned long long handle = 0;
    if (self) handle = *(unsigned long long *)((char *)self + 8);
    /* restore a 32-bit-truncated facade pointer before the readability test */
    if (facade_repair(self, "storeupd guard"))
        handle = *(unsigned long long *)((char *)self + 8);
    /* the game faulted reading [handle+0x28], so "not readable" is exactly
     * the crash condition; healthy boots have a committed heap object here */
    if (!self || !safe_readable((const void *)(handle + 0x28), 8)) {
        sdk_emit(self, __builtin_return_address(0),
                 "storeupd guard: handle insane - skipping primary-store update");
        return NULL;
    }
    return ((storeupd_fn)g_storeupd_tramp)(self, a2, a3);
}

static void storeupd_hook_install(void)
{
    static const unsigned char expect[STOREUPD_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x20,   /* mov [rsp+0x20], rbx */
        0x55,                            /* push rbp */
        0x56,                            /* push rsi */
        0x57,                            /* push rdi */
        0x41, 0x54,                      /* push r12 */
        0x41, 0x55,                      /* push r13 */
        0x41, 0x56,                      /* push r14 */
        0x41, 0x57                       /* push r15 */
    };
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *target, *t, patch[16];
    DWORD old = 0;

    if (!base) return;
    target = hook_target("TestStoreUpd", STOREUPD_RVA);
    if (!safe_readable(target, STOREUPD_COPY)) {
        probe_log("storeupd guard: target %p not readable - skipped", target);
        return;
    }
    if (memcmp(target, expect, STOREUPD_COPY) != 0) {
        probe_log("storeupd guard: prologue mismatch at %p - skipped", target);
        return;
    }
    g_storeupd_tramp = (unsigned char *)VirtualAlloc(NULL, 64, MEM_COMMIT | MEM_RESERVE,
                                                     PAGE_EXECUTE_READWRITE);
    if (!g_storeupd_tramp) { probe_log("storeupd guard: VirtualAlloc failed"); return; }
    memcpy(g_storeupd_tramp, target, STOREUPD_COPY);
    t = g_storeupd_tramp + STOREUPD_COPY;
    *t++ = 0xff; *t++ = 0x25;
    *(unsigned int *)t = 0; t += 4;
    *(unsigned long long *)t = (unsigned long long)(uintptr_t)(target + STOREUPD_COPY);

    if (!VirtualProtect(target, 16, PAGE_EXECUTE_READWRITE, &old)) {
        probe_log("storeupd guard: VirtualProtect failed (%lu)", GetLastError());
        return;
    }
    memset(patch, 0x90, sizeof(patch));
    patch[0] = 0xff; patch[1] = 0x25;
    *(unsigned int *)(patch + 2) = 0;
    *(unsigned long long *)(patch + 6) = (unsigned long long)(uintptr_t)storeupd_hook;
    memcpy(target, patch, sizeof(patch));
    VirtualProtect(target, 16, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    probe_log("storeupd guard: installed hook at %p (rva 0x%x)", target, STOREUPD_RVA);
}

/* ---------------- primary-store sync guard (v30) ----------------
 *
 * FUN_140efeb20 = SessionManagerStore::m_processPrimaryStoreInventoryResult.
 * When it builds "JobPrimaryStoreSyncAndSendEvent" it copies
 *     handle = *(void **)(store + 8)   (the FacadeInternal pointer)
 * through FUN_140e7d010, whose first instruction is `lock inc [handle+8]`.
 * Some boots leave only the low 32 bits of the facade at store+8, so that
 * write goes to 0x00000000xxxxxxxx and faults at RVA 0xe7d03d. Repair the
 * field at entry; the value is the exact one the store ctor received. */

#define PRIMARY_RVA   0xefeb20u
#define PRIMARY_COPY  24u

typedef void *(__cdecl *primary_fn)(void *self, void *out, void *result,
                                    void *a4, unsigned char a5, unsigned char a6);

static unsigned char *g_primary_tramp = NULL;

__attribute__((noinline))
static void *__cdecl primary_hook(void *self, void *out, void *result,
                                  void *a4, unsigned char a5, unsigned char a6)
{
    facade_repair(self, "primary guard");
    return ((primary_fn)g_primary_tramp)(self, out, result, a4, a5, a6);
}

static void primary_hook_install(void)
{
    static const unsigned char expect[PRIMARY_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x18,   /* mov [rsp+0x18], rbx */
        0x4c, 0x89, 0x4c, 0x24, 0x20,   /* mov [rsp+0x20], r9 */
        0x48, 0x89, 0x54, 0x24, 0x10,   /* mov [rsp+0x10], rdx */
        0x55,                            /* push rbp */
        0x56,                            /* push rsi */
        0x57,                            /* push rdi */
        0x41, 0x54,                      /* push r12 */
        0x41, 0x55,                      /* push r13 */
        0x41, 0x56                       /* push r14 */
    };
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *target, *t, patch[16];
    DWORD old = 0;

    if (!base) return;
    target = (unsigned char *)base + PRIMARY_RVA;
    if (!safe_readable(target, PRIMARY_COPY)) {
        probe_log("primary guard: target %p not readable - skipped", target);
        return;
    }
    if (memcmp(target, expect, PRIMARY_COPY) != 0) {
        probe_log("primary guard: prologue mismatch at %p - skipped", target);
        return;
    }
    g_primary_tramp = (unsigned char *)VirtualAlloc(NULL, 64, MEM_COMMIT | MEM_RESERVE,
                                                    PAGE_EXECUTE_READWRITE);
    if (!g_primary_tramp) { probe_log("primary guard: VirtualAlloc failed"); return; }
    memcpy(g_primary_tramp, target, PRIMARY_COPY);
    t = g_primary_tramp + PRIMARY_COPY;
    *t++ = 0xff; *t++ = 0x25;
    *(unsigned int *)t = 0; t += 4;
    *(unsigned long long *)t = (unsigned long long)(uintptr_t)(target + PRIMARY_COPY);

    if (!VirtualProtect(target, 16, PAGE_EXECUTE_READWRITE, &old)) {
        probe_log("primary guard: VirtualProtect failed (%lu)", GetLastError());
        return;
    }
    memset(patch, 0x90, sizeof(patch));
    patch[0] = 0xff; patch[1] = 0x25;
    *(unsigned int *)(patch + 2) = 0;
    *(unsigned long long *)(patch + 6) = (unsigned long long)(uintptr_t)primary_hook;
    memcpy(target, patch, sizeof(patch));
    VirtualProtect(target, 16, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    probe_log("primary guard: installed hook at %p (rva 0x%x)", target, PRIMARY_RVA);
}

/* ---------------- parameter-lookup probe (v19, default ON) ----------------
 *
 * FUN_140ed95d0(out, store, name, env, ...) = "fetch parameter NAME from
 * the applied-parameters store, {env}/{version}-substituted" - the
 * resolver behind every post-login URL (websocketNotifications id 0x7a,
 * populations, eventsDefinitions, ...). On a MISS it returns an EMPTY
 * string, which is why the SDK calls them "malformed" later.
 * Hook: log every lookup with the name and either the resolved value or
 * MISS. This hook is LOW FREQUENCY (a handful of calls per login), so it
 * does not shift timing the way the ostream<< hook did. */

#define RDNAME_RVA   0xed95d0u
#define RDNAME_COPY  16u

typedef void *(__cdecl *rdname_fn)(void *out, void *store, void *name,
                                   unsigned env, unsigned a5, unsigned char a6);

static unsigned char *g_rdname_tramp = NULL;

/* read an SDK refcounted string: param points to a HANDLE whose target is
 * a 48-byte object {vtable@0, refcount@8, char buf/ptr@16, size@0x20,
 * cap@0x28} (SSO threshold 15); returns length or -1 */
static int str_read(const void *handle, char *dst, int dstcap)
{
    unsigned long long obj, size, cap;
    const char *p;
    if (!handle || !safe_readable(handle, 8)) return -1;
    obj = *(const unsigned long long *)handle;
    if (!obj || !safe_readable((const void *)obj, 0x30)) return -1;
    size = *(const unsigned long long *)(obj + 0x20);
    cap  = *(const unsigned long long *)(obj + 0x28);
    if (size >= (unsigned long long)dstcap) size = (unsigned long long)(dstcap - 1);
    p = cap > 15 ? (const char *)*(const void **)(obj + 0x10)
                 : (const char *)(obj + 0x10);
    if (!size) { dst[0] = 0; return 0; }
    if (!safe_readable(p, (size_t)size + 1)) return -1;
    memcpy(dst, p, (size_t)size);
    dst[size] = 0;
    return (int)size;
}

/* One-shot dump of the URL template map (store+0x210, MSVC _Tree:
 * head@map+0, root=head->_Parent(+8); node: left@0, parent@8, right@0x10,
 * color@0x18, isnil@0x19, _Myval@0x20). Prints the first 40 keys/values so
 * we can see exactly which endpoint names the SDK actually has - the
 * applied parameter groups never seem to reach this map. */
static int g_urlmap_dumped = 0;

static void urlmap_dump(void *store)
{
    unsigned char *head, *root, *stack[64];
    int sp = 0, n = 0;
    char line[320], k[72], k2[72], v[160], v2[160];
    int kl, kl2, vl, vl2;

    if (g_urlmap_dumped || !store) return;
    if (!safe_readable((const char *)store + 0x210, 8)) {
        probe_log("urlmap: store %p +0x210 unreadable", store);
        g_urlmap_dumped = 1;
        return;
    }
    g_urlmap_dumped = 1;
    head = *(unsigned char **)((char *)store + 0x210);
    if (!head || !safe_readable(head, 0x20)) { probe_log("urlmap: no head"); return; }
    root = *(unsigned char **)(head + 8);
    if (!root || !safe_readable(root, 0x20) || *(char *)(root + 0x19) != 0) {
        probe_log("urlmap: tree empty"); return;
    }
    stack[sp++] = root;
    while (sp > 0 && n < 40) {
        unsigned char *node = stack[--sp];
        unsigned char *l, *r;
        if (!safe_readable(node, 0x40)) continue;
        l = *(unsigned char **)node;
        r = *(unsigned char **)(node + 0x10);
        kl  = str_read(node + 0x20, k,  sizeof(k));
        kl2 = str_read(node + 0x28, k2, sizeof(k2));
        vl  = str_read(node + 0x30, v,  sizeof(v));
        vl2 = str_read(node + 0x38, v2, sizeof(v2));
        wsprintfA(line, "urlmap[%d] node=%p k20=%s k28=%s v30=%s v38=%s",
                  n, node, kl >= 0 ? k : "?", kl2 >= 0 ? k2 : "?",
                  vl >= 0 ? v : "?", vl2 >= 0 ? v2 : "?");
        sdk_emit(node, __builtin_return_address(0), line);
        n++;
        if (l && safe_readable(l, 0x20) && *(char *)(l + 0x19) == 0) stack[sp++] = l;
        if (r && safe_readable(r, 0x20) && *(char *)(r + 0x19) == 0) stack[sp++] = r;
    }
    probe_log("urlmap: dumped %d entries (store=%p)", n, store);
}

/* ---------------- URL-map injection (v33) ----------------
 * The URL map (store+0x210) is dump-proven to hold ONLY the 7 compiled
 * bootstrap endpoints; applied parameters never add entries, so every
 * non-bootstrap endpoint lookup MISSes and the SDK builds empty URLs
 * (the OnlineConfiguration fetch never leaves the process - the single
 * remaining "connection lost" blocker). This injects the missing
 * endpoint templates into the SAME map the resolver reads, using the
 * SDK's own primitives:
 *   string object : FUN_140e8b910(&handle, cstr)  (0x30 obj, refcount 2)
 *   ordering      : FUN_140eafcc0(keyA, keyB)     (case-insensitive, >0 if A>B)
 *   node storage  : FUN_141391970(size,4,0,6.0f)  (SDK heap w/ accounting)
 * Node = MSVC _Tree node: left@0 parent@8 right@0x10 color@0x18
 * isnil@0x19, key handle@0x20 (2 qwords, both = objptr - matches the
 * dumped nodes), value handle@0x30. Plain BST leaf insert, no rebalance
 * (fine for <20 nodes); head's leftmost/rightmost caches are maintained.
 * UPC_URLINJECT=0 disables. */
#define RK_STRCTOR_RVA 0xe8b910u
#define RK_CMP_RVA     0xeafcc0u
#define RK_ALLOC_RVA   0x1391970u

typedef void *(*rk_alloc_fn)(unsigned size, unsigned a2, unsigned a3, unsigned a4);
typedef void *(*rk_strctor_fn)(void *handle, const char *cstr);
typedef int (*rk_cmp_fn)(const void *ka, const void *kb);

static int g_urlmap_injected = 0;

static int urlmap_insert_one(unsigned char *base, unsigned char *head,
                             const char *key, const char *value)
{
    rk_alloc_fn allocf = (rk_alloc_fn)(base + RK_ALLOC_RVA);
    rk_strctor_fn mkstr = (rk_strctor_fn)(base + RK_STRCTOR_RVA);
    rk_cmp_fn cmp = (rk_cmp_fn)(base + RK_CMP_RVA);
    unsigned char *node, *cur, *parent, *root;
    unsigned long long kh[2] = {0, 0}, vh[2] = {0, 0};
    int c;

    mkstr(kh, key);
    mkstr(vh, value);
    if (!kh[0] || !vh[0] ||
        !safe_readable((const void *)(uintptr_t)kh[0], 0x30) ||
        !safe_readable((const void *)(uintptr_t)vh[0], 0x30)) {
        probe_log("urlmap inject: string ctor failed for %s", key);
        return 0;
    }
    node = (unsigned char *)allocf(0x60, 4, 0, 0x40c00000u);
    if (!node) { probe_log("urlmap inject: node alloc failed"); return 0; }
    memset(node, 0, 0x60);
    *(void **)(node + 0x00) = head;   /* left  = nil (head) */
    *(void **)(node + 0x08) = NULL;   /* parent, set below */
    *(void **)(node + 0x10) = head;   /* right = nil (head) */
    *(node + 0x18) = 0;               /* color */
    *(node + 0x19) = 0;               /* not-nil */
    *(void **)(node + 0x20) = (void *)(uintptr_t)kh[0];
    *(void **)(node + 0x28) = (void *)(uintptr_t)kh[0];
    *(void **)(node + 0x30) = (void *)(uintptr_t)vh[0];
    *(void **)(node + 0x38) = (void *)(uintptr_t)vh[0];

    root = *(unsigned char **)(head + 8);
    if (!root || root == head || !safe_readable(root, 0x20) ||
        *(char *)(root + 0x19) != 0) {
        /* empty tree: new node becomes root (+ leftmost/rightmost) */
        *(void **)(node + 0x08) = head;
        *(void **)(head + 0x08) = node;
        *(void **)(head + 0x00) = node;
        *(void **)(head + 0x10) = node;
        return 1;
    }
    cur = root;
    parent = head;
    while (cur && cur != head && safe_readable(cur, 0x40) &&
           *(char *)(cur + 0x19) == 0) {
        parent = cur;
        c = cmp(cur + 0x20, node + 0x20);
        cur = (c < 0) ? *(unsigned char **)(cur + 0x10) : *(unsigned char **)cur;
    }
    if (parent == head || !safe_readable(parent, 0x40)) {
        probe_log("urlmap inject: walk failed for %s", key);
        return 0;
    }
    c = cmp(parent + 0x20, node + 0x20);
    *(void **)(node + 0x08) = parent;
    if (c < 0) *(void **)(parent + 0x10) = node;   /* parent < new -> right */
    else       *(void **)parent = node;            /* else left */
    /* refresh head's leftmost / rightmost caches (iteration uses them) */
    cur = *(unsigned char **)(head + 0x08);
    while (cur && cur != head && *(char *)(cur + 0x19) == 0) {
        unsigned char *l = *(unsigned char **)cur;
        if (!l || l == head || *(char *)(l + 0x19) != 0) break;
        cur = l;
    }
    *(void **)(head + 0x00) = cur;
    cur = *(unsigned char **)(head + 0x08);
    while (cur && cur != head && *(char *)(cur + 0x19) == 0) {
        unsigned char *r = *(unsigned char **)(cur + 0x10);
        if (!r || r == head || *(char *)(r + 0x19) != 0) break;
        cur = r;
    }
    *(void **)(head + 0x10) = cur;
    return 1;
}

static void urlmap_inject(unsigned char *base, void *store)
{
    static const struct { const char *k; const char *v; } entries[] = {
        {"websocketNotifications", "ws://127.0.0.1:8443/notifications"},
        {"populations",            "http://127.0.0.1:8443/v3/populations"},
        {"eventsDefinitions",      "http://127.0.0.1:8443/v3/eventsDefinitions"},
        {"configsEvents",          "http://127.0.0.1:8443/v3/configsEvents"},
        {"spacesConfigsPrimarystore",
                                   "http://127.0.0.1:8443/v3/spacesConfigsPrimarystore"},
        {"profilesEntities",       "http://127.0.0.1:8443/v3/profilesEntities"},
        {"spacesEntities",         "http://127.0.0.1:8443/v3/spaces/{spaceId}/entities"},
        {"allSpacesEntities",      "http://127.0.0.1:8443/v3/allSpacesEntities"},
        {"allProfilesEntities",    "http://127.0.0.1:8443/v3/allProfilesEntities"},
        {"connections",            "http://127.0.0.1:8443/v3/connections"},
        {"allConnections",         "http://127.0.0.1:8443/v3/allConnections"},
        {"profilesNotifications",  "http://127.0.0.1:8443/v3/profilesNotifications"},
        {"profilesNotificationsBatch",
                                   "http://127.0.0.1:8443/v3/profilesNotificationsBatch"},
        {"usersMeOnlineStatuses",  "http://127.0.0.1:8443/v3/usersMeOnlineStatuses"},
        {"usersOnlineStatuses",    "http://127.0.0.1:8443/v3/usersOnlineStatuses"},
        {"telemetry",              "http://127.0.0.1:8443/v3/telemetry"},
        {"remoteLogs",             "http://127.0.0.1:8443/v3/remoteLogs"},
        {"calendar",               "http://127.0.0.1:8443/v3/calendar"},
        /* PN 149: the SDK ships NO template for registry id 0x10 ("profilesMeEvents"):
         * the only writer of the resolver map (FUN_140ee3860) is the store ctor
         * FUN_140e87140 and it registers seven constant ids (1, 0x55, 0x74, 0x25,
         * 0x26, 0x2f, 0x3a) - no server response can add one. So the name resolves
         * to MISS, sendImpl enqueues an EMPTY url, libcurl fails with
         * CURLE_URL_MALFORMAT, the completion callback never runs and the job that
         * issued it (JobQueueAndSendEvents::WaitOnStartEventSession, created right
         * after our sessstate probe) hangs forever. Same mechanism that unblocked
         * "sandboxes": plant the mapping ourselves. */
        {"profilesMeEvents",       "http://127.0.0.1:8443/v3/profilesMeEvents"},
        {"friends",                "http://127.0.0.1:8443/v3/friends"},
        /* v43: ARK / item-box (primary store inventory) + space items */
        {"profilesInventoryPrimarystore",
                                   "http://127.0.0.1:8443/v3/profilesInventoryPrimarystore"},
        {"profilesMeInventoryPrimarystore",
                                   "http://127.0.0.1:8443/v3/profilesMeInventoryPrimarystore"},
        {"profilesInventory",      "http://127.0.0.1:8443/v3/profilesInventory"},
        {"profilesInventoryInstances",
                                   "http://127.0.0.1:8443/v3/profilesInventoryInstances"},
        {"profilesInventoryTransactions",
                                   "http://127.0.0.1:8443/v3/profilesInventoryTransactions"},
        {"profilesInventoryExpiredDetails",
                                   "http://127.0.0.1:8443/v3/profilesInventoryExpiredDetails"},
        {"spacesItems",            "http://127.0.0.1:8443/v3/spacesItems"},
        {"allSpacesItems",         "http://127.0.0.1:8443/v3/allSpacesItems"},
        {"spacesConfigsSsiAttributes",
                                   "http://127.0.0.1:8443/v3/spacesConfigsSsiAttributes"},
        /* v47: the RENDEZVOUS component resolves "sandboxes" during the game's
         * own online login; the lookup MISSes (rdv lookup: name=sandboxes ->
         * MISS) and the RV job then returns an error code which surfaces as
         * ("LOGIN", 7) "The connection to the server has been lost." and puts
         * the whole game into OFFLINE MODE. */
        {"sandboxes",              "http://127.0.0.1:8443/v3/sandboxes"},
        {"allSandboxes",           "http://127.0.0.1:8443/v3/allSandboxes"},
    };
    unsigned char *head;
    char dis[8] = {0};
    int i, n = 0, want;

    if (g_urlmap_injected || !base || !store) return;
    g_urlmap_injected = 1;
    if (GetEnvironmentVariableA("UPC_URLINJECT", dis, sizeof(dis)) > 0 &&
        dis[0] == '0') {
        probe_log("urlmap inject: disabled (UPC_URLINJECT=0)");
        return;
    }
    if (!safe_readable((const char *)store + 0x210, 8)) return;
    head = *(unsigned char **)((char *)store + 0x210);
    if (!head || !safe_readable(head, 0x40)) {
        probe_log("urlmap inject: no head at %p", store);
        return;
    }
    want = (int)(sizeof(entries) / sizeof(entries[0]));
    for (i = 0; i < want; i++)
        n += urlmap_insert_one(base, head, entries[i].k, entries[i].v);
    probe_log("urlmap inject: added %d/%d entries (head=%p)", n, want, head);
    g_urlmap_dumped = 0;   /* force a verification dump on this lookup */
}

/* ---------------- connection-store probe (v34) ----------------
 * The websocket login fails with "Failed to get the connection Id from
 * the connection services" (inside the FUN_140f530d0 pump, tail
 * 0xf540b6-0xf548a7). That code needs (a) an int-key-9 entry in a map
 * and (b) the "parameters ready" byte at *(conn+0x148)+0x10. This probe
 * dumps the connection's parameter map (conn+0x138, same tree layout as
 * the URL map) and the +0x148 store when FUN_140ee2080
 * (checkConnection, the websocket login step) runs, so we can see what
 * the login actually has available and what is missing.
 * UPC_CONNSTORE=0 disables. */
#define CS_CHECK_RVA  0xee2080u
#define CS_CHECK_COPY 16u      /* 3x mov [rsp+x],reg (5+5+5) + push rbp (1) */
#define CS_PUMP_RVA   0xf530d0u
#define CS_PUMP_COPY  16u      /* push rbx; push r14; sub rsp,0x58; mov rsi,[rcx+8] */

typedef void (__cdecl *cs_check_fn)(void *self);
typedef unsigned long long (__cdecl *cs_pump_fn)(void *a, void *b);

static unsigned char *g_cs_check_tramp = NULL;
static unsigned char *g_cs_pump_tramp = NULL;
static int g_connstore = 1;

/* walk a URL-map-style tree (head@map, root=head[1], key handle@0x20,
 * value handle@0x30) and log every key=value pair */
static void strmap_dump(void *mapptr, const char *tag)
{
    unsigned char *head, *root, *stack[64];
    int sp = 0, n = 0;
    char line[384], k[72], v[160];

    if (!mapptr || !safe_readable(mapptr, 0x20)) {
        probe_log("strmap[%s]: unreadable %p", tag, mapptr);
        return;
    }
    head = *(unsigned char **)mapptr;
    if (!head || !safe_readable(head, 0x20)) {
        probe_log("strmap[%s]: no head", tag);
        return;
    }
    root = *(unsigned char **)(head + 8);
    if (!root || !safe_readable(root, 0x20) || *(char *)(root + 0x19) != 0) {
        probe_log("strmap[%s]: EMPTY (head=%p)", tag, head);
        return;
    }
    stack[sp++] = root;
    while (sp > 0 && n < 40) {
        unsigned char *node = stack[--sp];
        unsigned char *l, *r;
        if (!safe_readable(node, 0x40)) continue;
        l = *(unsigned char **)node;
        r = *(unsigned char **)(node + 0x10);
        str_read(node + 0x20, k, sizeof(k));
        str_read(node + 0x30, v, sizeof(v));
        wsprintfA(line, "strmap[%s][%d] %s = %s", tag, n,
                  k[0] ? k : "?", v[0] ? v : "(empty)");
        sdk_emit(node, __builtin_return_address(0), line);
        n++;
        if (l && safe_readable(l, 0x20) && *(char *)(l + 0x19) == 0) stack[sp++] = l;
        if (r && safe_readable(r, 0x20) && *(char *)(r + 0x19) == 0) stack[sp++] = r;
    }
    probe_log("strmap[%s]: %d entries", tag, n);
}

/* ---------------- notification-type probe (v35) ----------------
 * FUN_140ee5700(mapOwner, kind, id) = "is notification type <id>
 * registered with state in [2, limit(kind)]" against the SDK's
 * std::map<int,int> registration table (key@node+0x1c, state@node+0x20).
 * The websocket login's "Failed to get the connection Id" and the
 * incoming-notification dispatch both consult it. Log kind/id/result
 * (throttled: failures always, successes only the first 60).
 * UPC_NTYPE=0 disables. */
#define NT_RVA   0xee5700u
#define NT_COPY  6u              /* mov r9,rcx; cmp edx,5  (no rel br/rip-rel; flags kept) */
/* copy stops before the `ja +0x39`: a relative branch cannot be
 * relocated, so the trampoline returns to target+6 where the original
 * ja executes with the flags the cmp set (jmp does not touch EFLAGS). */

typedef unsigned char (__cdecl *nt_fn)(void *owner, int kind, int id);

static unsigned char *g_nt_tramp = NULL;
static unsigned long g_nt_n = 0;

__attribute__((noinline))
static unsigned char __cdecl nt_hook(void *owner, int kind, int id)
{
    unsigned char rv;
    char line[128];
    rv = ((nt_fn)g_nt_tramp)(owner, kind, id);
    g_nt_n++;
    if (g_nt_n <= 60 || !rv) {
        wsprintfA(line, "ntype[%lu]: kind=%d id=%d -> %s",
                  (unsigned long)g_nt_n, kind, id, rv ? "yes" : "NO");
        sdk_emit(owner, __builtin_return_address(0), line);
    }
    return rv;
}

static void nt_install(void)
{
    static const unsigned char expect[NT_COPY] = {
        0x4c, 0x8b, 0xc9,               /* mov r9,rcx */
        0x83, 0xfa, 0x05                /* cmp edx,5 */
    };
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *target, *t, patch[16];
    DWORD old = 0;
    char on[8] = {0};

    /* v66: this hook patches 14 bytes at 0xee5700 and something in the game
     * branches into 0xee570a - i.e. INSIDE the patch - so it executes our
     * jmp's address bytes as code and raises 0xc000001d (illegal instruction,
     * seen in the 18:12 dump). Diagnostic only, so it is now opt-in. */
    if (GetEnvironmentVariableA("UPC_NTYPE", on, sizeof(on)) == 0 || on[0] != '1') {
        probe_log("ntype probe: off (UPC_NTYPE=1 enables; it crashes the "
                  "0xee5700 branch table otherwise)");
        return;
    }

    if (!base) return;
    target = base + NT_RVA;
    if (!safe_readable(target, NT_COPY)) return;
    if (memcmp(target, expect, NT_COPY) != 0) {
        probe_log("ntype probe: prologue mismatch - skipped");
        return;
    }
    g_nt_tramp = (unsigned char *)VirtualAlloc(NULL, 64, MEM_COMMIT | MEM_RESERVE,
                                               PAGE_EXECUTE_READWRITE);
    if (!g_nt_tramp) return;
    memcpy(g_nt_tramp, target, NT_COPY);
    t = g_nt_tramp + NT_COPY;
    *t++ = 0xff; *t++ = 0x25;
    *(unsigned int *)t = 0; t += 4;
    *(unsigned long long *)t = (unsigned long long)(uintptr_t)(target + NT_COPY);
    if (!VirtualProtect(target, 16, PAGE_EXECUTE_READWRITE, &old)) return;
    memset(patch, 0x90, sizeof(patch));
    patch[0] = 0xff; patch[1] = 0x25;
    *(unsigned int *)(patch + 2) = 0;
    *(unsigned long long *)(patch + 6) = (unsigned long long)(uintptr_t)nt_hook;
    memcpy(target, patch, sizeof(patch));
    VirtualProtect(target, 16, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    probe_log("ntype probe: installed at %p", target);
}

__attribute__((noinline))
static void regmap_ensure(unsigned char *owner);   /* v38, defined below */

static void __cdecl cs_check_hook(void *self)
{
    char line[256];
    if (g_connstore && self && safe_readable((char *)self + 0xa8, 8)) {
        unsigned long long obj = *(unsigned long long *)((char *)self + 0xa0);
        if (obj && safe_readable((const void *)(uintptr_t)obj, 0x30)) {
            unsigned long long conn = *(unsigned long long *)((char *)obj + 0x20);
            /* v38: the REGISTRY owner is *(obj+0x28) (FUN_140ee2080's own
             * body); *(obj+0x20) is the param-store object (+0x138 map) */
            unsigned long long owner = 0;
            if (safe_readable((const void *)(uintptr_t)obj + 0x28, 8))
                owner = *(unsigned long long *)((char *)obj + 0x28);
            wsprintfA(line, "connstore: self=%p obj=%p conn=%p owner=%p", self,
                      (void *)(uintptr_t)obj, (void *)(uintptr_t)conn,
                      (void *)(uintptr_t)owner);
            sdk_emit(self, __builtin_return_address(0), line);
            if (conn && safe_readable((const void *)(uintptr_t)conn, 0x160)) {
                strmap_dump((char *)conn + 0x138, "conn138");
                {
                    unsigned long long st = *(unsigned long long *)((char *)conn + 0x148);
                    unsigned char flag = 0;
                    if (st && safe_readable((const void *)(uintptr_t)st, 0x18))
                        flag = *(unsigned char *)((char *)st + 0x10);
                    wsprintfA(line, "connstore: conn+0x148=%p ready=%u",
                              (void *)(uintptr_t)st, flag);
                    sdk_emit((void *)(uintptr_t)conn, __builtin_return_address(0), line);
                }
            }
            if (owner && safe_readable((const void *)(uintptr_t)owner, 0x160))
                regmap_ensure((unsigned char *)(uintptr_t)owner);   /* v38 */
        }
    }
    ((cs_check_fn)g_cs_check_tramp)(self);
}

static void cs_check_install(void)
{
    static const unsigned char expect[CS_CHECK_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x10,   /* mov [rsp+0x10],rbx */
        0x48, 0x89, 0x74, 0x24, 0x18,   /* mov [rsp+0x18],rsi */
        0x48, 0x89, 0x7c, 0x24, 0x20,   /* mov [rsp+0x20],rdi */
        0x55                            /* push rbp */
    };
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *target, *t, patch[16];
    DWORD old = 0;

    if (!base) return;
    target = base + CS_CHECK_RVA;
    if (!safe_readable(target, CS_CHECK_COPY)) return;
    if (memcmp(target, expect, CS_CHECK_COPY) != 0) {
        probe_log("connstore probe: prologue mismatch at %p - skipped", target);
        return;
    }
    g_cs_check_tramp = (unsigned char *)VirtualAlloc(NULL, 64, MEM_COMMIT | MEM_RESERVE,
                                                     PAGE_EXECUTE_READWRITE);
    if (!g_cs_check_tramp) return;
    memcpy(g_cs_check_tramp, target, CS_CHECK_COPY);
    t = g_cs_check_tramp + CS_CHECK_COPY;
    *t++ = 0xff; *t++ = 0x25;
    *(unsigned int *)t = 0; t += 4;
    *(unsigned long long *)t = (unsigned long long)(uintptr_t)(target + CS_CHECK_COPY);
    if (!VirtualProtect(target, 16, PAGE_EXECUTE_READWRITE, &old)) return;
    memset(patch, 0x90, sizeof(patch));
    patch[0] = 0xff; patch[1] = 0x25;
    *(unsigned int *)(patch + 2) = 0;
    *(unsigned long long *)(patch + 6) = (unsigned long long)(uintptr_t)cs_check_hook;
    memcpy(target, patch, sizeof(patch));
    VirtualProtect(target, 16, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    probe_log("connstore probe: installed checkConnection hook at %p", target);
}

__attribute__((noinline))
static unsigned long long __cdecl cs_pump_hook(void *a, void *b)
{
    unsigned long long rv;
    char line[160];
    wsprintfA(line, "pump: FUN_140f530d0(a=%p, b=%p)", a, b);
    sdk_emit(a, __builtin_return_address(0), line);
    rv = ((cs_pump_fn)g_cs_pump_tramp)(a, b);
    return rv;
}

static void cs_pump_install(void)
{
    static const unsigned char expect[CS_PUMP_COPY] = {
        0x40, 0x53,                     /* push rbx */
        0x41, 0x56,                     /* push r14 */
        0x48, 0x83, 0xec, 0x58,         /* sub rsp,0x58 */
        0x4c, 0x8b, 0x71, 0x08,         /* mov rsi,[rcx+8] */
        0x48, 0x8b, 0xda                /* mov rbx,rdx */
    };
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *target, *t, patch[16];
    DWORD old = 0;

    if (!base) return;
    target = base + CS_PUMP_RVA;
    if (!safe_readable(target, CS_PUMP_COPY)) return;
    if (memcmp(target, expect, CS_PUMP_COPY) != 0) {
        probe_log("connstore probe: pump prologue mismatch - skipped");
        return;
    }
    g_cs_pump_tramp = (unsigned char *)VirtualAlloc(NULL, 64, MEM_COMMIT | MEM_RESERVE,
                                                    PAGE_EXECUTE_READWRITE);
    if (!g_cs_pump_tramp) return;
    memcpy(g_cs_pump_tramp, target, CS_PUMP_COPY);
    t = g_cs_pump_tramp + CS_PUMP_COPY;
    *t++ = 0xff; *t++ = 0x25;
    *(unsigned int *)t = 0; t += 4;
    *(unsigned long long *)t = (unsigned long long)(uintptr_t)(target + CS_PUMP_COPY);
    if (!VirtualProtect(target, 16, PAGE_EXECUTE_READWRITE, &old)) return;
    memset(patch, 0x90, sizeof(patch));
    patch[0] = 0xff; patch[1] = 0x25;
    *(unsigned int *)(patch + 2) = 0;
    *(unsigned long long *)(patch + 6) = (unsigned long long)(uintptr_t)cs_pump_hook;
    memcpy(target, patch, sizeof(patch));
    VirtualProtect(target, 16, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    probe_log("connstore probe: installed pump hook at %p", target);
}

/* ---------------- connection-getter probe (v42) ----------------
 * JobManageConnection asks for its connection via the session provider's
 * vtable+0x18 -> FUN_140f15460 -> FUN_140f15510(global+0x60, out, ref).
 * That function returns NULL when the refcounted async object's
 * vtable+0x18 "valid" check is false - which is our state (the job then
 * only sets its name and retries; no frames ever flow). Log the object,
 * its vtable and the slot address so the check can be decompiled.
 * UPC_CONNGET=0 disables. */
#define CONNGET_RVA  0xf15510u
#define CONNGET_COPY 16u

static void hook_install(const char *tag, unsigned long rva,
                         const unsigned char *expect, unsigned copy_n,
                         unsigned char **tramp_out, void *hook_fn);

typedef void *(__cdecl *connget_fn)(void *a, void *b, void *c);
static unsigned char *g_connget_tramp = NULL;
static int g_connget_n = 0;

__attribute__((noinline))
static void *__cdecl connget_hook(void *a, void *b, long long *c)
{
    void *rv;
    unsigned long long ref = 0, obj = 0, vt = 0, slot = 0, outv = 0;
    if (g_connget_n < 40) {
        g_connget_n++;
        if (c && safe_readable(c, 8)) ref = *(unsigned long long *)c;
        if (ref && safe_readable((void *)(uintptr_t)ref, 8))
            obj = *(unsigned long long *)(uintptr_t)ref;
        if (obj && safe_readable((void *)(uintptr_t)obj, 8))
            vt = *(unsigned long long *)(uintptr_t)obj;
        if (vt && safe_readable((void *)(uintptr_t)vt + 0x18, 8))
            slot = *(unsigned long long *)(uintptr_t)(vt + 0x18);
    }
    rv = ((connget_fn)g_connget_tramp)(a, b, c);
    if (g_connget_n <= 40) {
        if (b && safe_readable(b, 8)) outv = *(unsigned long long *)b;
        probe_log("connget: ref=%p obj=%p vt=%p slot18=%p -> out=%p",
                  (void *)(uintptr_t)ref, (void *)(uintptr_t)obj,
                  (void *)(uintptr_t)vt, (void *)(uintptr_t)slot,
                  (void *)(uintptr_t)outv);
    }
    return rv;
}

static void connget_install(void)
{
    static const unsigned char expect[CONNGET_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x10,   /* mov [rsp+0x10],rbx */
        0x48, 0x89, 0x74, 0x24, 0x20,   /* mov [rsp+0x20],rsi */
        0x41, 0x56,                     /* push r14 */
        0x48, 0x83, 0xec, 0x40          /* sub rsp,0x40 */
    };
    hook_install("connget probe", CONNGET_RVA, expect, CONNGET_COPY,
                 &g_connget_tramp, (void *)connget_hook);
}

/* ---------------- conquest server URL probe (v45) ----------------
 * FUN_140c19490 = "get the conquest server URL": it reads the SDK config
 * section "AnnoURLs" / key "ConquestServer" (FUN_140c18b60) and returns
 * it as a std::string in param_2.  The consumer chain is
 * FUN_1400dd1c0 (splits host:port) -> FUN_1400d1740 (connect job ctor).
 * If this string is empty, the game's own login cannot reach any server
 * and it falls back to OFFLINE MODE - which is exactly what we see.
 * std::string layout (MSVC): [0]=char* buf, [8]=size_t len, [16]=cap. */
#define CQSRV_RVA 0xc19490u
#define CQSRV_COPY 25
static unsigned char *g_cqsrv_tramp = NULL;
static int g_cqsrv_n = 0;

typedef void *(__cdecl *cqsrv_fn)(void *, void *, unsigned long long, unsigned long long);

__attribute__((noinline))
static void *__cdecl cqsrv_hook(void *a, void *b, unsigned long long c, unsigned long long d)
{
    void *rv = ((cqsrv_fn)g_cqsrv_tramp)(a, b, c, d);
    /* PN 164 (fallback B): plant the conquest address when the client's own
     * config document cannot supply it. `b` is a std::string (buf/len/cap at
     * +0/+8/+0x10); we only fill it when it is EMPTY and the capacity fits, so
     * this can never fight a real value. Purpose: validate the whole HTTP
     * conquest channel (24 routes) end to end; the config-document shape is a
     * separate, later question. Opt-in: UPC_CONQURL="127.0.0.1:3080". */
    {
        static const char *inject = NULL;
        static int checked = 0;
        if (!checked) {
            static char env[64];
            checked = 1;
            if (GetEnvironmentVariableA("UPC_CONQURL", env, (DWORD)sizeof(env)) > 0 && env[0])
                inject = env;
            if (inject) probe_log("conqurl: will inject \"%s\" when the address is empty", inject);
        }
        if (inject && b && safe_readable(b, 0x18)) {
            char *p = *(char **)b;
            unsigned long long len = *(unsigned long long *)((char *)b + 8);
            unsigned long long cap = *(unsigned long long *)((char *)b + 0x10);
            size_t n = strlen(inject);
            if (p && len == 0 && cap >= (unsigned long long)n && safe_readable(p, n + 1)) {
                memcpy(p, inject, n);
                p[n] = 0;
                *(unsigned long long *)((char *)b + 8) = (unsigned long long)n;
                probe_log("conqurl: injected \"%s\" (%llu chars, cap %llu)", inject, (unsigned long long)n, cap);
            }
        }
    }
    if (g_cqsrv_n < 20) {
        const char *str = "";
        unsigned long long len = 0;
        char *p = NULL;
        g_cqsrv_n++;
        if (b && safe_readable(b, 0x20)) {
            p = *(char **)b;
            len = *(unsigned long long *)((char *)b + 8);
            if (p && len && safe_readable(p, (size_t)(len > 256 ? 256 : len)))
                str = p;
            else if (p && !len) str = "";
        }
        probe_log("cqsrv: out=%p buf=%p len=%llu str=\"%.100s\"",
                  b, (void *)p, len, str);
    }
    return rv;
}

static void cqsrv_install(void)
{
    static const unsigned char expect[CQSRV_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x20,             /* mov [rsp+0x20],rbx */
        0x48, 0x89, 0x54, 0x24, 0x10,             /* mov [rsp+0x10],rdx */
        0x55, 0x56, 0x57,                         /* push rbp,rsi,rdi   */
        0x48, 0x8d, 0x6c, 0x24, 0xb9,             /* lea rbp,[rsp-0x47] */
        0x48, 0x81, 0xec, 0xc0, 0x00, 0x00, 0x00  /* sub rsp,0xC0       */
    };
    hook_install("cqsrv probe", CQSRV_RVA, expect, CQSRV_COPY,
                 &g_cqsrv_tramp, (void *)cqsrv_hook);
}

/* ---------------- conquest connect job probe (v46) ----------------
 * FUN_1400d1740 = the conquest connect JOB constructor: it copies the host
 * std::string (rdx) into the job and stores the port words (r8/r9).  If
 * this fires we know the game really resolved a server address, and we see
 * exactly which host:port it wants.  Called from FUN_1400d0f6f /
 * FUN_1400d1773 right after FUN_1400dd1c0 resolved the address. */
#define CQJOB_RVA 0xd1740u
#define CQJOB_COPY 16
static unsigned char *g_cqjob_tramp = NULL;
static int g_cqjob_n = 0;

__attribute__((noinline))
static void *__cdecl cqjob_hook(void *a, void *b, unsigned long long c, unsigned long long d)
{
    void *rv = ((cqsrv_fn)g_cqjob_tramp)(a, b, c, d);
    if (g_cqjob_n < 20) {
        const char *str = "";
        unsigned long long len = 0;
        char *p = NULL;
        g_cqjob_n++;
        if (b && safe_readable(b, 0x20)) {
            p = *(char **)b;
            len = *(unsigned long long *)((char *)b + 8);
            if (p && len && safe_readable(p, (size_t)(len > 256 ? 256 : len)))
                str = p;
        }
        probe_log("cqjob: job=%p host=%p buf=%p len=%llu str=\"%.100s\" r8=%llu r9=%llu",
                  a, b, (void *)p, len, str, c, d);
    }
    return rv;
}

/* ---------------- RV error BUILDER probe (v49) ----------------
 * FUN_141199620(out, componentString*, code, flag) is where the RV layer
 * CREATEs an error. Logging it names the component and the numeric code of
 * the failure that keeps the game in offline mode. std::string layout:
 * [0]=buf [8]=len [16..]=SSO chars when len < 16. */
#define RVEB_RVA 0x1199620u
#define RVEB_COPY 17
static unsigned char *g_rveb_tramp = NULL;
static int g_rveb_n = 0;

__attribute__((noinline))
static void *__cdecl rveb_hook(void *a, void *b, unsigned long long c, unsigned long long d)
{
    unsigned long long rva = 0;
    {
        unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
        unsigned long long ra = (unsigned long long)(uintptr_t)__builtin_return_address(0);
        if (base && ra > (unsigned long long)(uintptr_t)base &&
            ra < (unsigned long long)(uintptr_t)base + 0x4000000ull)
            rva = ra - (unsigned long long)(uintptr_t)base;
    }
    void *rv = ((cqsrv_fn)g_rveb_tramp)(a, b, c, d);
    if (g_rveb_n < 24 && b && safe_readable(b, 0x20)) {
        unsigned long long len = *(unsigned long long *)((char *)b + 8);
        const char *txt = NULL;
        char tmp[64];
        g_rveb_n++;
        if (len < 16)
            txt = (const char *)b + 16;
        else {
            char *p = *(char **)b;
            if (p && safe_readable(p, 64)) txt = p;
        }
        if (txt) {
            size_t i = 0;
            while (i < 40 && txt[i]) { tmp[i] = txt[i]; i++; }
            tmp[i] = 0;
        } else tmp[0] = 0;
        probe_log("rveb: component=\"%s\" code=%llu flag=%llu PRODUCER=Anno5+0x%llx",
                  tmp, c, d, rva);
    }
    return rv;
}

static void rveb_install(void)
{
    static const unsigned char expect[RVEB_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x08,               /* mov [rsp+0x08],rbx */
        0x57,                                       /* push rdi           */
        0x48, 0x83, 0xec, 0x20,                     /* sub rsp,0x20       */
        0x48, 0x8d, 0x05, 0x3f, 0x6f, 0x4d, 0x00    /* lea rax,[rip+..]   */
    };
    /* v87: ON by default again. The old 10-byte copy produced a corrupt thunk
     * (the v49 "crashed a run once" report) - with the 17-byte prologue it is
     * safe, and it is the only way to see WHO builds a given RV error code
     * (the hook's return address is the producer). UPC_RVEB=0 disables. */
    {
        char dis[8] = {0};
        if (GetEnvironmentVariableA("UPC_RVEB", dis, sizeof(dis)) > 0 && dis[0] == '0') {
            probe_log("rveb probe: off (UPC_RVEB=0)");
            return;
        }
    }
    hook_install("rveb probe", RVEB_RVA, expect, RVEB_COPY,
                 &g_rveb_tramp, (void *)rveb_hook);
}

static void fmt_hex(const unsigned char *b, int n, char *out, int cap);

/* ---------------- RV message formatter probe (v61) ----------------
 * FUN_140d94200(out, rvStringRef*) is the first frame of the LOGIN/7 stack.
 * It dereferences rdx -> object, then uses [obj+8] (data) and [obj+0x18]
 * (int length). Logging those gives the RV's actual message text. */
#define RVFMT_RVA 0xd94200u
#define RVFMT_COPY 15
static unsigned char *g_rvfmt_tramp = NULL;
static int g_rvfmt_n = 0;

__attribute__((noinline))
static void __cdecl rvfmt_hook(void *out, void *ref)
{
    if (g_rvfmt_n < 20 && ref && safe_readable(ref, 8)) {
        void *obj = *(void **)ref;
        char txt[128], hx[80];
        size_t i = 0;
        g_rvfmt_n++;
        txt[0] = 0; hx[0] = 0;
        if (obj && safe_readable(obj, 0x20)) {
            char *data = *(char **)((char *)obj + 8);
            int len = *(int *)((char *)obj + 0x18);
            if (data && safe_readable(data, 64)) {
                fmt_hex((const unsigned char *)data, 24, hx, sizeof(hx));
                while (i < 60 && data[i]) { txt[i] = data[i]; i++; }
                txt[i] = 0;
            }
            probe_log("rvfmt: obj=%p data=%p len=%d hex=%s txt=\"%s\"",
                      obj, (void *)data, len, hx, txt);
        }
    }
    ((void (__cdecl *)(void *, void *))g_rvfmt_tramp)(out, ref);
}

static void rvfmt_install(void)
{
    static const unsigned char expect[RVFMT_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x08,   /* mov [rsp+8],rbx  */
        0x48, 0x89, 0x74, 0x24, 0x10,   /* mov [rsp+0x10],rsi */
        0x57,                           /* push rdi         */
        0x48, 0x83, 0xec, 0x30          /* sub rsp,0x30     */
    };
    hook_install("rvfmt probe", RVFMT_RVA, expect, RVFMT_COPY,
                 &g_rvfmt_tramp, (void *)rvfmt_hook);
}

/* ---------------- RV "set error string" probe (v60) ----------------
 * FUN_141357ee0(endpoint, std::string* msg) is the RV setter that stores the
 * failure message on the endpoint (it appears in the LOGIN/7 stack). Logging
 * msg gives the RV layer's own words for why the login cannot proceed. */
#define RVSET_RVA 0x1357ee0u
#define RVSET_COPY 15
static unsigned char *g_rvset_tramp = NULL;
static int g_rvset_n = 0;

__attribute__((noinline))
static void __cdecl rvset_hook(void *ep, void *msg)
{
    if (g_rvset_n < 20 && msg && safe_readable(msg, 0x30)) {
        char hx[80], hx2[96], txt[96];
        unsigned long long w0 = *(unsigned long long *)msg;
        unsigned long long w1 = *(unsigned long long *)((char *)msg + 8);
        char *p0 = (char *)(uintptr_t)w0, *p1 = (char *)(uintptr_t)w1;
        size_t i = 0;
        g_rvset_n++;
        fmt_hex((const unsigned char *)msg, 32, hx, sizeof(hx));
        hx2[0] = 0; txt[0] = 0;
        if (p0 && safe_readable(p0, 32)) {
            fmt_hex((const unsigned char *)p0, 24, hx2, sizeof(hx2));
            while (i < 60 && p0[i]) { txt[i] = p0[i]; i++; }
            txt[i] = 0;
        }
        probe_log("rvset: ep=%p w0=%p w1=%p raw=%s p0hex=%s p0txt=\"%s\"",
                  ep, (void *)p0, (void *)p1, hx, hx2, txt);
        {
            /* v101: a backtrace here names the client's RMC response handler -
               rvset is called with the result code we put in the reply (PN 105). */
            void *frames[14];
            char st[360];
            unsigned nf = bt_capture(frames, 14, 1);
            bt_format(frames, nf, st, sizeof(st));
            probe_log("rvset stack: %s", st);
        }
    }
    ((void (__cdecl *)(void *, void *))g_rvset_tramp)(ep, msg);
}

static void rvset_install(void)
{
    static const unsigned char expect[RVSET_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x10,   /* mov [rsp+0x10],rbx */
        0x48, 0x89, 0x74, 0x24, 0x18,   /* mov [rsp+0x18],rsi */
        0x57,                           /* push rdi           */
        0x48, 0x83, 0xec, 0x50          /* sub rsp,0x50       */
    };
    hook_install("rvset probe", RVSET_RVA, expect, RVSET_COPY,
                 &g_rvset_tramp, (void *)rvset_hook);
}

/* ---------------- game LOGIN-error probe (v59) ----------------
 * FUN_1400bb510(gameError*) formats the game's own (namespace, code) error pair
 * into UI text - the function whose table maps ("LOGIN", 7) to "The connection
 * to the server has been lost.".  gameError: std::string ns at +0x10
 * (buf/len/SSO at +0x10/+0x18/+0x20), int code at +0x30, bool at +0x34.
 * Logging (ns, code) + a stack tells us which call site raises LOGIN/7. */
#define LOGERR_RVA 0xbb510u
#define LOGERR_COPY 23
static unsigned char *g_logerr_tramp = NULL;
static int g_logerr_n = 0;

__attribute__((noinline))
static void __cdecl logerr_hook(void *a, void *e)
{
    if (g_logerr_n < 30 && e && safe_readable(e, 0x38)) {
        char ns[64];
        unsigned long long len = *(unsigned long long *)((char *)e + 0x18);
        const char *src = NULL;
        size_t i = 0;
        int code = *(int *)((char *)e + 0x30);
        void *frames[8];
        unsigned short nf;
        char st[160];
        int o2 = 0;
        unsigned char *b = (unsigned char *)GetModuleHandleA(NULL);
        g_logerr_n++;
        ns[0] = 0;
        if (len < 16)
            src = (const char *)e + 0x20;
        else {
            char *hp = *(char **)((char *)e + 0x10);
            if (hp && safe_readable(hp, 48)) src = hp;
        }
        if (src) { while (i < 40 && src[i]) { ns[i] = src[i]; i++; } ns[i] = 0; }
        nf = (unsigned short)RtlCaptureStackBackTrace(2, 8, frames, NULL);
        st[0] = 0;
        for (unsigned k = 0; k < nf && o2 < 120; k++)
            o2 += snprintf(st + o2, sizeof(st) - o2, " %llx",
                           (unsigned long long)((unsigned char *)frames[k] - b));
        probe_log("logerr: ns=\"%s\" code=%d stack:%s", ns, code, st);
    }
    ((void (__cdecl *)(void *, void *))g_logerr_tramp)(a, e);
}

static void logerr_install(void)
{
    static const unsigned char expect[LOGERR_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x08,   /* mov [rsp+8],rbx  */
        0x48, 0x89, 0x74, 0x24, 0x10,   /* mov [rsp+0x10],rsi */
        0x48, 0x89, 0x7c, 0x24, 0x18,   /* mov [rsp+0x18],rdi */
        0x55,                           /* push rbp         */
        0x48, 0x8b, 0xec,               /* mov rbp,rsp      */
        0x48, 0x83, 0xec, 0x50          /* sub rsp,0x50     */
    };
    hook_install("logerr probe", LOGERR_RVA, expect, LOGERR_COPY,
                 &g_logerr_tramp, (void *)logerr_hook);
}

/* ---------------- RV state/message probe (v58) ----------------
 * FUN_14119d540(endpoint, state, std::string* msg, flag) is the RV state
 * machine's single entry point: every RV failure funnels through it with a
 * transport state code and a message string. Logging (state, msg) gives the
 * RV layer's own reason - much safer than reading endpoint internals. */
#define RVSIG_RVA 0x119d540u
#define RVSIG_COPY 17
static unsigned char *g_rvsig_tramp = NULL;
static int g_rvsig_n = 0;

__attribute__((noinline))
static void __cdecl rvsig_hook(void *ep, unsigned long long state, void *msg, unsigned long long flag)
{
    if (g_rvsig_n < 40) {
        char txt[128];
        unsigned long long len = 0;
        const char *src = NULL;
        size_t i = 0;
        g_rvsig_n++;
        txt[0] = 0;
        if (msg && safe_readable(msg, 0x20)) {
            len = *(unsigned long long *)((char *)msg + 8);
            if (len < 16 && safe_readable((char *)msg + 16, 16))
                src = (const char *)msg + 16;
            else {
                char *hp = *(char **)msg;
                if (hp && safe_readable(hp, 96)) src = hp;
            }
        }
        if (src) { while (i < 100 && src[i]) { txt[i] = src[i]; i++; } txt[i] = 0; }
        probe_log("rvsig: ep=%p state=%llu flag=%llu len=%llu msg=\"%s\"",
                  ep, state, flag, len, txt);
    }
    ((void (__cdecl *)(void *, unsigned long long, void *, unsigned long long))g_rvsig_tramp)
        (ep, state, msg, flag);
}

static void rvsig_install(void)
{
    static const unsigned char expect[RVSIG_COPY] = {
        0x40, 0x53,                          /* push rbx              */
        0x57,                                /* push rdi              */
        0x41, 0x56,                          /* push r14              */
        0x48, 0x83, 0xec, 0x70,              /* sub rsp,0x70          */
        0x48, 0x89, 0xb4, 0x24, 0xa0, 0x00, 0x00, 0x00  /* mov [rsp+0xa0],rsi */
    };
    hook_install("rvsig probe", RVSIG_RVA, expect, RVSIG_COPY,
                 &g_rvsig_tramp, (void *)rvsig_hook);
}

/* ---------------- RV login diagnosis probe (v57) ----------------
 * FUN_141358080 is where the RV layer decides the login cannot proceed:
 *   if (*(int*)(this + 0xbc) == 1) -> error 100;
 *   else it takes the std::string at this + 0xF8 (= param_1 + 0x1f when
 *   param_1 is a longlong*) and hands it to vtable+0x60, which ends in the
 *   error formatter.  Logging that string + the int gives the RV layer's own
 *   reason for failing right after our rendezvous configuration is read. */
#define RVFAIL_RVA 0x1358080u
#define RVFAIL_COPY 13
static unsigned char *g_rvfail_tramp = NULL;
static int g_rvfail_n = 0;

__attribute__((noinline))
static void __cdecl rvfail_hook(void *p)
{
    if (g_rvfail_n < 20 && p && safe_readable(p, 0x120)) {
        char str[128];
        unsigned long long len = *(unsigned long long *)((char *)p + 0x100);
        const char *src = NULL;
        size_t i = 0;
        int code = *(int *)((char *)p + 0xbc);
        g_rvfail_n++;
        str[0] = 0;
        if (len < 16 && safe_readable((char *)p + 0x108, 16))
            src = (const char *)p + 0x108;          /* SSO chars */
        else {
            char *hp = *(char **)((char *)p + 0xF8);
            if (hp && safe_readable(hp, 96)) src = hp;
        }
        if (src) { while (i < 100 && src[i]) { str[i] = src[i]; i++; } str[i] = 0; }
        probe_log("rvfail: this=%p code=%d len=%llu text=\"%s\"", p, code, len, str);
    }
    ((void (__cdecl *)(void *))g_rvfail_tramp)(p);
}

static void rvfail_install(void)
{
    static const unsigned char expect[RVFAIL_COPY] = {
        0x40, 0x57,                          /* push rdi        */
        0x48, 0x83, 0xec, 0x50,              /* sub rsp,0x50    */
        0x83, 0xb9, 0xbc, 0x00, 0x00, 0x00, 0x01  /* cmp [rcx+0xbc],1 */
    };
    hook_install("rvfail probe", RVFAIL_RVA, expect, RVFAIL_COPY,
                 &g_rvfail_tramp, (void *)rvfail_hook);
}

/* ---------------- RV transport-URL state probe (v55) ----------------
 * FUN_14134fcc0(urlSet*) parses (once) the transport URL set and returns its
 * state: 0 = unparsed/invalid, 1 = prudp, 2 = udp, 3 = ws/wss.  The RV
 * completion (FUN_14134b44f) only takes its success branch when this is 3,
 * so logging it tells us whether the client accepts what we serve in
 * "rendezvous/configuration". */
/* ---------------- RV transport-URL probe (v76) ----------------
 * FUN_14134fd20 is the URL scheme parser: it copies the scheme out of the URL
 * object's std::string, compares it against "prudp"/"udp"/"ws"/"wss" and stores
 * 1/2/3 at obj+8 (0 = unrecognised).  Hooking it answers the only question
 * that matters for the transport: does the game ever actually parse a PRUDP
 * URL in a live run - and which one.
 * (The old target 0x134fcc0 could not be hooked safely: its 3rd instruction is
 * a rel8 jcc, so no >=14-byte relocation window exists.) */
#define RVURL_RVA 0x134fd20u
#define RVURL_COPY 15u
static unsigned char *g_rvurl_tramp = NULL;
static int g_rvurl_n = 0;

__attribute__((noinline))
static void __cdecl rvurl_hook(void *obj)
{
    char url[160];
    char *sp = NULL;
    unsigned long long len = 0;
    int scheme8 = -1;
    url[0] = 0;
    if (obj && safe_readable(obj, 0x18)) {
        unsigned long long *o = (unsigned long long *)obj;
        len = o[1];
        if (len < 16 && safe_readable((char *)obj + 16, 16)) sp = (char *)obj + 16;  /* SSO */
        else if (safe_readable((void *)(uintptr_t)o[0], 160)) sp = (char *)(uintptr_t)o[0];
        if (sp) {
            size_t k = 0;
            while (k < 150 && sp[k]) { url[k] = sp[k]; k++; }
            url[k] = 0;
        }
    }
    ((void (__cdecl *)(void *))g_rvurl_tramp)(obj);
    if (obj && safe_readable(obj, 0x10)) scheme8 = *(int *)((char *)obj + 8);
    if (g_rvurl_n < 30) {
        g_rvurl_n++;
        probe_log("rvurl[%d]: obj=%p len=%llu scheme=%d url=\"%s\"",
                  g_rvurl_n, obj, len, scheme8, url);
    }
}

static void rvstate_install(void)
{
    static const unsigned char expect[RVURL_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x18,               /* mov [rsp+0x18],rbx */
        0x55, 0x56, 0x57,                           /* push rbp,rsi,rdi   */
        0x48, 0x81, 0xec, 0xa0, 0x00, 0x00, 0x00    /* sub rsp,0xa0       */
    };
    hook_install("rvurl probe", RVURL_RVA, expect, RVURL_COPY,
                 &g_rvurl_tramp, (void *)rvurl_hook);
}

/* ---------------- RendezVous error reporter probe (v48) ----------------
 * FUN_140d8d8c0(rdvError*) formats "FAILED: <component> returned code <N>
 * (see ... error=<a>%3A<b> for details)".  The struct: std::string at +0,
 * +0x20 = int code, +0x24 = failed flag.  Logging it gives us the exact RV
 * error code the game hits while bootstrapping its online login. */
#define RVERR_RVA 0xd8d8c0u
#define RVERR_COPY 19
static unsigned char *g_rverr_tramp = NULL;
static int g_rverr_n = 0;

__attribute__((noinline))
static void *__cdecl rverr_hook(void *a, void *b, unsigned long long c, unsigned long long d)
{
    void *rv = ((cqsrv_fn)g_rverr_tramp)(a, b, c, d);
    if (g_rverr_n < 20 && a && safe_readable(a, 0x28)) {
        char comp[48];
        unsigned long long len = *(unsigned long long *)((char *)a + 8);
        const char *src = NULL;
        int code = *(int *)((char *)a + 0x20);
        int failed = *(unsigned char *)((char *)a + 0x24);
        size_t i = 0;
        g_rverr_n++;
        comp[0] = 0;
        if (len < 16 && safe_readable((char *)a + 16, 16))
            src = (const char *)a + 16;               /* SSO */
        else {
            char *p = *(char **)a;
            if (p && safe_readable(p, 48)) src = p;
        }
        if (src) { while (i < 40 && src[i]) { comp[i] = src[i]; i++; } comp[i] = 0; }
        {
            void *frames[16];
            unsigned nf = 0;
            char st[400];
            nf = bt_capture(frames, 16, 2);
            bt_format(frames, nf, st, sizeof(st));
            probe_log("rverr: component=\"%s\" len=%llu code=%d failed=%d stack:%s",
                      comp, len, code, failed, st);
        }
    }
    return rv;
}

static void rverr_install(void)
{
    static const unsigned char expect[RVERR_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x18,   /* mov [rsp+0x18],rbx */
        0x48, 0x89, 0x74, 0x24, 0x20,   /* mov [rsp+0x20],rsi */
        0x48, 0x89, 0x54, 0x24, 0x10,   /* mov [rsp+0x10],rdx */
        0x55, 0x57, 0x41, 0x56          /* push rbp,rdi,r14   */
    };
    hook_install("rverr probe", RVERR_RVA, expect, RVERR_COPY,
                 &g_rverr_tramp, (void *)rverr_hook);
}

static void cqjob_install(void)
{
    static const unsigned char expect[CQJOB_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x18,                    /* mov [rsp+0x18],rbx */
        0x55, 0x56, 0x57, 0x41, 0x54, 0x41, 0x55,        /* push rbp..r13      */
        0x41, 0x56, 0x41, 0x57                            /* push r14,r15       */
    };
    hook_install("cqjob probe", CQJOB_RVA, expect, CQJOB_COPY,
                 &g_cqjob_tramp, (void *)cqjob_hook);
}

/* ---------------- notification-registry injection (v38) ----------------
 * The SDK's notification-registry std::map<int,int> lives IN the
 * connection object: sentinel head = *(conn+0x10), root = head->_Parent
 * (head+0x08); node: left@0 parent@8 right@0x10 color@0x18 isnil@0x19
 * key int@0x1c value int@0x20 (pair aligns to 4), nil children point
 * back at the head; head->_Left/_Right = leftmost/rightmost caches.
 * PROVEN layout: FUN_140ee2080 itself walks id 9 this way, and the
 * identical idiom in FUN_140ed95d0 is the URL-map lookup we already
 * dump/inject. THE OWNER: inside FUN_140ee2080,
 *   X = *(self+0xa0);  conn = *(X+0x28);  head = *(conn+0x10)
 * (v37 wrongly used *(X+0x20) - that object holds the +0x138 parameter
 * map, and its +0x10 is NOT a tree: v37 walked garbage, wrote into
 * image .data and the game exit(-1)'d from 4 threads. Strict
 * validation below refuses anything that is not this exact tree.)
 * Gates that read it:
 *   - FUN_140ee2080 (this function!) and the ws-login pump
 *     FUN_140f530d0 ("Failed to get the connection Id") walk id 9;
 *     the pump proceeds only when id-9 state is NOT in [2,5];
 *   - the auth push FUN_140f0c590 ("Pushing incoming Authentication
 *     notification") pushes only when id-26 state == 2 or the
 *     conn+0x148 ready byte is 0 (ours is 1).
 * Real entries only appear once the client's own flow advances - which
 * never happens - so inject {9:1} and {26:2} (SDK heap node alloc
 * FUN_141391970(0x28,4,0,6.0f), BST leaf insert, bounded+validated).
 * UPC_REGINJECT=0 disables. */
static int g_reginject = -1;               /* -1 = env not read yet */
static unsigned char *g_reg_owner = NULL;  /* last conn we ensured */
static unsigned char *g_reg_head = NULL;   /* its registry head */

/* validate: head must be a readable sentinel (isnil != 0); root must be
 * head (empty tree) or a readable non-nil node. Returns 0 = refuse. */
static int reg_tree_ok(unsigned char *head)
{
    unsigned char *root;
    if (!head || !safe_readable(head, 0x28)) return 0;
    if (*(char *)(head + 0x19) == 0) return 0;      /* not the sentinel */
    root = *(unsigned char **)(head + 0x08);
    if (!root) return 0;                            /* never null in a live tree */
    if (root == head) return 1;                     /* empty tree */
    if (!safe_readable(root, 0x28) || *(char *)(root + 0x19) != 0) return 0;
    return 1;
}

/* bounded, validated lower_bound (mirror of the FUN_140ee5700 walk).
 * Returns prev node (may be head) with *ok=1, or *ok=0 = tree bad. */
static unsigned char *reg_lower(unsigned char *head, int key, int *ok)
{
    unsigned char *prev = head, *cur;
    int steps = 0;
    *ok = 0;
    if (!reg_tree_ok(head)) return head;
    cur = *(unsigned char **)(head + 0x08);
    while (cur && cur != head) {
        if (++steps > 32) return head;              /* runaway - refuse */
        if (!safe_readable(cur, 0x28) || *(char *)(cur + 0x19) != 0)
            return head;                            /* bad node - refuse */
        prev = cur;
        cur = (*(int *)(cur + 0x1c) < key)
            ? *(unsigned char **)(cur + 0x10)       /* right */
            : *(unsigned char **)cur;               /* left */
        if (cur && !safe_readable(cur, 0x28)) return head;
    }
    *ok = 1;
    return prev;
}

/* one-shot bounded in-order dump of the registry (per head+tag) */
static void reg_dump(unsigned char *head, const char *tag)
{
    static unsigned char *done_b = NULL, *done_a = NULL;
    unsigned char **latch = (tag[0] == 'b') ? &done_b : &done_a;
    unsigned char *root, *stack[32];
    int sp = 0, n = 0;

    if (*latch == head) return;
    *latch = head;
    if (!reg_tree_ok(head)) {
        probe_log("regmap[%s]: NOT A TREE (head=%p)", tag, head);
        return;
    }
    root = *(unsigned char **)(head + 0x08);
    if (root == head) {
        probe_log("regmap[%s]: EMPTY (head=%p)", tag, head);
        return;
    }
    stack[sp++] = root;
    while (sp > 0 && n < 16) {
        unsigned char *node = stack[--sp], *l, *r;
        if (!safe_readable(node, 0x28)) continue;
        l = *(unsigned char **)node;
        r = *(unsigned char **)(node + 0x10);
        probe_log("regmap[%s][%d] id=%d state=%d", tag, n,
                  *(int *)(node + 0x1c), *(int *)(node + 0x20));
        n++;
        if (r && r != head && safe_readable(r, 0x28) &&
            *(char *)(r + 0x19) == 0 && sp < 30) stack[sp++] = r;
        if (l && l != head && safe_readable(l, 0x28) &&
            *(char *)(l + 0x19) == 0 && sp < 30) stack[sp++] = l;
    }
    probe_log("regmap[%s]: %d entries (head=%p)", tag, n, head);
}

/* 0 = already correct, 1 = added, 2 = existing state fixed, -1 = error */
static int reg_insert_one(unsigned char *base, unsigned char *head,
                          int key, int val)
{
    rk_alloc_fn allocf = (rk_alloc_fn)(base + RK_ALLOC_RVA);
    unsigned char *node, *cur, *parent, *root;
    int ok;

    root = *(unsigned char **)(head + 0x08);
    if (root == head) {                             /* empty tree */
        node = (unsigned char *)allocf(0x28, 4, 0, 0x40c00000u);
        if (!node) return -1;
        memset(node, 0, 0x28);
        *(void **)(node + 0x00) = head;
        *(void **)(node + 0x08) = head;             /* parent = sentinel */
        *(void **)(node + 0x10) = head;
        *(node + 0x18) = 0;
        *(node + 0x19) = 0;
        *(int *)(node + 0x1c) = key;
        *(int *)(node + 0x20) = val;
        *(void **)(head + 0x08) = node;
        *(void **)(head + 0x00) = node;
        *(void **)(head + 0x10) = node;
        return 1;
    }
    reg_lower(head, key, &ok);                      /* validates the tree */
    if (!ok) return -1;
    cur = root;
    parent = head;
    while (cur && cur != head) {
        if (!safe_readable(cur, 0x28) || *(char *)(cur + 0x19) != 0)
            return -1;
        parent = cur;
        if (*(int *)(cur + 0x1c) == key) {          /* exists: fix state */
            if (*(int *)(cur + 0x20) == val) return 0;
            *(int *)(cur + 0x20) = val;
            return 2;
        }
        cur = (key < *(int *)(cur + 0x1c))
            ? *(unsigned char **)cur
            : *(unsigned char **)(cur + 0x10);
    }
    if (parent == head) return -1;
    node = (unsigned char *)allocf(0x28, 4, 0, 0x40c00000u);
    if (!node) return -1;
    memset(node, 0, 0x28);
    *(void **)(node + 0x00) = head;
    *(void **)(node + 0x10) = head;
    *(node + 0x18) = 0;
    *(node + 0x19) = 0;
    *(int *)(node + 0x1c) = key;
    *(int *)(node + 0x20) = val;
    *(void **)(node + 0x08) = parent;
    if (key < *(int *)(parent + 0x1c)) *(void **)parent = node;
    else *(void **)(parent + 0x10) = node;
    /* refresh the head's leftmost / rightmost caches */
    cur = *(unsigned char **)(head + 0x08);
    while (cur && cur != head && safe_readable(cur, 0x28) &&
           *(char *)(cur + 0x19) == 0) {
        unsigned char *l = *(unsigned char **)cur;
        if (!l || l == head || *(char *)(l + 0x19) != 0) break;
        cur = l;
    }
    *(void **)(head + 0x00) = cur;
    cur = *(unsigned char **)(head + 0x08);
    while (cur && cur != head && safe_readable(cur, 0x28) &&
           *(char *)(cur + 0x19) == 0) {
        unsigned char *r = *(unsigned char **)(cur + 0x10);
        if (!r || r == head || *(char *)(r + 0x19) != 0) break;
        cur = r;
    }
    *(void **)(head + 0x10) = cur;
    return 1;
}

/* id 9 state 1 : pump FUN_140f530d0 + FUN_140ee2080 want state NOT in [2,5]
 * id 26 state 2: auth push FUN_140f0c590 wants state == 2 */
static void regmap_ensure(unsigned char *owner)
{
    unsigned char *base, *head, *store;
    int a, b, ok9, ok26, d9;

    if (g_reginject < 0) {
        char dis[8] = {0};
        g_reginject = !(GetEnvironmentVariableA("UPC_REGINJECT", dis,
                                                sizeof(dis)) > 0 && dis[0] == '0');
        probe_log("regmap inject: %s",
                  g_reginject ? "enabled" : "disabled (UPC_REGINJECT=0)");
    }
    if (!g_reginject || !owner || !safe_readable(owner, 0x160)) return;
    if (!safe_readable(owner + 0x10, 8)) return;
    head = *(unsigned char **)(owner + 0x10);
    if (!reg_tree_ok(head)) {
        if (g_reg_owner != owner) {     /* only log once per owner */
            probe_log("regmap: *(owner=%p +0x10)=%p is not a registry tree - no write",
                      owner, head);
            g_reg_owner = owner;
        }
        return;
    }
    if (owner == g_reg_owner && head == g_reg_head) return;  /* already ensured */
    /* read-only observation first: what do the two lookups see right now? */
    reg_lower(head, 9, &ok9);
    reg_lower(head, 26, &ok26);
    d9 = *(int *)(owner + 0x08);
    probe_log("regmap: owner=%p head=%p default-state=%d (lookups ok9=%d ok26=%d)",
              owner, head, d9, ok9, ok26);
    reg_dump(head, "before");
    base = (unsigned char *)GetModuleHandleA(NULL);
    if (!base) return;
    a = reg_insert_one(base, head, 9, 2);   /* v41: 2 = [2,5] branch -> param websocketHost path */
    b = reg_insert_one(base, head, 26, 2);
    if (a < 0 || b < 0) {
        probe_log("regmap inject: FAILED (owner=%p head=%p id9=%d id26=%d) - will retry",
                  owner, head, a, b);
        return;                       /* do not latch; retry on next probe call */
    }
    g_reg_owner = owner;
    g_reg_head = head;
    store = *(unsigned char **)(owner + 0x148);
    probe_log("regmap inject: owner=%p head=%p id9=%d id26=%d ready-store=%p",
              owner, head, a, b, store);
    reg_dump(head, "after");
}

/* ---------------- connid-pump owner probe (v39) ----------------
 * FUN_140f541f6 (0xf541f6..0xf548a7) prints "Failed to get the connection
 * Id from the connection services." when its registry walk for id 9
 * yields state in [2,5] OR the owner's ready byte (*(owner+0x148)+0x10)
 * is 0 -- owner = r9 (the caller sets it; tail-shared entry, no static
 * callers). v38 injected {9:1} into the owner reachable from
 * FUN_140ee2080 (cs_check), yet the error persists -> either r9 is a
 * DIFFERENT owner or its ready byte is 0. This probe logs r9, its head,
 * default state, the id-9 state it actually sees, its ready byte, and
 * whether it matches the injected owner/head. Naked hook: entry saves
 * rbx/rdi with two 8-byte movs (16 bytes); we replay them and jmp back
 * to +16. UPC_PUMP170=0 disables. */
#define PUMP170_RVA  0xf541f6u
#define PUMP170_COPY 16u

static unsigned char *g_pump170_ret = NULL;
static int g_pump170 = 1;

void pump170_log(void *r9, void *r12)   /* referenced from asm */
{
    unsigned char *owner = (unsigned char *)r9;
    unsigned char *head = NULL, *store = NULL, *node;
    unsigned char *x2 = NULL, *owner2 = NULL, *store2 = NULL;
    int dflt = -1, state = -2, ok = 0, ready = -1, ready2 = -1;

    if (!safe_readable(owner, 0x150)) {
        probe_log("pump170: r9=%p unreadable - skipped", r9);
        return;
    }
    dflt = *(int *)(owner + 8);
    head = *(unsigned char **)(owner + 0x10);
    if (reg_tree_ok(head)) {
        node = reg_lower(head, 9, &ok);
        state = (ok && node != head) ? *(int *)(node + 0x20) : dflt;
    }
    store = *(unsigned char **)(owner + 0x148);
    if (store && safe_readable(store, 0x18)) ready = *(unsigned char *)(store + 0x10);
    /* the gate's ready byte comes from *( *(r12+0xa0) + 0x28 ) - a DIFFERENT
     * chain than r9; if r12 is not our context object it can be a second
     * connection-services instance whose store is not ready */
    if (r12 && safe_readable((char *)r12 + 0xa8, 8)) {
        x2 = *(unsigned char **)((char *)r12 + 0xa0);
        if (x2 && safe_readable(x2, 0x30)) {
            owner2 = *(unsigned char **)(x2 + 0x28);
            if (owner2 && safe_readable(owner2, 0x150)) {
                store2 = *(unsigned char **)(owner2 + 0x148);
                if (store2 && safe_readable(store2, 0x18))
                    ready2 = *(unsigned char *)(store2 + 0x10);
            }
        }
    }
    probe_log("pump170: r9=%p head=%p default=%d id9(state ok=%d)=%d ready=%d injOwner=%d injHead=%d",
              r9, head, dflt, ok, state, ready,
              owner == g_reg_owner, head == g_reg_head);
    probe_log("pump170: r12=%p obj2=%p owner2=%p ready2=%d owner2==owner=%d",
              r12, x2, owner2, ready2, owner2 == owner);
}

__attribute__((naked)) static void __cdecl pump170_hook(void)
{
    __asm__ __volatile__(
        "push %r15\n\t"
        "push %r9\n\t"
        "push %r10\n\t"
        "push %r11\n\t"
        "push %rax\n\t"
        "push %rcx\n\t"
        "push %rdx\n\t"
        "push %r8\n\t"
        "push %r12\n\t"
        "mov  %rsp, %r15\n\t"
        "and  $-16, %rsp\n\t"
        "sub  $0x20, %rsp\n\t"
        "mov  0x38(%r15), %rcx\n\t"      /* saved r9 */
        "mov  (%r15), %rdx\n\t"          /* saved r12 */
        "call pump170_log\n\t"
        "mov  %r15, %rsp\n\t"
        "pop  %r12\n\t"
        "pop  %r8\n\t"
        "pop  %rdx\n\t"
        "pop  %rcx\n\t"
        "pop  %rax\n\t"
        "pop  %r11\n\t"
        "pop  %r10\n\t"
        "pop  %r9\n\t"
        "pop  %r15\n\t"
        "mov  %rbx, 0x3b8(%rsp)\n\t"     /* replay the 2 saved prologue movs */
        "mov  %rdi, 0x3c0(%rsp)\n\t"
        "jmp  *g_pump170_ret(%rip)\n\t"
    );
}

static void pump170_install(void)
{
    static const unsigned char expect[PUMP170_COPY] = {
        0x48, 0x89, 0x9c, 0x24, 0xb8, 0x03, 0x00, 0x00,  /* mov [rsp+0x3b8],rbx */
        0x48, 0x89, 0xbc, 0x24, 0xc0, 0x03, 0x00, 0x00   /* mov [rsp+0x3c0],rdi */
    };
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *target, patch[16];
    DWORD old = 0;

    if (!base) return;
    target = base + PUMP170_RVA;
    if (!safe_readable(target, PUMP170_COPY)) return;
    if (memcmp(target, expect, PUMP170_COPY) != 0) {
        probe_log("pump170 probe: prologue mismatch - skipped");
        return;
    }
    g_pump170_ret = target + PUMP170_COPY;
    if (!VirtualProtect(target, 16, PAGE_EXECUTE_READWRITE, &old)) return;
    memset(patch, 0x90, sizeof(patch));
    patch[0] = 0xff; patch[1] = 0x25;
    *(unsigned int *)(patch + 2) = 0;
    *(unsigned long long *)(patch + 6) = (unsigned long long)(uintptr_t)pump170_hook;
    memcpy(target, patch, sizeof(patch));
    VirtualProtect(target, 16, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    probe_log("pump170 probe: installed at %p", target);
}

__attribute__((noinline))
static void *__cdecl rdname_hook(void *out, void *store, void *name,
                                 unsigned env, unsigned a5, unsigned char a6)
{
    void *ret;
    char line[384], nm[64], val[128];
    int nl, vl;
    {
        unsigned char *b = (unsigned char *)GetModuleHandleA(NULL);
        urlmap_inject(b, store);
    }
    urlmap_dump(store);
    ret = ((rdname_fn)g_rdname_tramp)(out, store, name, env, a5, a6);
    nl = str_read(name, nm, sizeof(nm));
    vl = str_read(out, val, sizeof(val));
    if (vl <= 0)
        wsprintfA(line, "rdv lookup: store=%p name=%s -> MISS (env=%u seg=%u)",
                  store, nl >= 0 ? nm : "?", env, a5);
    else
        wsprintfA(line, "rdv lookup: store=%p name=%s -> \"%s\"", store, nl >= 0 ? nm : "?", val);
    sdk_emit(out, __builtin_return_address(0), line);
    return ret;
}

static void rdname_hook_install(void)
{
    static const unsigned char expect[RDNAME_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x10,   /* mov [rsp+0x10], rbx */
        0x55,                            /* push rbp */
        0x56,                            /* push rsi */
        0x57,                            /* push rdi */
        0x41, 0x54,                      /* push r12 */
        0x41, 0x55,                      /* push r13 */
        0x41, 0x56,                      /* push r14 */
        0x41, 0x57                       /* push r15 */
    };
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *target, *t, patch[16];
    DWORD old = 0;

    if (!base) return;
    target = hook_target("TestRdName", RDNAME_RVA);
    if (!safe_readable(target, RDNAME_COPY)) {
        probe_log("rdv probe: target %p not readable - skipped", target);
        return;
    }
    if (memcmp(target, expect, RDNAME_COPY) != 0) {
        probe_log("rdv probe: prologue mismatch at %p - skipped", target);
        return;
    }
    g_rdname_tramp = (unsigned char *)VirtualAlloc(NULL, 64, MEM_COMMIT | MEM_RESERVE,
                                                   PAGE_EXECUTE_READWRITE);
    if (!g_rdname_tramp) { probe_log("rdv probe: VirtualAlloc failed"); return; }
    memcpy(g_rdname_tramp, target, RDNAME_COPY);
    t = g_rdname_tramp + RDNAME_COPY;
    *t++ = 0xff; *t++ = 0x25;
    *(unsigned int *)t = 0; t += 4;
    *(unsigned long long *)t = (unsigned long long)(uintptr_t)(target + RDNAME_COPY);

    if (!VirtualProtect(target, 16, PAGE_EXECUTE_READWRITE, &old)) {
        probe_log("rdv probe: VirtualProtect failed (%lu)", GetLastError());
        return;
    }
    memset(patch, 0x90, sizeof(patch));
    patch[0] = 0xff; patch[1] = 0x25;
    *(unsigned int *)(patch + 2) = 0;
    *(unsigned long long *)(patch + 6) = (unsigned long long)(uintptr_t)rdname_hook;
    memcpy(target, patch, sizeof(patch));
    VirtualProtect(target, 16, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    probe_log("rdv probe: installed lookup hook at %p (rva 0x%x)", target, RDNAME_RVA);
}

/* ---------------- apply-store probe (v20) ----------------
 *
 * FUN_140f21870 = JobRequestParameters::reportOutcome - parses the
 * parameters response and inserts every member into a store at
 * *(job+0x60)+0x10 (map operator[] FUN_140e96f90). Log that store pointer
 * so we can compare it with the store the URL lookups use (rdv probe
 * logs store=%p) - if they differ, the applied parameters land in a map
 * the URL consumers never read. Low frequency: 2 calls per login. */

#define APPLY_RVA   0xf21870u
#define APPLY_COPY  15u

typedef void (__cdecl *apply_fn)(void *job);

static unsigned char *g_apply_tramp = NULL;

__attribute__((noinline))
static void __cdecl apply_hook(void *job)
{
    unsigned long long storeobj = 0;
    if (job && safe_readable(job, 0x70))
        storeobj = *(unsigned long long *)((char *)job + 0x60);
    char line[128];
    wsprintfA(line, "apply: job=%p jobstore=%p map=%p", job,
              (void *)(uintptr_t)storeobj, (void *)(uintptr_t)(storeobj + 0x10));
    sdk_emit(job, __builtin_return_address(0), line);
    ((apply_fn)g_apply_tramp)(job);
}

static void apply_hook_install(void)
{
    static const unsigned char expect[APPLY_COPY] = {
        0x40, 0x55,                     /* push rbp */
        0x56,                           /* push rsi */
        0x41, 0x56,                     /* push r14 */
        0x41, 0x57,                     /* push r15 */
        0x48, 0x8d, 0xac, 0x24, 0xb8, 0xf9, 0xff, 0xff /* lea rbp,[rsp-0x648] */
    };
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *target, *t, patch[16];
    DWORD old = 0;

    if (!base) return;
    target = hook_target("TestApply", APPLY_RVA);
    if (!safe_readable(target, APPLY_COPY)) {
        probe_log("apply probe: target %p not readable - skipped", target);
        return;
    }
    if (memcmp(target, expect, APPLY_COPY) != 0) {
        probe_log("apply probe: prologue mismatch at %p - skipped", target);
        return;
    }
    g_apply_tramp = (unsigned char *)VirtualAlloc(NULL, 64, MEM_COMMIT | MEM_RESERVE,
                                                  PAGE_EXECUTE_READWRITE);
    if (!g_apply_tramp) { probe_log("apply probe: VirtualAlloc failed"); return; }
    memcpy(g_apply_tramp, target, APPLY_COPY);
    t = g_apply_tramp + APPLY_COPY;
    *t++ = 0xff; *t++ = 0x25;
    *(unsigned int *)t = 0; t += 4;
    *(unsigned long long *)t = (unsigned long long)(uintptr_t)(target + APPLY_COPY);

    if (!VirtualProtect(target, 16, PAGE_EXECUTE_READWRITE, &old)) {
        probe_log("apply probe: VirtualProtect failed (%lu)", GetLastError());
        return;
    }
    memset(patch, 0x90, sizeof(patch));
    patch[0] = 0xff; patch[1] = 0x25;
    *(unsigned int *)(patch + 2) = 0;
    *(unsigned long long *)(patch + 6) = (unsigned long long)(uintptr_t)apply_hook;
    memcpy(target, patch, sizeof(patch));
    VirtualProtect(target, 16, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    probe_log("apply probe: installed hook at %p (rva 0x%x)", target, APPLY_RVA);
}

/* ---------------- conquest-layer probes (v22, default ON) ----------------
 *
 * rdps::CPlatformServiceEngineWin = the UPC->conquest bridge singleton
 * (ctor FUN_140c15150; engine ptr at DAT_142606258, CConnectionClientWin
 * at DAT_142606288). Engine fields:
 *   +0x10  "authenticated" flag (SetAuthenticated FUN_140c1e7d0)
 *   +0x14  userGet state (0->1->2; 2 => FUN_140c1e540 copies ticket)
 *   +0x118 ubiservices FACADE handle (needed by FacadeInternal::
 *          createSession FUN_140ebfe00 - dereferenced unconditionally!)
 *   +0x120 job submitter
 *   +0x150 ticket std::string (copied from UPC_TicketGet_Extended)
 *   +0x160 that string's SIZE field (non-zero == "profile ready" gate
 *          CProfileClientWin::slot8 FUN_140c1ef10)
 * rdps::CConnectionClient::Connect (FUN_140c17d20, rva 0xc17d20) checks
 * credentials (GetCredentials FUN_140c263e0 reads the ticket string,
 * empty => "Missing credentials or no login ticket available"), then
 * submits the SDK job "createSession" whose completion FUN_140c186b0
 * (rva 0xc186b0) on success triggers the OnlineConfiguration fetch
 * (FUN_140c1d410) and on failure writes "<li>Error %d: %s</li>".
 * FUN_1401363f0 calls Connect ONCE at CGameApp init; the manager class
 * has a second Connect entry (FUN_140dd1b90, rva 0xdd1b90, vtable slot).
 * THEORY under test: Connect fires before the SDK facade/engine+0x118
 * exists -> createSession is lost -> conquest never retries.
 * All three hooks are LOW FREQUENCY (startup + UI actions), far from the
 * forbidden parameters apply path. */

#define CONNECT_RVA   0xc17d20u
#define CONNECT_COPY  16u   /* 3x mov [rsp+x],reg + push rbp - complete */
#define CONQDONE_RVA  0xc186b0u
#define CONQDONE_COPY 16u   /* mov [rsp+8],rbx + 7 pushes - complete */
#define MGRCONN_RVA   0xdd1b90u
#define MGRCONN_COPY  19u   /* mov/pushes + lea rbp,[rsp-0x100] - ends AT 19 */

typedef unsigned char *__cdecl connect_fn(void *client, unsigned long long a2,
                                          unsigned long long a3, unsigned long long a4,
                                          unsigned long long a5, unsigned long long a6);
typedef void (__cdecl *conqdone_fn)(void *self, void *res);
typedef unsigned char *__cdecl mgrconn_fn(void *mgr, void *cb);

static unsigned char *g_connect_tramp = NULL;
static unsigned char *g_conqdone_tramp = NULL;
static unsigned char *g_mgrconn_tramp = NULL;
static int g_conquest_connect_allowed = 0;   /* UPC_CONQUEST_CONNECT=1: never suppress */
static unsigned long g_conquest_startup_ms = 15000; /* suppress window after load */
static unsigned long g_load_tick = 0;

/* generic prologue hook installer (whole instructions only!) */
static void hook_install(const char *tag, unsigned long rva,
                         const unsigned char *expect, unsigned copy_n,
                         unsigned char **tramp_out, void *hook_fn)
{
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *target, *t, patch[24];
    DWORD old = 0;

    if (!base) return;
    target = base + rva;
    /* v69 BUGFIX: the patch is a 14-byte "jmp qword ptr [rip+0]" thunk
     * (6 bytes of instruction + 8 bytes of absolute pointer at target+6).
     * The old code wrote only copy_n bytes, so every probe with copy_n < 14
     * installed a jump through 8 bytes of the ORIGINAL code as if they were
     * an address - i.e. a corrupt hook that either crashed or silently never
     * fired. That is why "the rvstep probe never fires in a live run"
     * (PROTOCOL-NOTES 45): it was a broken hook, not evidence about the game.
     * Now: copy_n must cover the whole thunk and end on an instruction
     * boundary in the target prologue. */
    if (copy_n < 14) {
        probe_log("%s: copy_n=%u < 14 (thunk needs 14 bytes) - SKIPPED, hook would be corrupt",
                  tag, copy_n);
        return;
    }
    if (!safe_readable(target, copy_n)) {
        probe_log("%s: target %p not readable - skipped", tag, target);
        return;
    }
    if (memcmp(target, expect, copy_n) != 0) {
        probe_log("%s: prologue mismatch at %p (rva 0x%lx) - skipped", tag, target, rva);
        return;
    }
    *tramp_out = (unsigned char *)VirtualAlloc(NULL, 64, MEM_COMMIT | MEM_RESERVE,
                                               PAGE_EXECUTE_READWRITE);
    if (!*tramp_out) { probe_log("%s: VirtualAlloc failed", tag); return; }
    memcpy(*tramp_out, target, copy_n);
    t = *tramp_out + copy_n;
    *t++ = 0xff; *t++ = 0x25;
    *(unsigned int *)t = 0; t += 4;
    *(unsigned long long *)t = (unsigned long long)(uintptr_t)(target + copy_n);

    if (!VirtualProtect(target, copy_n, PAGE_EXECUTE_READWRITE, &old)) {
        probe_log("%s: VirtualProtect failed (%lu)", tag, GetLastError());
        return;
    }
    memset(patch, 0x90, sizeof(patch));
    patch[0] = 0xff; patch[1] = 0x25;
    *(unsigned int *)(patch + 2) = 0;
    *(unsigned long long *)(patch + 6) = (unsigned long long)(uintptr_t)hook_fn;
    memcpy(target, patch, copy_n);
    VirtualProtect(target, copy_n, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, copy_n);
    probe_log("%s: installed hook at %p (rva 0x%lx, %u bytes)", tag, target, rva, copy_n);
}

static int engine_snapshot(void *client, char *line, int cap)
{
    unsigned long long engine, facade, jobs, ticketlen, state;
    (void)cap;
    if (!client || !safe_readable(client, 0x80)) return 0;
    engine = *(unsigned long long *)((char *)client + 8);
    if (!engine || !safe_readable((const void *)(uintptr_t)engine, 0x1a0)) return 0;
    facade    = *(unsigned long long *)(engine + 0x118);
    jobs      = *(unsigned long long *)(engine + 0x120);
    ticketlen = *(unsigned long long *)(engine + 0x160);
    state     = *(unsigned int *)((char *)client + 0x74);
    wsprintfA(line, "engine=%p facade=%p jobs=%p ticketlen=%u state=%u",
              (void *)(uintptr_t)engine, (void *)(uintptr_t)facade,
              (void *)(uintptr_t)jobs, (unsigned)ticketlen, (unsigned)state);
    return 1;
}

__attribute__((noinline))
/* forward declarations (defined further down) */
static int g_rvstore_logged;
static int g_connect_wait_store;

static unsigned char *__cdecl connect_hook(void *client, unsigned long long a2,
                                           unsigned long long a3, unsigned long long a4,
                                           unsigned long long a5, unsigned long long a6)
{
    char line[256], snap[192];
    if (engine_snapshot(client, snap, sizeof(snap)))
        wsprintfA(line, "conquest connect: client=%p %s", client, snap);
    else
        wsprintfA(line, "conquest connect: client=%p (engine unreadable)", client);
    sdk_emit(client, __builtin_return_address(0), line);
    /* v23/v24: the STARTUP Connect (from FUN_1401363f0 at CGameApp init,
     * 0.9 ms after the SessionManagerStore ctor) submits the SDK job
     * "createSession" BEFORE the login chain; its context dangles and the
     * job explodes at Anno5+0xe7d03d (lock inc [rdx+8] on the freed
     * SessionManagerStore arg2 object) when it is finally processed after
     * the space-params apply.
     * BUT the game ALSO retries Connect via the manager path every 30 s
     * (FUN_140dd1b90, observed 33 s / 63 s / ... after load) - THOSE run
     * against the fully-initialized session manager and are safe, and
     * their completion triggers the OnlineConfiguration fetch. So: suppress
     * only the startup window (default 15 s), allow everything later.
     * UPC_CONQUEST_CONNECT=1 allows ALL (reproduces the startup crash). */
    /* v65: UPC_CONQUEST_WAIT_STORE=1 replaces the blind timer with a *state*
     * gate: the startup Connect is suppressed only until the SDK store has
     * been marked live by the rvstore hook (i.e. the login chain has run).
     * Rationale (PROTOCOL-NOTES 41): the notification websocket - and with it
     * the SDK connection - is only created when the conquest connect goes
     * through, but letting it through before the store exists dumps at
     * 0xe7d03d. Gating on the store instead of on a timer gets both: the
     * connect happens, and it happens after the store is alive. */
    if (!g_conquest_connect_allowed && g_connect_wait_store) {
        if (!g_rvstore_logged) {
            probe_log("conquest connect: SUPPRESSED waiting for the SDK store "
                      "(age %lu ms)", GetTickCount() - g_load_tick);
            return (unsigned char *)(uintptr_t)0;
        }
        probe_log("conquest connect: ALLOWED (store is live, age %lu ms)",
                  GetTickCount() - g_load_tick);
        return ((connect_fn *)g_connect_tramp)(client, a2, a3, a4, a5, a6);
    }
    if (!g_conquest_connect_allowed) {
        unsigned long age = GetTickCount() - g_load_tick;
        if (age < g_conquest_startup_ms) {
            probe_log("conquest connect: SUPPRESSED startup-window (age %lu ms; "
                      "job would dangle -> 0xe7d03d crash)", age);
            return (unsigned char *)(uintptr_t)0; /* FALSE = connect failed */
        }
        probe_log("conquest connect: ALLOWED (age %lu ms > window)", age);
    }
    return ((connect_fn *)g_connect_tramp)(client, a2, a3, a4, a5, a6);
}

__attribute__((noinline))
static void __cdecl conqdone_hook(void *self, void *res)
{
    unsigned long long q0 = 0, q1 = 0;
    char line[224];
    int err = 0;
    unsigned status = 0;
    if (res && safe_readable(res, 16)) {
        q0 = *(unsigned long long *)res;
        q1 = *(unsigned long long *)((char *)res + 8);
    }
    wsprintfA(line, "conquest createSession completion: this=%p res=%08x%08x %08x%08x",
              self, (unsigned)(q0 >> 32), (unsigned)q0,
              (unsigned)(q1 >> 32), (unsigned)q1);
    sdk_emit(self, __builtin_return_address(0), line);
    ((conqdone_fn)g_conqdone_tramp)(self, res);
    /* AFTER the handler: it writes error code at this+0x70 (int) and
     * status at this+0x74 (2 = success path taken, 1 = failure path).
     * Success ALSO submits the OnlineConfiguration fetch
     * (FUN_140c1d410); failure does not - the cfgfetch probe
     * disambiguates. */
    if (self && safe_readable(self, 0x80)) {
        err    = *(int *)((char *)self + 0x70);
        status = *(unsigned int *)((char *)self + 0x74);
    }
    wsprintfA(line, "conquest createSession outcome: err=%d status=%u (%s)",
              err, status, status == 2 ? "success path" : "failure path");
    sdk_emit(self, __builtin_return_address(0), line);
}

__attribute__((noinline))
static unsigned char *__cdecl mgrconn_hook(void *mgr, void *cb)
{
    char line[128];
    wsprintfA(line, "mgr connect: mgr=%p cb=%p", mgr, cb);
    sdk_emit(mgr, __builtin_return_address(0), line);
    return ((mgrconn_fn *)g_mgrconn_tramp)(mgr, cb);
}

/* OnlineConfiguration fetch entry (FUN_140c1d410, rva 0xc1d410) - only
 * reached via the completion's SUCCESS path; its firing (or not) proves
 * which path the createSession completion took. */
#define CFGFETCH_RVA   0xc1d410u
#define CFGFETCH_COPY  19u   /* pushes + lea rbp + sub rsp,0xC0 - complete */

typedef void (__cdecl *cfgfetch_fn)(void *facade);

static unsigned char *g_cfgfetch_tramp = NULL;

__attribute__((noinline))
static void __cdecl cfgfetch_hook(void *facade)
{
    char line[128];
    wsprintfA(line, "conquest config fetch ENTERED facade=%p (completion succeeded)", facade);
    sdk_emit(facade, __builtin_return_address(0), line);
    ((cfgfetch_fn)g_cfgfetch_tramp)(facade);
}

/* Session-state gate probe (v29): FUN_140edfe70(guard) is the second
 * condition of the GetConfiguration gate FUN_140f51c10 ("Session to
 * UbiServices is not created" / event 0x142 when it fails). It reads
 * guard+0x48 -> facade+0x18 (session state) and facade+0x30 -> obj+0x20
 * (connection object): +0x58/+0x59 flag bytes, +0x20 sub-object,
 * +0x50 keepalive/expiry. Log ALL of it + the return value. */
#define SESSSTATE_RVA   0xedfe70u
#define SESSSTATE_COPY  18u   /* push rbx; sub rsp,0x20; 3 movs - complete */

typedef unsigned long long (__cdecl *sessstate_fn)(void *guard);

static unsigned char *g_sessstate_tramp = NULL;

__attribute__((noinline))
static unsigned long long __cdecl sessstate_hook(void *guard)
{
    unsigned long long rv;
    char line[256];
    int state = -1, f58 = -1, f59 = -1, has_sub = -1;
    unsigned long long keep = 0, facade = 0, conn = 0;
    rv = ((sessstate_fn)g_sessstate_tramp)(guard);
    if (guard && safe_readable(guard, 0x38)) {
        facade = *(unsigned long long *)((char *)guard + 0x30);
        if (facade && safe_readable((const void *)(uintptr_t)facade, 0x40)) {
            state = *(int *)(facade + 0x18);
            {
                unsigned long long obj = *(unsigned long long *)(facade + 0x20);
                if (obj && safe_readable((const void *)(uintptr_t)obj, 0x28)) {
                    conn = *(unsigned long long *)(obj + 0x20);
                    if (conn && safe_readable((const void *)(uintptr_t)conn, 0x60)) {
                        f58 = *(unsigned char *)(conn + 0x58);
                        f59 = *(unsigned char *)(conn + 0x59);
                        keep = *(unsigned long long *)(conn + 0x50);
                        has_sub = (*(unsigned long long *)(conn + 0x20) != 0);
                    }
                }
            }
        }
    }
    wsprintfA(line, "sessstate: state=%d f58=%d f59=%d sub=%d keep=%08x%08x rv=%08x%08x",
              state, f58, f59, has_sub, (unsigned)(keep >> 32), (unsigned)keep,
              (unsigned)(rv >> 32), (unsigned)rv);
    sdk_emit(guard, __builtin_return_address(0), line);
    return rv;
}

/* forward declarations for probes installed from conquest_probe_install */
static void exit_trace_install(void);
static void noquit_patch_install(void);

static void conquest_probe_install(void)
{
    char flag[8] = {0};
    char msflag[16] = {0};
    g_conquest_connect_allowed =
        GetEnvironmentVariableA("UPC_CONQUEST_CONNECT", flag, sizeof(flag)) > 0 && flag[0] == '1';
    if (GetEnvironmentVariableA("UPC_CONQUEST_SUPPRESS_MS", msflag, sizeof(msflag)) > 0)
        g_conquest_startup_ms = (unsigned long)strtoul(msflag, NULL, 10);
    {
        char ws[8] = {0};
        g_connect_wait_store =
            GetEnvironmentVariableA("UPC_CONQUEST_WAIT_STORE", ws, sizeof(ws)) > 0
            && ws[0] == '1';
    }
    g_load_tick = GetTickCount();
    static const unsigned char con_connect[CONNECT_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x10,   /* mov [rsp+0x10], rbx */
        0x4c, 0x89, 0x4c, 0x24, 0x20,   /* mov [rsp+0x20], r9  */
        0x4c, 0x89, 0x44, 0x24, 0x18,   /* mov [rsp+0x18], r8  */
        0x55                             /* push rbp */
    };
    static const unsigned char con_done[CONQDONE_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x08,   /* mov [rsp+0x8], rbx  */
        0x55, 0x56, 0x57,               /* push rbp, rsi, rdi  */
        0x41, 0x54,                     /* push r12            */
        0x41, 0x55,                     /* push r13            */
        0x41, 0x56,                     /* push r14            */
        0x41, 0x57                      /* push r15            */
    };
    static const unsigned char con_mgr[MGRCONN_COPY] = {
        0x48, 0x89, 0x54, 0x24, 0x10,   /* mov [rsp+0x10], rdx */
        0x55, 0x53, 0x56, 0x57,         /* push rbp, rbx, rsi, rdi */
        0x41, 0x56,                     /* push r14            */
        0x48, 0x8d, 0xac, 0x24,         /* lea rbp, [rsp-0x100] (complete) */
        0x00, 0xff, 0xff, 0xff
    };
    static const unsigned char con_cfgf[CFGFETCH_COPY] = {
        0x40, 0x55,                     /* push rbp            */
        0x53,                           /* push rbx            */
        0x56,                           /* push rsi            */
        0x57,                           /* push rdi            */
        0x41, 0x56,                     /* push r14            */
        0x48, 0x8d, 0x6c, 0x24, 0xc9,   /* lea rbp, [rsp-0x37] */
        0x48, 0x81, 0xec, 0xc0, 0x00, 0x00, 0x00 /* sub rsp, 0xC0 */
    };
    static const unsigned char con_sesss[SESSSTATE_COPY] = {
        0x40, 0x53,                     /* push rbx            */
        0x48, 0x83, 0xec, 0x20,         /* sub rsp, 0x20       */
        0x48, 0x8b, 0x41, 0x30,         /* mov rax, [rcx+0x30] */
        0x48, 0x8b, 0x50, 0x20,         /* mov rdx, [rax+0x20] */
        0x48, 0x8b, 0x42, 0x20          /* mov rax, [rdx+0x20] */
    };
    hook_install("connect probe", CONNECT_RVA, con_connect, CONNECT_COPY,
                 &g_connect_tramp, (void *)connect_hook);
    hook_install("conqdone probe", CONQDONE_RVA, con_done, CONQDONE_COPY,
                 &g_conqdone_tramp, (void *)conqdone_hook);
    hook_install("mgrconn probe", MGRCONN_RVA, con_mgr, MGRCONN_COPY,
                 &g_mgrconn_tramp, (void *)mgrconn_hook);
    hook_install("cfgfetch probe", CFGFETCH_RVA, con_cfgf, CFGFETCH_COPY,
                 &g_cfgfetch_tramp, (void *)cfgfetch_hook);
    hook_install("sessstate probe", SESSSTATE_RVA, con_sesss, SESSSTATE_COPY,
                 &g_sessstate_tramp, (void *)sessstate_hook);
    {
        char dis[8] = {0};
        g_connstore = !(GetEnvironmentVariableA("UPC_CONNSTORE", dis, sizeof(dis)) > 0 &&
                        dis[0] == '0');
        if (g_connstore) {
            cs_check_install();
            cs_pump_install();
        }
        {
            char dis[8] = {0};
            g_pump170 = !(GetEnvironmentVariableA("UPC_PUMP170", dis, sizeof(dis)) > 0 &&
                          dis[0] == '0');
            if (g_pump170) pump170_install();
        }
        {
            char dis[8] = {0};
            if (!(GetEnvironmentVariableA("UPC_CONNGET", dis, sizeof(dis)) > 0 &&
                  dis[0] == '0'))
                connget_install();
        }
        {
            char dis[8] = {0};
            if (!(GetEnvironmentVariableA("UPC_NTYPE", dis, sizeof(dis)) > 0 && dis[0] == '0'))
                nt_install();
        }
        {
            char dis[8] = {0};
            if (!(GetEnvironmentVariableA("UPC_CQSRV", dis, sizeof(dis)) > 0 && dis[0] == '0'))
                cqsrv_install();
        {
            char dis[8] = {0};
            if (!(GetEnvironmentVariableA("UPC_CQJOB", dis, sizeof(dis)) > 0 && dis[0] == '0'))
                cqjob_install();
        {
            char dis[8] = {0};
            if (!(GetEnvironmentVariableA("UPC_RVERR", dis, sizeof(dis)) > 0 && dis[0] == '0'))
                rverr_install();
        {
            char dis[8] = {0};
            if (!(GetEnvironmentVariableA("UPC_RVSTATE", dis, sizeof(dis)) > 0 && dis[0] == '0'))
                rvstate_install();
        {
            char dis[8] = {0};
            /* opt-in: the 14:10 run with this probe installed exited early
             * (clean, no dump) before the RV error - treat as unsafe until
             * proven otherwise. UPC_RVFAIL=1 enables. */
            if (GetEnvironmentVariableA("UPC_RVFAIL", dis, sizeof(dis)) > 0 && dis[0] == '1')
                rvfail_install();
        {
            char dis[8] = {0};
            if (!(GetEnvironmentVariableA("UPC_RVSIG", dis, sizeof(dis)) > 0 && dis[0] == '0'))
                rvsig_install();
        {
            char dis[8] = {0};
            if (!(GetEnvironmentVariableA("UPC_LOGERR", dis, sizeof(dis)) > 0 && dis[0] == '0'))
                logerr_install();
        {
            char dis[8] = {0};
            if (!(GetEnvironmentVariableA("UPC_RVSET", dis, sizeof(dis)) > 0 && dis[0] == '0'))
                rvset_install();
        {
            char dis[8] = {0};
            if (!(GetEnvironmentVariableA("UPC_RVFMT", dis, sizeof(dis)) > 0 && dis[0] == '0'))
                rvfmt_install();
        }
        }
        }
        }
        }
        }
        {
            /* v49: default OFF - the SSO string scan can walk past the
             * checked window and took the game down once (12:04 run).
             * Enable only for a diagnostic run: UPC_RVEB=1. */
            char on[8] = {0};
            if (GetEnvironmentVariableA("UPC_RVEB", on, sizeof(on)) > 0 && on[0] == '1')
                rveb_install();
        }
        }
        }
        }
    }
    exit_trace_install();
    probe_log("conquest probes: startup-window suppression %s, window=%lu ms",
              g_conquest_connect_allowed ? "OFF (UPC_CONQUEST_CONNECT=1)" : "ON (default)",
              g_conquest_startup_ms);
}

/* ---------------- force-online patch (v51, DIAGNOSTIC) ----------------
 * FUN_14031a170 decides whether the game runs in OFFLINE MODE:
 *   if (DAT_141ba1418 || GetCommandLine has "-offline" ||
 *       *(char*)(DAT_141ba1510 + 0x818))   -> *(int*)(this+0x194) = 1; return;
 * The third test is the one that actually fires in our runs (the runtime
 * flag set by the failed online login).  NOPing the three conditional jumps
 * makes the function fall through into the ONLINE init path instead, which is
 * what we want to watch: it shows exactly which online calls the game makes
 * once it believes it is online.  Diagnostic only - NOT default.
 *   UPC_FORCEONLINE=1 enables.
 */
#define ONLINE_CHECK_RVA 0x31a37fu

static void force_online_patch(void)
{
    char on[8] = {0};
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *p;
    DWORD old = 0;
    static const unsigned char expect[13] = {
        0x0f, 0x85, 0x91, 0x00, 0x00, 0x00,  /* jne +0x91 (config flag)   */
        0xe8, 0xd6, 0xc3, 0xe1, 0xff,        /* call FUN_140136760        */
        0x84, 0xc0                           /* test al,al               */
    };
    if (!(GetEnvironmentVariableA("UPC_FORCEONLINE", on, sizeof(on)) > 0 && on[0] == '1'))
        return;
    if (!base) return;
    p = base + ONLINE_CHECK_RVA;
    if (!safe_readable(p, 0x1c)) { probe_log("forceonline: target unreadable"); return; }
    if (memcmp(p, expect, sizeof(expect)) != 0) {
        probe_log("forceonline: prologue mismatch at %p - skipped", p);
        return;
    }
    if (!VirtualProtect(p, 0x1c, PAGE_EXECUTE_READWRITE, &old)) {
        probe_log("forceonline: VirtualProtect failed (%lu)", GetLastError());
        return;
    }
    memset(p + 0, 0x90, 6);          /* jne  #1 (DAT_141ba1418)        */
    p[11] = 0x90; p[12] = 0x90;      /* test al,al                     */
    memset(p + 13, 0x90, 6);         /* jne  #2 ("-offline" cmdline)   */
    p[26] = 0x90; p[27] = 0x90;      /* jne  #3 ([app+0x818])          */
    VirtualProtect(p, 0x1c, old, &old);
    FlushInstructionCache(GetCurrentProcess(), p, 0x1c);
    probe_log("forceonline: patched 3 offline-mode jumps at %p (ONLINE path forced)", p);
}

/* ---------------- quit-site tracer (v26) ----------------
 * The game exits CLEANLY ~0.8 s after the conquest chain's space-params
 * response ("stub unloaded", no dump, createSession completion never
 * runs). IAT-hook exit/_exit (api-ms-win-crt-runtime) + TerminateProcess
 * (kernel32) in Anno5.exe and log the CALLER's return address translated
 * to an Anno5 RVA - that pinpoints the quit site. One shot per process,
 * zero timing impact. IAT slots computed offline with pefile:
 * exit=0x15834e8 _exit=0x1583530 TerminateProcess=0x15826e8. */

#define IAT_EXIT     0x15834e8u
#define IAT__EXIT    0x1583530u
#define IAT_TERM     0x15826e8u

typedef void (__cdecl *exit_fn)(unsigned code);
typedef int (WINAPI *terminate_fn)(void *handle, unsigned code);

static void *g_exit_orig = NULL;
static void *g__exit_orig = NULL;
static void *g_term_orig = NULL;
static unsigned char *g_anno_base = NULL;

/* v70: format a captured frame list as "0x<rva>" when the frame is inside
 * Anno5.exe, "mod+0x<off>" otherwise. Written with snprintf because wsprintfA
 * has no %llx (that produced the useless "+llx(0x...)"/" lx lx" output). */
/* v119: resolve a code address to "mod+0xoff" for ANY module, not just Anno5.
 * Without this a crash trace shows bare 7fff.. numbers for our own DLL frames,
 * which is exactly where the interesting frames are (hook wrappers and the
 * stub's websocket layer). */
static void bt_sym(unsigned long long a, char *out, int cap)
{
    HMODULE mod = NULL;
    char path[MAX_PATH];
    const char *name;
    if (cap <= 0) return;
    out[0] = 0;
    if (!a) { snprintf(out, cap, " 0"); return; }
    if (!GetModuleHandleExA(0x00000004 /*FROM_ADDRESS*/ | 0x00000002 /*UNCHANGED_REFCOUNT*/,
                            (LPCSTR)(uintptr_t)a, &mod) || !mod) {
        snprintf(out, cap, " %llx", a);
        return;
    }
    if (GetModuleFileNameA(mod, path, sizeof(path)) > 0) {
        name = strrchr(path, '\\');
        name = name ? name + 1 : path;
    } else {
        name = "?";
    }
    snprintf(out, cap, " %s+%llx", name, a - (unsigned long long)(uintptr_t)mod);
}

static void bt_format(void **frames, unsigned nf, char *out, int cap)
{
    int o = 0;
    unsigned i;
    if (cap > 0) out[0] = 0;
    for (i = 0; i < nf && o < cap - 40; i++) {
        char one[96];
        bt_sym((unsigned long long)(uintptr_t)frames[i], one, sizeof(one));
        o += snprintf(out + o, cap - o, "%s", one);
    }
}

static unsigned bt_capture(void **frames, unsigned max, unsigned skip)
{
    typedef unsigned short (WINAPI *bt_fn)(unsigned long, unsigned long, void **, unsigned long *);
    static bt_fn fn = NULL;
    static int tried = 0;
    if (!tried) {
        tried = 1;
        fn = (bt_fn)(uintptr_t)GetProcAddress(GetModuleHandleA("ntdll.dll"),
                                              "RtlCaptureStackBackTrace");
    }
    if (!fn) return 0;
    return (unsigned)fn(skip, max, frames, NULL);
}

static void quit_log(const char *what, unsigned code, void *retaddr)
{
    unsigned long long ra = (unsigned long long)(uintptr_t)retaddr;
    char line[192];
    char line2[600];
    unsigned long long rva = (g_anno_base && ra >= (unsigned long long)(uintptr_t)g_anno_base
                              && ra < (unsigned long long)(uintptr_t)g_anno_base + 0x4000000)
        ? ra - (unsigned long long)(uintptr_t)g_anno_base : 0;
    if (rva)
        wsprintfA(line, "QUIT SITE: %s(code=%u) from Anno5+0x%x", what, code, (unsigned)rva);
    else
        wsprintfA(line, "QUIT SITE: %s(code=%u) from non-Anno module %p", what, code, retaddr);
    sdk_emit(retaddr, __builtin_return_address(0), line);
    {
        void *frames[24];
        unsigned nf = bt_capture(frames, 24, 2);
        if (nf) {
            bt_format(frames, nf, line2, sizeof(line2));
            snprintf(line, sizeof(line), "QUIT STACK:%s", line2);
            sdk_emit(retaddr, __builtin_return_address(0), line);
        }
    }
}

/* ---------------- first-chance exception trace (v71) ----------------
 * Live runs die at Anno5+0x1352da1: a virtual call inside the RV login step
 * (vtable 0x16472d0 = MethodCallJob<CallContextRegister>) is wrapped in a
 * catch-all that calls FUN_141364cc0 -> _exit(1). So the login does not
 * "fail politely" - something THROWS in the first step and the process is
 * killed. This logs the throw so we can see what it is.
 * UPC_VEXC=0 disables; 40 events max. */
static LONG CALLBACK vec_handler(PEXCEPTION_POINTERS info)
{
    static volatile LONG n = 0;
    LONG c;
    if (!info || !info->ExceptionRecord) return EXCEPTION_CONTINUE_SEARCH;
    c = InterlockedIncrement(&n);
    if (c > 40) return EXCEPTION_CONTINUE_SEARCH;
    {
        EXCEPTION_RECORD *er = info->ExceptionRecord;
        unsigned char *b = (unsigned char *)GetModuleHandleA(NULL);
        unsigned long long a = (unsigned long long)(uintptr_t)er->ExceptionAddress;
        unsigned long long rip = (info->ContextRecord)
            ? (unsigned long long)info->ContextRecord->Rip : 0;
        unsigned long long argu0 = (er->NumberParameters > 0)
            ? (unsigned long long)er->ExceptionInformation[0] : 0;
        unsigned long long argu1 = (er->NumberParameters > 1)
            ? (unsigned long long)er->ExceptionInformation[1] : 0;
        char st[900];
        void *frames[24];
        unsigned nf = bt_capture(frames, 24, 0);
        bt_format(frames, nf, st, sizeof(st));
        probe_log("VEXC[%ld] code=0x%08x addr=A+%llx rip=A+%llx p0=0x%llx p1=0x%llx thread=%lu stack:%s",
                  c, (unsigned)er->ExceptionCode,
                  a - (unsigned long long)(uintptr_t)b,
                  rip - (unsigned long long)(uintptr_t)b,
                  argu0, argu1, (unsigned long)GetCurrentThreadId(), st);
        /* v72: for a fault we also need the register file and a raw stack scan -
         * the frame chain is useless once RIP is garbage (a call through a bad
         * pointer). rsp-scan picks the real return addresses back out. */
        if (info->ContextRecord && (unsigned)er->ExceptionCode == 0xc0000005) {
            CONTEXT *ctx = info->ContextRecord;
            unsigned long long *sp = (unsigned long long *)(uintptr_t)ctx->Rsp;
            char reg[420], sc[900];
            int o = 0, so = 0, i;
            o += snprintf(reg + o, sizeof(reg) - o,
                          "rax=%llx rbx=%llx rcx=%llx rdx=%llx rsi=%llx rdi=%llx "
                          "r8=%llx r9=%llx rsp=%llx rbp=%llx",
                          (unsigned long long)ctx->Rax, (unsigned long long)ctx->Rbx,
                          (unsigned long long)ctx->Rcx, (unsigned long long)ctx->Rdx,
                          (unsigned long long)ctx->Rsi, (unsigned long long)ctx->Rdi,
                          (unsigned long long)ctx->R8, (unsigned long long)ctx->R9,
                          (unsigned long long)ctx->Rsp, (unsigned long long)ctx->Rbp);
            probe_log("VEXC regs: %s", reg);
            if (sp && safe_readable(sp, 8 * 64)) {
                for (i = 0; i < 96 && so < (int)sizeof(sc) - 48; i++) {
                    unsigned long long v = sp[i];
                    unsigned long long rv = v - (unsigned long long)(uintptr_t)b;
                    if (rv < 0x4000000ull && (v & 0xfff) != 0) {
                        so += snprintf(sc + so, sizeof(sc) - so, " A+%llx", rv);
                    } else if ((v & 0xffff000000000000ull) == 0x0000000000000000ull
                               && (v & 0xfff) != 0 && v > 0x10000) {
                        char one[80];
                        bt_sym(v, one, sizeof(one));
                        if (one[1] && one[1] != 'x')
                            so += snprintf(sc + so, sizeof(sc) - so, "%s", one);
                    }
                }
                probe_log("VEXC stack-scan:%s", sc);
            }
        }
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

static void veh_install(void)
{
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_VEXC", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("vexca: off (UPC_VEXC=0)");
        return;
    }
    if (AddVectoredExceptionHandler(1, vec_handler))
        probe_log("vexca: first-chance exception logger installed");
    else
        probe_log("vexca: AddVectoredExceptionHandler FAILED (%lu)", GetLastError());
}

__attribute__((noinline))
static void __cdecl exit_hook(unsigned code)
{
    quit_log("ucrt exit", code, __builtin_return_address(0));
    ((exit_fn)g_exit_orig)(code);
}

__attribute__((noinline))
static void __cdecl _exit_hook(unsigned code)
{
    quit_log("ucrt _exit", code, __builtin_return_address(0));
    ((exit_fn)g__exit_orig)(code);
}

__attribute__((noinline))
static int WINAPI terminateproc_hook(void *handle, unsigned code)
{
    quit_log("TerminateProcess", code, __builtin_return_address(0));
    return ((terminate_fn)g_term_orig)(handle, code);
}

static void iat_hook(const char *tag, unsigned long slot_rva, void *hook_fn, void **orig_out)
{
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *slot = base + slot_rva;
    DWORD old = 0;
    unsigned long long cur;
    if (!safe_readable(slot, 8)) {
        probe_log("%s: IAT slot %p unreadable - skipped", tag, (void *)slot);
        return;
    }
    cur = *(unsigned long long *)slot;
    if (!cur) {
        probe_log("%s: IAT slot %p unresolved - skipped", tag, (void *)slot);
        return;
    }
    *orig_out = (void *)(uintptr_t)cur;
    if (!VirtualProtect(slot, 8, PAGE_READWRITE, &old)) {
        probe_log("%s: VirtualProtect failed (%lu)", tag, GetLastError());
        return;
    }
    *(unsigned long long *)slot = (unsigned long long)(uintptr_t)hook_fn;
    VirtualProtect(slot, 8, old, &old);
    probe_log("%s: IAT hooked at %p (orig %p)", tag, (void *)slot, (void *)(uintptr_t)cur);
}

static void exit_trace_install(void)
{
    g_anno_base = (unsigned char *)GetModuleHandleA(NULL);
    iat_hook("exit trace", IAT_EXIT, (void *)exit_hook, &g_exit_orig);
    iat_hook("exit trace", IAT__EXIT, (void *)_exit_hook, &g__exit_orig);
    iat_hook("exit trace", IAT_TERM, (void *)terminateproc_hook, &g_term_orig);
    noquit_patch_install();
}

/* ---------------- network probe (v52) ----------------
 * The RendezVous layer fails right after we hand it the transport URL, and
 * the game then reports "connection lost". To see WHAT it actually tries we
 * hook the exe's ws2_32 imports: getaddrinfo (what host is resolved), connect
 * and sendto/recvfrom (the PRUDP datagrams once it dials). Import slots
 * computed offline with pefile.
 *   getaddrinfo 0x15830f8   connect   0x1583198   sendto   0x1583108
 *   recvfrom    0x1583110   WSASendTo 0x1583238   WSARecvFrom 0x1583230
 */
#define IAT_GETADDRINFO 0x15830f8u
#define IAT_SENDTO      0x1583108u
#define IAT_RECVFROM    0x1583110u
#define IAT_RECV        0x15831a8u
#define IAT_WSAGETOV    0x1583218u
#define IAT_CLOSESOCKET 0x1583150u
#define IAT_CONNECT     0x1583198u
#define IAT_WSASENDTO   0x1583238u
#define IAT_WSARECVFROM 0x1583230u

typedef struct { unsigned long len; char *buf; } my_wsabuf;

static int   (__cdecl *g_gai_orig)(const char *, const char *, const void *, void **);
static int   (__cdecl *g_conn_orig)(unsigned long long, const void *, int);
static int   (__cdecl *g_sendto_orig)(unsigned long long, const char *, int, int, const void *, int);
static int   (__cdecl *g_recvfrom_orig)(unsigned long long, char *, int, int, void *, int *);
static int   (__cdecl *g_recv_orig)(unsigned long long, char *, int, int);
static int   (__cdecl *g_wsagetov_orig)(unsigned long long, void *, unsigned long *, int, unsigned long *);
static int   (__cdecl *g_closesock_orig)(unsigned long long);
static int   (__cdecl *g_wsasendto_orig)(unsigned long long, my_wsabuf *, unsigned long,
                                         unsigned long *, unsigned long, const void *, int, void *, void *);
static int   (__cdecl *g_wsarecvfrom_orig)(unsigned long long, my_wsabuf *, unsigned long,
                                           unsigned long *, unsigned long *, void *, int *, void *, void *);
static int g_net_n = 0;
static int g_net_dgram_n = 0;

static void fmt_hex(const unsigned char *b, int n, char *out, int cap)
{
    static const char *hx = "0123456789abcdef";
    int i, o = 0;
    if (!b || n <= 0 || cap < 3) { out[0] = 0; return; }
    if (n > 24) n = 24;
    for (i = 0; i < n && o + 2 < cap; i++) {
        out[o++] = hx[b[i] >> 4];
        out[o++] = hx[b[i] & 15];
    }
    out[o] = 0;
}

static void fmt_sa(const void *sa, char *out, int cap)
{
    const unsigned char *p = (const unsigned char *)sa;
    if (!sa) { snprintf(out, cap, "(null)"); return; }
    if (*(const short *)p == 2) {
        unsigned port = ((unsigned)p[2] << 8) | p[3];
        snprintf(out, cap, "%u.%u.%u.%u:%u", p[4], p[5], p[6], p[7], port);
    } else {
        snprintf(out, cap, "af=%d", (int)*(const short *)p);
    }
}

static int __cdecl gai_hook(const char *node, const char *service, const void *hints, void **res)
{
    int rc = g_gai_orig(node, service, hints, res);
    if (g_net_n < 60) {
        g_net_n++;
        probe_log("dns: node=\"%.64s\" service=\"%.16s\" rc=%d",
                  node ? node : "", service ? service : "", rc);
    }
    return rc;
}

static int __cdecl conn_hook(unsigned long long s, const void *name, int namelen)
{
    char sa[48];
    int rc;
    fmt_sa(name, sa, sizeof(sa));
    if (g_net_n < 60) { g_net_n++; probe_log("tcp connect -> %s", sa); }
    rc = g_conn_orig(s, name, namelen);
    if (g_net_n < 60) { g_net_n++; probe_log("tcp connect -> %s rc=%d", sa, rc); }
    return rc;
}

static int __cdecl sendto_hook(unsigned long long s, const char *buf, int len, int flags,
                               const void *to, int tolen)
{
    char sa[48], hx[64];
    fmt_sa(to, sa, sizeof(sa));
    fmt_hex((const unsigned char *)buf, len, hx, sizeof(hx));
    if (g_net_dgram_n < 60) {
        g_net_dgram_n++;
        probe_log("udp send %dB -> %s hex=%s", len, sa, hx);
    }
    return g_sendto_orig(s, buf, len, flags, to, tolen);
}

static int __cdecl recvfrom_hook(unsigned long long s, char *buf, int len, int flags,
                                 void *from, int *fromlen)
{
    int rc = g_recvfrom_orig(s, buf, len, flags, from, fromlen);
    char sa[48], hx[64];
    fmt_sa(from, sa, sizeof(sa));
    fmt_hex((const unsigned char *)buf, rc, hx, sizeof(hx));
    if (g_net_dgram_n < 60) {
        g_net_dgram_n++;
        probe_log("udp recv %dB <- %s hex=%s", rc, sa, hx);
    }
    return rc;
}

static int __cdecl wsasendto_hook(unsigned long long s, my_wsabuf *bufs, unsigned long count,
                                  unsigned long *sent, unsigned long flags, const void *to,
                                  int tolen, void *ov, void *comp)
{
    char sa[48], hx[64];
    fmt_sa(to, sa, sizeof(sa));
    fmt_hex((const unsigned char *)(bufs ? bufs[0].buf : 0), (int)(bufs ? bufs[0].len : 0), hx, sizeof(hx));
    if (g_net_dgram_n < 60 && bufs && count) {
        g_net_dgram_n++;
        probe_log("wsasendto %luB -> %s hex=%s", bufs[0].len, sa, hx);
    }
    return g_wsasendto_orig(s, bufs, count, sent, flags, to, tolen, ov, comp);
}

static int __cdecl wsarecvfrom_hook(unsigned long long s, my_wsabuf *bufs, unsigned long count,
                                    unsigned long *recvd, unsigned long *flags, void *from,
                                    int *fromlen, void *ov, void *comp)
{
    int rc = g_wsarecvfrom_orig(s, bufs, count, recvd, flags, from, fromlen, ov, comp);
    char sa[48], hx[64];
    fmt_sa(from, sa, sizeof(sa));
    fmt_hex((const unsigned char *)(bufs ? bufs[0].buf : 0), recvd ? (int)*recvd : 0, hx, sizeof(hx));
    if (g_net_dgram_n < 60 && bufs && count) {
        g_net_dgram_n++;
        probe_log("wsarecvfrom %luB <- %s hex=%s", recvd ? *recvd : 0, sa, hx);
    }
    return rc;
}


/* ---------------- receive-only socket probe (v106, PN 118) ---------------
 * UPC_PRUDPSOCK=1 hooks ONLY recvfrom/WSARecvFrom (no send path), so the HTTP
 * login still works. It answers one question: do our PRUDP datagrams actually
 * arrive at the client process? The RV data-path hooks in the netprobe are
 * known to disturb libcurl, hence a separate switch. */
static int g_prudp_recv_n = 0;
static int g_prudp_bt_done = 0;
static int __cdecl whoclose_hook(unsigned long long sk);   /* PN 131 */

/* PN 119: the client's UDP socket is overlapped, so WSARecvFrom returns
 * WSA_IO_PENDING (997) and the bytes only appear later. Remember
 * (overlapped -> buffer) here and print the data from WSAGetOverlappedResult. */
static struct { void *ov; char *buf; int len; } g_ov[32];
static int g_ov_n = 0;
static int g_prudp_ov_log = 0;

static void prudp_ov_remember(void *ov, char *buf, int len)
{
    if (!ov || !buf) return;
    if (g_ov_n < 32) {
        g_ov[g_ov_n].ov = ov; g_ov[g_ov_n].buf = buf; g_ov[g_ov_n].len = len;
        g_ov_n++;
    } else {
        g_ov[(g_ov_n++) % 32].ov = ov;
        g_ov[((g_ov_n - 1) % 32)].buf = buf;
        g_ov[((g_ov_n - 1) % 32)].len = len;
    }
}

static BOOL __cdecl prudp_wsagetov_hook(unsigned long long sk, void *ov, unsigned long *transferred,
                                        int wait, unsigned long *flags)
{
    BOOL rc = g_wsagetov_orig(sk, ov, transferred, wait, flags);
    if (rc && transferred && *transferred > 0 && *transferred <= 40 && g_prudp_ov_log < 40) {
        int i;
        for (i = 0; i < (g_ov_n < 32 ? g_ov_n : 32); i++) {
            if (g_ov[i].ov == ov && g_ov[i].buf) {
                char hx[96];
                fmt_hex((const unsigned char *)g_ov[i].buf, (int)*transferred, hx, sizeof(hx));
                g_prudp_ov_log++;
                probe_log("PRUDPSOCK ovrecv %luB hex=%s", *transferred, hx);
                break;
            }
        }
    }
    return rc;
}

static int __cdecl prudp_recvonly_hook(unsigned long long sk, char *buf, int len, int flags)
{
    int rc = g_recv_orig(sk, buf, len, flags);
    /* only PRUDP-looking datagrams (src 0x31, dst 0x3f, tiny), so the HTTP/ws
     * traffic does not consume the log budget (PN 118) */
    if (rc > 0 && rc <= 40 && buf && (unsigned char)buf[0] == 0x31
        && (unsigned char)buf[1] == 0x3f && g_prudp_recv_n < 40) {
        char hx[96];
        g_prudp_recv_n++;
        fmt_hex((const unsigned char *)buf, rc, hx, sizeof(hx));
        probe_log("PRUDPSOCK recv() %dB sk=%llu hex=%s", rc, sk, hx);
    }
    return rc;
}

static int __cdecl prudp_recv_hook(unsigned long long sk, char *buf, int len, int flags,
                                   void *from, int *fromlen)
{
    int rc = g_recvfrom_orig(sk, buf, len, flags, from, fromlen);
    if (rc > 0 && g_prudp_recv_n < 40) {
        char sa[48], hx[64];
        g_prudp_recv_n++;
        fmt_sa(from, sa, sizeof(sa));
        fmt_hex((const unsigned char *)buf, rc, hx, sizeof(hx));
        probe_log("PRUDPSOCK recv %dB <- %s hex=%s", rc, sa, hx);
    }
    return rc;
}

static int __cdecl prudp_wsarecv_hook(unsigned long long sk, my_wsabuf *bufs, unsigned long count,
                                      unsigned long *recvd, unsigned long *flags, void *from,
                                      int *fromlen, void *ov, void *comp)
{
    int rc = g_wsarecvfrom_orig(sk, bufs, count, recvd, flags, from, fromlen, ov, comp);
    if (bufs && count > 0) prudp_ov_remember(ov, bufs[0].buf, (int)bufs[0].len);
    if (!g_prudp_bt_done) {   /* PN 122: find the receive loop / parser caller */
        void *frames[12];
        char st[300];
        unsigned nf;
        g_prudp_bt_done = 1;
        nf = bt_capture(frames, 12, 1);
        bt_format(frames, nf, st, sizeof(st));
        probe_log("PRUDPSOCK wsarecvfrom stack:%s", st);
    }
    if (rc == 0 && recvd && *recvd > 0 && g_prudp_recv_n < 40) {
        char sa[48], hx[64];
        g_prudp_recv_n++;
        fmt_sa(from, sa, sizeof(sa));
        fmt_hex((const unsigned char *)(bufs ? bufs[0].buf : 0), (int)*recvd, hx, sizeof(hx));
        probe_log("PRUDPSOCK wsarecv %luB <- %s hex=%s", *recvd, sa, hx);
    }
    return rc;
}

static void prudpsock_install(void)
{
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_PRUDPSOCK", dis, sizeof(dis)) <= 0 || dis[0] != '1') {
        probe_log("prudpsock probe: off (UPC_PRUDPSOCK=1 enables)");
        return;
    }
    iat_hook("prudpsock", IAT_RECVFROM, (void *)prudp_recv_hook, (void **)&g_recvfrom_orig);
    iat_hook("prudpsock", IAT_WSARECVFROM, (void *)prudp_wsarecv_hook, (void **)&g_wsarecvfrom_orig);
    iat_hook("prudpsock", IAT_RECV, (void *)prudp_recvonly_hook, (void **)&g_recv_orig);
    iat_hook("prudpsock", IAT_WSAGETOV, (void *)prudp_wsagetov_hook, (void **)&g_wsagetov_orig);
    iat_hook("whoclose", IAT_CLOSESOCKET, (void *)whoclose_hook, (void **)&g_closesock_orig);
}

static void net_probe_install(void)
{
    /* OPT-IN since the 12:56 run: with the probe installed the SDK's HTTP
     * requests stopped going out entirely (server saw bare CONN open/close,
     * 0 HTTP requests). One of these IAT hooks breaks libcurl's write path.
     * Not investigated further - enable only for a network-diagnostic run. */
    char dis[8] = {0};
    int mode = 0;
    if (GetEnvironmentVariableA("UPC_NETPROBE", dis, sizeof(dis)) > 0 && dis[0] != '0')
        mode = (dis[0] == '2') ? 2 : 1;
    if (!mode) return;
    /* mode 1 = "who does it dial" only (getaddrinfo + connect). The data-path
     * hooks (sendto/WSASendTo/...) are mode 2 because one of them breaks
     * libcurl's writes (see 12:56 run: HTTP stopped entirely). */
    iat_hook("netprobe", IAT_GETADDRINFO, (void *)gai_hook, (void **)&g_gai_orig);
    iat_hook("netprobe", IAT_CONNECT, (void *)conn_hook, (void **)&g_conn_orig);
    if (mode >= 2) {
        iat_hook("netprobe", IAT_SENDTO, (void *)sendto_hook, (void **)&g_sendto_orig);
        iat_hook("netprobe", IAT_RECVFROM, (void *)recvfrom_hook, (void **)&g_recvfrom_orig);
        iat_hook("netprobe", IAT_WSASENDTO, (void *)wsasendto_hook, (void **)&g_wsasendto_orig);
        iat_hook("netprobe", IAT_WSARECVFROM, (void *)wsarecvfrom_hook, (void **)&g_wsarecvfrom_orig);
    }
}

/* ---------------- winsock trace, runtime-resolved (v75) ----------------
 * The old netprobe (v52) used hand-computed IAT slot RVAs and is suspected of
 * hooking the WRONG import (it silenced libcurl's writes), which means the
 * "the client never dials anything" conclusion was never measured reliably.
 * This version resolves the ws2_32 exports at runtime and rewrites every IAT
 * slot in Anno5.exe whose current value equals one of them - no offsets to
 * guess, and the original pointer is taken from the slot itself.
 * Signals are read-only (call-through), so nothing can break.
 * UPC_WSTRACE=0 disables. */
static void *g_ws_orig[10];
static int   g_ws_n = 0;
static int   g_ws_events = 0;

static void ws_emit(const char *fmt, ...)
{
    char line[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    probe_log("%s", line);
}

static void ws_sa(const void *sa, char *out, int cap)
{
    const unsigned char *p = (const unsigned char *)sa;
    if (!sa) { snprintf(out, cap, "(null)"); return; }
    if (*(const unsigned short *)p == 2) {
        unsigned port = ((unsigned)p[2] << 8) | p[3];
        snprintf(out, cap, "%u.%u.%u.%u:%u", p[4], p[5], p[6], p[7], port);
    } else {
        snprintf(out, cap, "af=%d", (int)*(const unsigned short *)p);
    }
}

static void ws_hex(const void *buf, int len, char *out, int cap)
{
    static const char *hx = "0123456789abcdef";
    const unsigned char *b = (const unsigned char *)buf;
    int i, o = 0;
    if (!b || len <= 0) { out[0] = 0; return; }
    if (len > 32) len = 32;
    for (i = 0; i < len && o + 3 < cap; i++) {
        out[o++] = hx[b[i] >> 4];
        out[o++] = hx[b[i] & 15];
        out[o++] = ' ';
    }
    out[o] = 0;
}

typedef struct { unsigned long len; char *buf; } wsabuf_t;

static int __cdecl ws_connect_hook(unsigned long long s, const void *name, int namelen)
{
    char sa[64];
    int rc;
    ws_sa(name, sa, sizeof(sa));
    if (g_ws_events++ < 200) ws_emit("WSTRACE connect sock=%llx -> %s", s, sa);
    rc = ((int (__cdecl *)(unsigned long long, const void *, int))g_ws_orig[0])(s, name, namelen);
    if (g_ws_events < 200) ws_emit("WSTRACE connect -> %s rc=%d", sa, rc);
    return rc;
}

static int __cdecl ws_sendto_hook(unsigned long long s, const char *buf, int len, int flags,
                                  const void *to, int tolen)
{
    char sa[64], hx[140];
    ws_sa(to, sa, sizeof(sa));
    ws_hex(buf, len, hx, sizeof(hx));
    if (g_ws_events++ < 200)
        ws_emit("WSTRACE sendto sock=%llx %dB -> %s [%s]", s, len, sa, hx);
    return ((int (__cdecl *)(unsigned long long, const char *, int, int, const void *, int))
            g_ws_orig[1])(s, buf, len, flags, to, tolen);
}

static int __cdecl ws_wsasendto_hook(unsigned long long s, wsabuf_t *b, unsigned long cnt,
                                     unsigned long *sent, unsigned long flags, const void *to,
                                     int tolen, void *ov, void *comp)
{
    char sa[64], hx[140];
    ws_sa(to, sa, sizeof(sa));
    if (b && cnt) ws_hex(b[0].buf, (int)b[0].len, hx, sizeof(hx)); else hx[0] = 0;
    if (g_ws_events++ < 200)
        ws_emit("WSTRACE wsasendto sock=%llx %luB -> %s [%s]", s, b ? b[0].len : 0, sa, hx);
    return ((int (__cdecl *)(unsigned long long, wsabuf_t *, unsigned long, unsigned long *,
                             unsigned long, const void *, int, void *, void *))
            g_ws_orig[2])(s, b, cnt, sent, flags, to, tolen, ov, comp);
}

static int __cdecl ws_recvfrom_hook(unsigned long long s, char *buf, int len, int flags,
                                    void *from, int *fromlen)
{
    int rc = ((int (__cdecl *)(unsigned long long, char *, int, int, void *, int *))
              g_ws_orig[3])(s, buf, len, flags, from, fromlen);
    if (rc > 0 && g_ws_events++ < 200) {
        char sa[64], hx[140];
        ws_sa(from, sa, sizeof(sa));
        ws_hex(buf, rc, hx, sizeof(hx));
        ws_emit("WSTRACE recvfrom sock=%llx %dB <- %s [%s]", s, rc, sa, hx);
    }
    return rc;
}

static int __cdecl ws_send_hook(unsigned long long s, const char *buf, int len, int flags)
{
    char hx[140];
    ws_hex(buf, len, hx, sizeof(hx));
    if (g_ws_events++ < 200) {
        void *fr[10];
        unsigned nf = bt_capture(fr, 10, 1);
        char st[300];
        bt_format(fr, nf, st, sizeof(st));
        ws_emit("WSTRACE send sock=%llx %dB [%s] stack:%s", s, len, hx, st);
    }
    return ((int (__cdecl *)(unsigned long long, const char *, int, int))g_ws_orig[4])
           (s, buf, len, flags);
}

static int __cdecl ws_wsasend_hook(unsigned long long s, wsabuf_t *b, unsigned long cnt,
                                   unsigned long *sent, unsigned long flags, void *ov, void *comp)
{
    char hx[140];
    if (b && cnt) ws_hex(b[0].buf, (int)b[0].len, hx, sizeof(hx)); else hx[0] = 0;
    if (g_ws_events++ < 200)
        ws_emit("WSTRACE wsasend sock=%llx %luB [%s]", s, b ? b[0].len : 0, hx);
    return ((int (__cdecl *)(unsigned long long, wsabuf_t *, unsigned long, unsigned long *,
                             unsigned long, void *, void *))g_ws_orig[5])
           (s, b, cnt, sent, flags, ov, comp);
}

static int __cdecl ws_wsarecv_hook(unsigned long long s, wsabuf_t *b, unsigned long cnt,
                                   unsigned long *recvd, unsigned long *flags, void *ov, void *comp)
{
    int rc = ((int (__cdecl *)(unsigned long long, wsabuf_t *, unsigned long, unsigned long *,
                               unsigned long *, void *, void *))g_ws_orig[6])
             (s, b, cnt, recvd, flags, ov, comp);
    if (rc == 0 && recvd && *recvd && g_ws_events++ < 200) {
        char hx[140];
        ws_hex(b[0].buf, (int)*recvd, hx, sizeof(hx));
        ws_emit("WSTRACE wsarecv sock=%llx %luB [%s]", s, *recvd, hx);
    }
    return rc;
}

/* ---- generic IAT hook by module + name, with a value fallback ---- *
 * v81: the v75 version only managed to patch ONE of its eight ws2_32 slots
 * (WSASendTo), so "no WSTRACE events" was never proof of anything. This walks
 * every import descriptor of the exe for the given module and matches each slot
 * by NAME (via OriginalFirstThunk) *and*, as a fallback, by the resolved export
 * address (covers bound imports and descriptors without an OFT).            */
typedef struct {
    const char *mod;
    const char *name;
    void *hook;
    void *orig;
} iat_entry;

static int iat_hook_set(iat_entry *ents, int n, const char *modname)
{
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    HMODULE mod;
    IMAGE_DOS_HEADER *dos;
    IMAGE_NT_HEADERS64 *nt;
    IMAGE_IMPORT_DESCRIPTOR *imp;
    int i, patched = 0;

    if (!base) return 0;
    mod = GetModuleHandleA(modname);
    if (!mod) mod = LoadLibraryA(modname);
    if (!mod) { probe_log("iat: %s not loadable", modname); return 0; }
    for (i = 0; i < n; i++)
        if (!ents[i].orig) ents[i].orig = (void *)(uintptr_t)GetProcAddress(mod, ents[i].name);

    dos = (IMAGE_DOS_HEADER *)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return 0;
    nt = (IMAGE_NT_HEADERS64 *)(base + dos->e_lfanew);
    imp = (IMAGE_IMPORT_DESCRIPTOR *)(base + nt->OptionalHeader.DataDirectory[1].VirtualAddress);
    for (; imp && imp->Name; imp++) {
        const char *mod_in_desc = (const char *)(base + imp->Name);
        IMAGE_THUNK_DATA64 *oft = imp->OriginalFirstThunk
            ? (IMAGE_THUNK_DATA64 *)(base + imp->OriginalFirstThunk) : NULL;
        IMAGE_THUNK_DATA64 *ft = (IMAGE_THUNK_DATA64 *)(base + imp->FirstThunk);
        unsigned idx;
        if (!ft || _stricmp(mod_in_desc, modname) != 0) continue;
        for (idx = 0; ft[idx].u1.Function; idx++) {
            const char *fn = NULL;
            unsigned long long slot_val = (unsigned long long)ft[idx].u1.Function;
            if (oft && !(oft[idx].u1.Function & 0x8000000000000000ull)) {
                IMAGE_IMPORT_BY_NAME *ibn = (IMAGE_IMPORT_BY_NAME *)
                    (base + (unsigned)(oft[idx].u1.Function & 0x7fffffffull));
                fn = (const char *)ibn->Name;
            }
            for (i = 0; i < n; i++) {
                int match = 0;
                DWORD old = 0;
                if (!ents[i].hook) continue;
                if (fn && _stricmp(fn, ents[i].name) == 0) match = 1;
                else if (ents[i].orig && slot_val == (unsigned long long)(uintptr_t)ents[i].orig)
                    match = 1;
                if (!match) continue;
                if (slot_val == (unsigned long long)(uintptr_t)ents[i].hook) continue;
                if (!VirtualProtect(&ft[idx], sizeof(void *), PAGE_READWRITE, &old)) continue;
                ents[i].orig = (void *)(uintptr_t)ft[idx].u1.Function;
                ft[idx].u1.Function = (unsigned long long)(uintptr_t)ents[i].hook;
                VirtualProtect(&ft[idx], sizeof(void *), old, &old);
                patched++;
                probe_log("iat: %s!%s hooked (slot %p)", modname, ents[i].name, (void *)&ft[idx]);
            }
        }
    }
    if (!patched) probe_log("iat: %s - no slot patched", modname);
    return patched;
}

/* ---- WinHTTP trace (v81) ----
 * The game's HTTP client is WinHTTP (13 imports), not ws2_32. Hooking
 * WinHttpConnect / WinHttpOpenRequest / WinHttpSendRequest gives the exact
 * request stream incl. loopback, with no dependence on server-side logging. */
static int (WINAPI *g_wh_connect)(void *, const wchar_t *, unsigned short, unsigned long);
static void *(WINAPI *g_wh_openreq)(void *, const wchar_t *, const wchar_t *, const wchar_t *,
                                    const wchar_t *, const wchar_t **, unsigned long);
static int (WINAPI *g_wh_send)(void *, const wchar_t *, unsigned long, void *, unsigned long,
                               unsigned long, unsigned long long);

static void wide_to_ascii(const wchar_t *w, char *out, int cap)
{
    int i = 0;
    if (cap <= 0) return;
    out[0] = 0;
    if (!w) return;
    while (w[i] && i < cap - 1) { out[i] = (w[i] < 128) ? (char)w[i] : '?'; i++; }
    out[i] = 0;
}

static int WINAPI wh_connect_hook(void *sess, const wchar_t *server, unsigned short port, unsigned long rsv)
{
    char hs[128];
    wide_to_ascii(server, hs, sizeof(hs));
    probe_log("HTTP connect %s:%u", hs, (unsigned)port);
    return g_wh_connect(sess, server, port, rsv);
}

static void *WINAPI wh_openreq_hook(void *conn, const wchar_t *verb, const wchar_t *obj,
                                    const wchar_t *ver, const wchar_t *ref, const wchar_t **types,
                                    unsigned long flags)
{
    char hv[16], ho[220];
    wide_to_ascii(verb, hv, sizeof(hv));
    wide_to_ascii(obj, ho, sizeof(ho));
    probe_log("HTTP %s %s", hv, ho);
    return g_wh_openreq(conn, verb, obj, ver, ref, types, flags);
}

static int WINAPI wh_send_hook(void *req, const wchar_t *hdr, unsigned long hdrlen, void *opt,
                               unsigned long optlen, unsigned long total, unsigned long long ctx)
{
    if (opt && optlen) {
        char head[120];
        unsigned i, n = optlen < 100 ? optlen : 100;
        for (i = 0; i < n; i++) {
            unsigned char c = ((const unsigned char *)opt)[i];
            head[i] = (c >= 0x20 && c < 0x7f) ? (char)c : '.';
        }
        head[n] = 0;
        probe_log("HTTP send body %luB: %s", optlen, head);
    }
    return g_wh_send(req, hdr, hdrlen, opt, optlen, total, ctx);
}

/* ---------------- notification apply probe (PN 184) ----------------
 *
 * The server now pushes an unsolicited US_SPACE_PARAMS_FULL_UPDATE text frame
 * on the /notifications websocket (mimic_server.py::space_params_notification)
 * because the idle-only record is FUN_140f4d940 (the config-store apply that
 * sets the reader gate bytes +0x58/+0x59) reached from the job step
 * FUN_140f0f6e0 = "JobManageConnection::pushNotification". After the first
 * such run cqsrv still reported buf=NULL len=0 str="" (the gate-closed
 * signature), so we need to know WHERE the chain stops:
 *   notif: line  -> the message reached the SDK notification queue and the
 *                   job step ran (the frame was not filtered away)
 *   apply: line  -> FUN_140f4d940 ran, with the gate bytes read AFTER it
 * No line at all means the client-side reader dropped the frame (subscription
 * or envelope problem), which is a SERVER-side fix.
 * Opt-in: UPC_NOTIF=1 (a hook on a job step is exactly the class of change
 * that crashed the client on PN 154, so it must never be on by default). */

#define NOTIF_RVA   0xf0f6e0u
#define NOTIF_COPY  22u
#define APPLY_RVA   0xf4d940u
#define APPLY_COPY  18u

static unsigned char *g_notif_tramp = NULL;
static unsigned char *g_napply_tramp = NULL;
static int g_notif_n = 0;
static int g_apply_n = 0;

typedef void *(__cdecl *notif_fn)(void *a, void *b, void *c, void *d);
static void log_doc_maps2(const char *tag, void *Y);   /* defined below */
typedef void *(__cdecl *storeapply_fn)(void *self, void *a2, void *a3);

__attribute__((noinline))
static void *__cdecl notif_hook(void *a, void *b, void *c, void *d)
{
    if (g_notif_n < 40) {
        g_notif_n++;
        probe_log("notif: pushNotification step ran #%d (job=%p force=%02x)",
                  g_notif_n, a,
                  (a && safe_readable((char *)a + 0x26, 1))
                      ? (unsigned)*(unsigned char *)((char *)a + 0x26) : 0u);
    }
    return ((notif_fn)g_notif_tramp)(a, b, c, d);
}

__attribute__((noinline))
static void *__cdecl storeapply_hook(void *self, void *a2, void *a3)
{
    unsigned long long p = 0;
    void *rv;
    if (self && safe_readable((char *)self + 0x20, 8))
        p = *(unsigned long long *)((char *)self + 0x20);
    rv = ((storeapply_fn)g_napply_tramp)(self, a2, a3);
    if (g_apply_n < 40) {
        g_apply_n++;
        log_doc_maps2("apply", self);
        probe_log("apply: config store apply ran #%d (store=%p +0x20=%p "
                  "flags58=%02x flags59=%02x)", g_apply_n, self, (void *)(uintptr_t)p,
                  (p && safe_readable((void *)(uintptr_t)(p + 0x58), 1))
                      ? (unsigned)*(unsigned char *)(uintptr_t)(p + 0x58) : 0xffu,
                  (p && safe_readable((void *)(uintptr_t)(p + 0x59), 1))
                      ? (unsigned)*(unsigned char *)(uintptr_t)(p + 0x59) : 0xffu);
    }
    return rv;
}

/* FUN_140edfe30 = the "config store is ready" predicate: it tests THREE
 * things and returns 0 (=> its callers fall back to a static empty string)
 * if any fails:
 *     mov rax,[rcx+0x10] ; cmp byte [rax+0x58],0 ; cmp byte [rax+0x59],0
 *     call FUN_140ee6020 ; return al
 * The apply hook already proves +0x58/+0x59 are set by our
 * US_SPACE_PARAMS_FULL_UPDATE push, so if the gate still reports "not ready"
 * the missing piece is FUN_140ee6020 (or the object is a different store).
 * This probe logs the verdict together with the object it judged. */
#define GATE_RVA   0xedfe30u
#define GATE_COPY  19u

typedef unsigned char (__cdecl *gate_fn)(void *self);
static unsigned char *g_gate_tramp = NULL;
static int g_gate_n = 0;

__attribute__((noinline))
static unsigned char __cdecl gate_probe_hook(void *self)
{
    unsigned long long w = 0;
    unsigned char r;
    unsigned long long s2 = 0;
    if (self && safe_readable((char *)self + 0x10, 8))
        w = *(unsigned long long *)((char *)self + 0x10);
    if (w && safe_readable((void *)(uintptr_t)(w + 0x20), 8))
        s2 = *(unsigned long long *)(uintptr_t)(w + 0x20);
    r = ((gate_fn)g_gate_tramp)(self);
    if (g_gate_n < 40) {
        g_gate_n++;
        probe_log("gate: ready=%u self=%p W=[self+0x10]=%p W58=%02x W59=%02x "
                  "Z=[W+0x20]=%p Z58=%02x Z59=%02x",
                  (unsigned)r, self, (void *)(uintptr_t)w,
                  (w && safe_readable((void *)(uintptr_t)(w + 0x58), 1))
                      ? (unsigned)*(unsigned char *)(uintptr_t)(w + 0x58) : 0xffu,
                  (w && safe_readable((void *)(uintptr_t)(w + 0x59), 1))
                      ? (unsigned)*(unsigned char *)(uintptr_t)(w + 0x59) : 0xffu,
                  (void *)(uintptr_t)s2,
                  (s2 && safe_readable((void *)(uintptr_t)(s2 + 0x58), 1))
                      ? (unsigned)*(unsigned char *)(uintptr_t)(s2 + 0x58) : 0xffu,
                  (s2 && safe_readable((void *)(uintptr_t)(s2 + 0x59), 1))
                      ? (unsigned)*(unsigned char *)(uintptr_t)(s2 + 0x59) : 0xffu);
    }
    return r;
}

/* ---- the parameter document, correctly dereferenced (PN 186) -------------
 *
 * The first attempt read [store+0x20] and produced nonsense (a "257-member
 * document" that was really an empty std::map, and gate bytes 0xc2/0xb5).
 * The real chain, from decomp/_adhoc/conquest-doc.md:
 *
 *   H = [manager+0x118]
 *   A = FUN_140e745e0(*H, *H+0xc0)        (lazy "ParametersClient")
 *   Y = [*A + 0x20]                       (parameter-document object)
 *   reader map : object at Y+0x450, _Myhead [Y+0x458], _Mysize [Y+0x460]
 *   writer map : the apply copies its staged tree into [[Y]+0x28]+0x10
 *   node       : 0x28 bytes, _Left 0, _Parent 8, _Right 0x10,
 *                color/isnil 0x18/0x19, key at 0x20 (std::string)
 *
 * If the two maps are the same object, our push must have filled the reader
 * map and the empty conquest URL means the section/key path is wrong. If they
 * differ, the whole notification route writes into a map nobody reads. One
 * run settles it. */
#define PARAMSCLIENT_RVA   0xe745e0u
#define PARAMSCLIENT_COPY  14u

typedef void *(__cdecl *pc_fn)(void *a, void *b);
static unsigned char *g_pc_tramp = NULL;
static int g_pc_n = 0;
static unsigned long long g_pc_last = 0;

__attribute__((noinline))
static void *__cdecl pc_hook(void *a, void *b)
{
    void *rv = ((pc_fn)g_pc_tramp)(a, b);
    if (b && safe_readable(b, 8)) g_pc_last = *(unsigned long long *)b;
    if (g_pc_n < 20) {
        g_pc_n++;
        probe_log("paramsclient: a=%p slot=%p -> rv=%p cached=%p", a, b, rv,
                  (void *)(uintptr_t)g_pc_last);
    }
    return rv;
}

/* key of a std::map node, tolerant about the two plausible layouts */
static int node_key(void *node, char *out, size_t cap)
{
    unsigned long long len = 0, c = 0, p = 0;
    const char *buf = NULL;
    size_t i;
    if (!node || !safe_readable(node, 0x38)) return 0;
    /* A: inline std::string at node+0x20 */
    len = *(unsigned long long *)((char *)node + 0x28);
    c = *(unsigned long long *)((char *)node + 0x30);
    buf = (c >= 0x10) ? *(const char **)((char *)node + 0x20)
                      : (const char *)((char *)node + 0x20);
    if (!(len > 0 && len < cap - 1 && buf && safe_readable(buf, len))) {
        /* B: pointer at node+0x20 -> std::string object */
        p = *(unsigned long long *)((char *)node + 0x20);
        if (!p || !safe_readable((void *)(uintptr_t)p, 0x20)) return 0;
        len = *(unsigned long long *)(uintptr_t)(p + 8);
        c = *(unsigned long long *)(uintptr_t)(p + 0x10);
        buf = (c >= 0x10) ? *(const char **)(uintptr_t)p : (const char *)(uintptr_t)p;
        if (!(len > 0 && len < cap - 1 && buf && safe_readable(buf, len))) return 0;
    }
    for (i = 0; i < len; i++) {
        unsigned char ch = (unsigned char)buf[i];
        if (ch < 0x20 || ch > 0x7e) return 0;
    }
    memcpy(out, buf, len);
    out[len] = 0;
    return 1;
}

static void dump_map(const char *tag, void *mapobj, int max_keys)
{
    unsigned long long head = 0, size = 0, root = 0;
    void *stack[48];
    int sp = 0, emitted = 0, visited = 0;
    if (!mapobj || !safe_readable(mapobj, 0x18)) {
        probe_log("%s: map=%p unreadable", tag, mapobj);
        return;
    }
    head = *(unsigned long long *)((char *)mapobj + 8);
    size = *(unsigned long long *)((char *)mapobj + 0x10);
    probe_log("%s: map=%p head=%p size=%llu", tag, mapobj, (void *)(uintptr_t)head, size);
    if (!head || !safe_readable((void *)(uintptr_t)head, 0x18)) return;
    root = *(unsigned long long *)(uintptr_t)(head + 8);   /* head->_Parent = root */
    if (!root || !safe_readable((void *)(uintptr_t)root, 0x20)) return;
    stack[sp++] = (void *)(uintptr_t)root;
    while (sp > 0 && visited < 200 && emitted < max_keys) {
        void *n = stack[--sp];
        char k[72];
        unsigned long long l = 0, r = 0;
        if (!n || !safe_readable(n, 0x20)) continue;
        visited++;
        l = *(unsigned long long *)n;
        r = *(unsigned long long *)((char *)n + 0x10);
        if (l && safe_readable((void *)(uintptr_t)l, 0x20)) stack[sp < 47 ? sp++ : sp - 1] = (void *)(uintptr_t)l;
        if (r && safe_readable((void *)(uintptr_t)r, 0x20)) stack[sp < 47 ? sp++ : sp - 1] = (void *)(uintptr_t)r;
        if (node_key(n, k, sizeof(k))) {
            probe_log("%s: key[%d]=\"%s\"", tag, emitted, k);
            emitted++;
        }
    }
    probe_log("%s: walked %d nodes, %d keys", tag, visited, emitted);
}

/* The two gates at the top of FUN_140f3b1d0. Each is a thin wrapper that
 * looks its section up in the notification `content` and returns 0 when it is
 * absent; staging bails out on either 0. Logging their verdicts (instead of
 * guessing which section name is required - two guesses were wrong) is the
 * cheapest way to see what the client actually wants. */
#define GATE1_RVA   0xf4c980u
#define GATE1_COPY  15u
#define GATE2_RVA   0xf4c420u
#define GATE2_COPY  14u

typedef unsigned char (__cdecl *gatewrap_fn)(void *a, void *b, void *c, void *d);
static unsigned char *g_gate1_tramp = NULL;
static unsigned char *g_gate2_tramp = NULL;
static int g_gate1_n = 0, g_gate2_n = 0;

__attribute__((noinline))
static unsigned char __cdecl gate1_hook(void *a, void *b, void *c, void *d)
{
    unsigned char r = ((gatewrap_fn)g_gate1_tramp)(a, b, c, d);
    if (g_gate1_n < 20) {
        g_gate1_n++;
        probe_log("gate1(FUN_140f4c980 us-sdkClientUrls@doc+0x228): %s", r ? "ok" : "ABSENT -> staging aborts");
    }
    return r;
}

__attribute__((noinline))
static unsigned char __cdecl gate2_hook(void *a, void *b, void *c, void *d)
{
    unsigned char r = ((gatewrap_fn)g_gate2_tramp)(a, b, c, d);
    if (g_gate2_n < 20) {
        g_gate2_n++;
        probe_log("gate2(FUN_140f4c420 @doc+0x1d8): %s", r ? "ok" : "ABSENT -> staging aborts");
    }
    return r;
}

/* ---- staging trace (PN 188) ------------------------------------------------
 *
 * Why the "Custom" map stays empty even when both required sections are
 * present needs the staging step's own decisions:
 *   0xf3b1d0  FUN_140f3b1d0  staging (returns early unless us-sdkClientUrls
 *                            AND us-sdkClientSettingsHttpGame are found)
 *   0xef5760  FUN_140ef5760  "find my section by name" -> 0 when absent
 *   0xef5b70  FUN_140ef5b70  the last step that copies every non-us-* section
 *                            into the "Custom" map the conquest getter reads
 * One run shows which section names are asked for, whether each was found,
 * and whether the Custom filler ran at all. */
#define STAGING_RVA  0xf3b1d0u
#define STAGING_COPY 20u
#define SECFIND_RVA  0xef5760u
#define SECFIND_COPY 14u
#define CUSTOM_RVA   0xef5b70u
#define CUSTOM_COPY  16u

typedef void *(__cdecl *staging_fn)(void *a, void *b, void *c, void *d);
typedef void *(__cdecl *secfind_fn)(void *a, void *b, void *c, void *d);
static unsigned char *g_staging_tramp = NULL;
static unsigned char *g_secfind_tramp = NULL;
static unsigned char *g_custom_tramp = NULL;
static int g_staging_n = 0, g_secfind_n = 0, g_custom_n = 0;

__attribute__((noinline))
static void *__cdecl staging_hook(void *a, void *b, void *c, void *d)
{
    void *rv = ((staging_fn)g_staging_tramp)(a, b, c, d);
    if (g_staging_n < 20) {
        g_staging_n++;
        probe_log("staging: FUN_140f3b1d0(this=%p content=%p override=%p) -> %p",
                  a, b, c, rv);
        log_doc_maps2("staging", a);
    }
    return rv;
}

__attribute__((noinline))
static void *__cdecl custom_hook(void *a, void *b, void *c, void *d)
{
    void *rv;
    if (g_custom_n < 20) {
        g_custom_n++;
        probe_log("custom: enter FUN_140ef5b70(this=%p a2=%p a3=%p)", a, b, c);
        log_doc_maps2("custom-before", NULL);      /* the getter's real Y */
    }
    rv = ((staging_fn)g_custom_tramp)(a, b, c, d);
    if (g_custom_n <= 20) {
        probe_log("custom: FUN_140ef5b70(this=%p) -> %p", a, rv);
        log_doc_maps2("custom-after", NULL);
    }
    return rv;
}

/* FUN_140ef5760(this, content, override) = "find MY section in content".
 * The name is not an argument: `this` is a document-section object and its
 * name sits at +0x8 (the same layout as the "Custom" section at Y+0x448).
 * Logging the name + the verdict is what tells us which gate misses. */
__attribute__((noinline))
static void *__cdecl secfind_hook(void *a, void *b, void *c, void *d)
{
    void *rv;
    char name[64] = {0};
    int has = 0;
    if (a && safe_readable((char *)a + 8, 8)) {
        unsigned long long sp = *(unsigned long long *)((char *)a + 8);
        if (sp && safe_readable((void *)(uintptr_t)sp, 1)) {
            const char *p = (const char *)(uintptr_t)sp;
            int i = 0;
            while (i < 60 && safe_readable(p + i, 1) && p[i]) { name[i] = p[i]; i++; }
            name[i] = 0;
            has = (i > 0) ? 1 : 0;
        }
    }
    rv = ((secfind_fn)g_secfind_tramp)(a, b, c, d);
    if (g_secfind_n < 60) {
        g_secfind_n++;
        probe_log("secfind: section=\"%s\"%s -> %p (%s)", name,
                  has ? "" : " <unnamed>", rv, rv ? "FOUND" : "ABSENT");
    }
    return rv;
}

/* Y for the current ParametersClient, or 0 */
static unsigned long long param_doc_Y(void)
{
    unsigned long long A = g_pc_last, obj = 0, Y = 0;
    if (!A || !safe_readable((void *)(uintptr_t)A, 8)) return 0;
    obj = *(unsigned long long *)(uintptr_t)A;
    if (!obj || !safe_readable((void *)(uintptr_t)(obj + 0x20), 8)) return 0;
    Y = *(unsigned long long *)(uintptr_t)(obj + 0x20);
    if (!Y || !safe_readable((void *)(uintptr_t)Y, 0x470)) return 0;
    return Y;
}

/* Log the two candidate documents for a given Y.
 *
 * Y is the object FUN_140f4d940 receives (the apply hook's `self` is exactly
 * the same pointer the conquest getter walks to, measured 05:06:05).
 *   staged tree (what the apply fills):  Y+0x4b8, map object at +0x10
 *   document map (what the apply copies into): [*Y+0x28], map object at +0x10
 *   reader map (what FUN_140c18b60 walks):      Y+0x450
 * Note the double dereference on the writer side - an earlier version used
 * [Y+0x28] and therefore compared against a .data address. */
static void log_one_map(const char *tag, const char *what, unsigned long long mapobj)
{
    unsigned long long head = 0, size = 0;
    if (!mapobj || !safe_readable((void *)(uintptr_t)mapobj, 0x18)) {
        probe_log("%s: %s map=%p unreadable", tag, what, (void *)(uintptr_t)mapobj);
        return;
    }
    head = *(unsigned long long *)(uintptr_t)(mapobj + 8);
    size = *(unsigned long long *)(uintptr_t)(mapobj + 0x10);
    probe_log("%s: %s map=%p head=%p size=%llu", tag, what, (void *)(uintptr_t)mapobj,
              (void *)(uintptr_t)head, size);
    if (size && size < 100000) dump_map(tag, (void *)(uintptr_t)mapobj, 12);
}

static void log_doc_maps2(const char *tag, void *Yv)
{
    unsigned long long Y = (unsigned long long)(uintptr_t)Yv;
    unsigned long long obj = 0, impl = 0, wmap = 0, staged = 0;
    if (!Y) Y = param_doc_Y();
    if (!Y) {
        probe_log("%s: no ParametersClient yet (Y=0)", tag);
        return;
    }
    if (safe_readable((void *)(uintptr_t)Y, 8))
        obj = *(unsigned long long *)(uintptr_t)Y;
    if (obj && safe_readable((void *)(uintptr_t)(obj + 0x28), 8))
        impl = *(unsigned long long *)(uintptr_t)(obj + 0x28);
    if (impl) wmap = impl + 0x10;
    staged = Y + 0x4b8;
    probe_log("%s: Y=%p obj=[Y]=%p impl=[obj+0x28]=%p writerMap=%p readerMap=%p SAME=%d",
              tag, (void *)(uintptr_t)Y, (void *)(uintptr_t)obj, (void *)(uintptr_t)impl,
              (void *)(uintptr_t)wmap, (void *)(uintptr_t)(Y + 0x450),
              (wmap == Y + 0x450) ? 1 : 0);
    log_one_map(tag, "reader", Y + 0x450);
    if (wmap && wmap != Y + 0x450) log_one_map(tag, "writer", wmap);
    log_one_map(tag, "staged", staged + 0x10);
}

/* FUN_140c18b60 = the AnnoURLs path getter: FUN_140c18b60(out_string,
 * manager, "ConquestServer", "AnnoURLs"). It walks
 *     store = [manager+0x118];  Z = [store+0x20];  doc = [Z+0x38]
 * and returns the string member. PN 182 read "buf=NULL len=0 str=\"\"" as
 * "the gate is closed"; that reading was WRONG - rvstore_patch_install (ON by
 * default) has already replaced FUN_140edfe30 with a function that always
 * answers "ready", so the empty string means the VALUE is not in the parsed
 * document. This probe logs the whole chain (including the document pointer
 * and its element count) so we can see whether the document is missing,
 * empty, or merely lacks AnnoURLs.ConquestServer. */
#define CQGET_RVA   0xc18b60u
#define CQGET_COPY  17u

typedef void *(__cdecl *cqget_fn)(void *out, void *mgr, void *key, void *section);
static unsigned char *g_cqget_tramp = NULL;
static int g_cqget_n = 0;

/* std::string (MSVC): buf at +0, size at +8; SSO buffer starts at +0x10 */
static void log_std_string(const char *what, void *str)
{
    unsigned long long len = 0, cap = 0;
    const char *buf = NULL;
    char text[96] = {0};
    if (!str || !safe_readable(str, 0x20)) {
        probe_log("%s: <unreadable %p>", what, str);
        return;
    }
    len = *(unsigned long long *)((char *)str + 8);
    cap = *(unsigned long long *)((char *)str + 0x10);
    buf = (cap >= 0x10) ? *(const char **)str : (const char *)str;
    if (buf && len && safe_readable(buf, len < 80 ? len : 80)) {
        size_t n = len < 80 ? (size_t)len : 80;
        memcpy(text, buf, n);
        text[n] = 0;
    }
    probe_log("%s: len=%llu cap=%llu str=\"%s\"", what, len, cap, text);
}

__attribute__((noinline))
static void *__cdecl cqget_hook(void *out, void *mgr, void *key, void *section)
{
    void *rv;
    unsigned long long store = 0, Z = 0, doc = 0, alt = 0;
    unsigned long long count_38 = 0, count_30 = 0;
    void *inner = NULL;
    if (mgr && safe_readable((char *)mgr + 0x118, 8))
        store = *(unsigned long long *)((char *)mgr + 0x118);
    if (store && safe_readable((void *)(uintptr_t)(store + 0x20), 8))
        Z = *(unsigned long long *)(uintptr_t)(store + 0x20);
    if (Z && safe_readable((void *)(uintptr_t)(Z + 0x38), 8))
        doc = *(unsigned long long *)(uintptr_t)(Z + 0x38);
    if (Z && safe_readable((void *)(uintptr_t)(Z + 0x30), 8))
        alt = *(unsigned long long *)(uintptr_t)(Z + 0x30);
    if (doc && safe_readable((void *)(uintptr_t)(doc + 0x18), 8))
        count_38 = *(unsigned long long *)(uintptr_t)(doc + 0x18);
    if (alt && safe_readable((void *)(uintptr_t)(alt + 0x18), 8))
        count_30 = *(unsigned long long *)(uintptr_t)(alt + 0x18);
    if (store && safe_readable((void *)(uintptr_t)(store + 0x28), 8))
        inner = (void *)(uintptr_t) * (unsigned long long *)(uintptr_t)(store + 0x28);
    rv = ((cqget_fn)g_cqget_tramp)(out, mgr, key, section);
    if (g_cqget_n < 40) {
        g_cqget_n++;
        log_doc_maps2("cqget", NULL);
        probe_log("cqget: key=\"%s\" section=\"%s\"", 
                  (key && safe_readable(key, 1)) ? (const char *)key : "?",
                  (section && safe_readable(section, 1)) ? (const char *)section : "?");
        probe_log("cqget: mgr=%p store=%p Z=%p doc38=%p n38=%llu Z30=%p n30=%llu "
                  "store28=%p Z58=%02x Z59=%02x",
                  mgr, (void *)(uintptr_t)store, (void *)(uintptr_t)Z,
                  (void *)(uintptr_t)doc, count_38, (void *)(uintptr_t)alt, count_30,
                  inner,
                  (Z && safe_readable((void *)(uintptr_t)(Z + 0x58), 1))
                      ? (unsigned)*(unsigned char *)(uintptr_t)(Z + 0x58) : 0xffu,
                  (Z && safe_readable((void *)(uintptr_t)(Z + 0x59), 1))
                      ? (unsigned)*(unsigned char *)(uintptr_t)(Z + 0x59) : 0xffu);
        log_std_string("cqget: result", out);
    }
    return rv;
}

/* ---------------- conquest "Custom" section patch (PN 192) ----------------
 *
 * FUN_140f3b1d0 stages a US_SPACE_PARAMS_FULL_UPDATE notification and its last
 * statement dispatches slot 0 of the object at Y+0x448 (vtable 0x14158e320,
 * slot 0 = FUN_140ef5b70 = "copy every non-us-* section into the Custom map").
 * Two presence gates at the top of the function can abort before that:
 *   je 0xf3b209   gate 1 (us-sdkClientUrls, doc+0x228)
 *   je 0xf3b222   gate 2 (doc+0x1d8)
 * Live runs show the abort is taken even when the server sends BOTH sections
 * (four notification kinds, sections at root / under "parameters" / under the
 * space id / under "spaces"; the filler never fires and the Custom map stays
 * empty), so the remaining lever is this second branch, which sits in front of
 * the "Custom" write and in front of a switch-array write only:
 *     0xf3b222  0f 84 4a 01 00 00   je +0x14a   ->   90 x 6
 *
 * OFF BY DEFAULT and it must stay that way for any "looks like the real
 * server" run: a code patch is not a server behaviour. It exists to measure
 * whether conquest is otherwise reachable, and for that purpose it is opt-in
 * via UPC_CONQCUSTOM=1. Bytes are verified before the write.
 */
static int gate_write(unsigned long rva, const unsigned char *expect,
                      const unsigned char *repl, int ncheck, int nwrite);

static void conqcustom_patch_install(void)
{
    static const unsigned char expect[6] = { 0x0f, 0x84, 0x4a, 0x01, 0x00, 0x00 };
    static const unsigned char repl[6] = { 0x90, 0x90, 0x90, 0x90, 0x90, 0x90 };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_CONQCUSTOM", dis, sizeof(dis)) == 0 || dis[0] != '1') {
        probe_log("conqcustom patch: off (UPC_CONQCUSTOM=1 enables; it is a CODE PATCH, "
                  "see PN 192)");
        return;
    }
    gate_write(0xf3b222u, expect, repl, 6, 6);
    /* gate 1, the FIRST early exit (FUN_140f4c980 -> us-sdkClientUrls at
     * doc+0x228). Measured 07:24: patching only the second jump changes
     * nothing - the filler still never runs - so the abort is this one:
     *     0xf3b209  0f 84 63 01 00 00   je +0x163   ->   90 x 6
     * (the displacement differs from gate 2 - reusing gate 2's expect array
     * made gate_write refuse the patch, which cost one run) */
    {
        static const unsigned char expect1[6] = { 0x0f, 0x84, 0x63, 0x01, 0x00, 0x00 };
        gate_write(0xf3b209u, expect1, repl, 6, 6);
    }
}

static void notif_probes_install(void)
{
    /* FUN_140f0f6e0:
     *   48897c2420        mov [rsp+0x20], rdi
     *   55                push rbp
     *   4154              push r12
     *   4155              push r13
     *   4156              push r14
     *   4157              push r15
     *   488dac2430e3ffff  lea rbp, [rsp-0x1cd0]
     *  = 23 bytes, ends on an instruction boundary */
    static const unsigned char e_notif[NOTIF_COPY] = {
        0x48, 0x89, 0x7c, 0x24, 0x20,
        0x55, 0x41, 0x54, 0x41, 0x55, 0x41, 0x56, 0x41, 0x57,
        0x48, 0x8d, 0xac, 0x24, 0x30, 0xe3, 0xff, 0xff
    };
    /* FUN_140f4d940:
     *   48895c2418        mov [rsp+0x18], rbx
     *   4889742420        mov [rsp+0x20], rsi
     *   57                push rdi
     *   4883ec60          sub rsp, 0x60
     *   488b01            mov rax, [rcx]
     *  = 19 bytes, ends on an instruction boundary */
    static const unsigned char e_apply[APPLY_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x18,
        0x48, 0x89, 0x74, 0x24, 0x20,
        0x57,
        0x48, 0x83, 0xec, 0x60,
        0x48, 0x8b, 0x01
    };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_NOTIF", dis, sizeof(dis)) == 0 || dis[0] != '1') {
        probe_log("notif probes: off (UPC_NOTIF=1 enables the pushNotification/apply hooks)");
        return;
    }
    hook_install("notif step", NOTIF_RVA, e_notif, NOTIF_COPY,
                 &g_notif_tramp, (void *)notif_hook);
    hook_install("apply probe", APPLY_RVA, e_apply, APPLY_COPY,
                 &g_napply_tramp, (void *)storeapply_hook);
    /* FUN_140edfe30:
     *   4883ec28        sub rsp, 0x28
     *   488b4110        mov rax, [rcx+0x10]
     *   90              nop
     *   80785800        cmp byte [rax+0x58], 0
     *   741b            je  +0x1b
     *   488b4110        mov rax, [rcx+0x10]      <- 19 bytes, boundary-safe */
    static const unsigned char e_gate[GATE_COPY] = {
        0x48, 0x83, 0xec, 0x28,
        0x48, 0x8b, 0x41, 0x10,
        0x90,
        0x80, 0x78, 0x58, 0x00,
        0x74, 0x1b,
        0x48, 0x8b, 0x41, 0x10
    };
    hook_install("gate probe", GATE_RVA, e_gate, GATE_COPY,
                 &g_gate_tramp, (void *)gate_probe_hook);
    /* FUN_140c18b60:
     *   48895c2418      mov [rsp+0x18], rbx
     *   48894c2408      mov [rsp+8], rcx
     *   55 56 57        push rbp/rsi/rdi
     *   4154 4155       push r12/r13            -> 17 bytes, boundary-safe */
    static const unsigned char e_cqget[CQGET_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x18,
        0x48, 0x89, 0x4c, 0x24, 0x08,
        0x55, 0x56, 0x57,
        0x41, 0x54, 0x41, 0x55
    };
    hook_install("cqget probe", CQGET_RVA, e_cqget, CQGET_COPY,
                 &g_cqget_tramp, (void *)cqget_hook);
    /* FUN_140e745e0:
     *   4889742410      mov [rsp+0x10], rsi
     *   57              push rdi
     *   4883ec20        sub rsp, 0x20
     *   48833a00        cmp qword ptr [rdx], 0    -> 14 bytes, boundary-safe */
    static const unsigned char e_pc[PARAMSCLIENT_COPY] = {
        0x48, 0x89, 0x74, 0x24, 0x10,
        0x57,
        0x48, 0x83, 0xec, 0x20,
        0x48, 0x83, 0x3a, 0x00
    };
    hook_install("paramsclient probe", PARAMSCLIENT_RVA, e_pc, PARAMSCLIENT_COPY,
                 &g_pc_tramp, (void *)pc_hook);
    /* FUN_140f3b1d0 staging: 5+5+5+1+4 = 20 bytes, boundary-safe */
    static const unsigned char e_stg[STAGING_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x08,
        0x48, 0x89, 0x6c, 0x24, 0x10,
        0x48, 0x89, 0x74, 0x24, 0x18,
        0x57,
        0x48, 0x83, 0xec, 0x20
    };
    hook_install("staging probe", STAGING_RVA, e_stg, STAGING_COPY,
                 &g_staging_tramp, (void *)staging_hook);
    /* FUN_140ef5760 section find: 5+1+2+2+4 = 14 bytes */
    static const unsigned char e_sec[SECFIND_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x20,
        0x56, 0x41, 0x56, 0x41, 0x57,
        0x48, 0x83, 0xec, 0x70
    };
    hook_install("secfind probe", SECFIND_RVA, e_sec, SECFIND_COPY,
                 &g_secfind_tramp, (void *)secfind_hook);
    /* FUN_140ef5b70 Custom filler: 5+1+1+1+2+2+2+2 = 16 bytes */
    static const unsigned char e_cus[CUSTOM_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x18,
        0x55, 0x56, 0x57,
        0x41, 0x54, 0x41, 0x55, 0x41, 0x56, 0x41, 0x57
    };
    hook_install("custom probe", CUSTOM_RVA, e_cus, CUSTOM_COPY,
                 &g_custom_tramp, (void *)custom_hook);
    /* PN 190: the gate1/gate2 wrapper hooks were REMOVED - with them
     * installed the RV login never ran at all (4 runs, 0 calls). A probe that
     * changes the behaviour it measures is worthless. */
}

static void ws_trace_install(void)
{
    static iat_entry ws_ents[8];
    static int g_ws_mode = 1;
    static iat_entry wh_ents[3];
    char dis[8] = {0};

    /* v81b: only the WinHTTP hooks are ON by default. Hooking the ws2_32 data
     * path (send/sendto/connect) kills the process ~2 s into startup - the same
     * libcurl breakage documented for the v52 netprobe - so it stays opt-in
     * behind UPC_WSTRACE=2. */
    {
        int mode = 1;
        if (GetEnvironmentVariableA("UPC_WSTRACE", dis, sizeof(dis)) > 0) {
            if (dis[0] == '0') { probe_log("wstrace: off (UPC_WSTRACE=0)"); return; }
            if (dis[0] == '2') mode = 2;
            if (dis[0] == '3') mode = 3;      /* v96: send/WSASend only + backtrace */
        }
        g_ws_mode = mode;
    }
    ws_ents[0].name = "connect";    ws_ents[0].hook = (void *)ws_connect_hook;
    ws_ents[1].name = "sendto";     ws_ents[1].hook = (void *)ws_sendto_hook;
    ws_ents[2].name = "WSASendTo";  ws_ents[2].hook = (void *)ws_wsasendto_hook;
    ws_ents[3].name = "recvfrom";   ws_ents[3].hook = (void *)ws_recvfrom_hook;
    ws_ents[4].name = "send";       ws_ents[4].hook = (void *)ws_send_hook;
    ws_ents[5].name = "WSASend";    ws_ents[5].hook = (void *)ws_wsasend_hook;
    ws_ents[6].name = "WSARecv";    ws_ents[6].hook = (void *)ws_wsarecv_hook;
    ws_ents[7].name = "getaddrinfo"; ws_ents[7].hook = NULL;
    if (g_ws_mode == 3) {
        /* Only the two send entry points, with a caller backtrace: enough to see
         * WHICH client code writes the RV frames (and in which flavour), without
         * touching connect/sendto which broke libcurl in earlier builds. */
        iat_entry only[2];
        only[0].mod = "WS2_32.dll"; only[0].name = "send";   only[0].hook = (void *)ws_send_hook;
        only[0].orig = NULL;
        only[1].mod = "WS2_32.dll"; only[1].name = "WSASend"; only[1].hook = (void *)ws_wsasend_hook;
        only[1].orig = NULL;
        iat_hook_set(only, 2, "WS2_32.dll");
    } else if (g_ws_mode >= 2) {
        int i;
        for (i = 0; i < 8; i++) ws_ents[i].mod = "WS2_32.dll";
        iat_hook_set(ws_ents, 8, "WS2_32.dll");
    } else {
        probe_log("wstrace: mode 1 (WinHTTP only; UPC_WSTRACE=2 adds the ws2_32 data path)");
    }
    wh_ents[0].name = "WinHttpConnect";       wh_ents[0].hook = (void *)wh_connect_hook;
    wh_ents[1].name = "WinHttpOpenRequest";   wh_ents[1].hook = (void *)wh_openreq_hook;
    wh_ents[2].name = "WinHttpSendRequest";   wh_ents[2].hook = (void *)wh_send_hook;
    iat_hook_set(wh_ents, 3, "WINHTTP.dll");
    g_wh_connect = (int (WINAPI *)(void *, const wchar_t *, unsigned short, unsigned long))
                   (uintptr_t)wh_ents[0].orig;
    g_wh_openreq = (void *(WINAPI *)(void *, const wchar_t *, const wchar_t *, const wchar_t *,
                                     const wchar_t *, const wchar_t **, unsigned long))
                   (uintptr_t)wh_ents[1].orig;
    g_wh_send = (int (WINAPI *)(void *, const wchar_t *, unsigned long, void *, unsigned long,
                                unsigned long, unsigned long long))(uintptr_t)wh_ents[2].orig;
    probe_log("wstrace: ws2_32=%p/%p winhttp=%p/%p/%p", ws_ents[0].orig, ws_ents[2].orig,
              wh_ents[0].orig, wh_ents[1].orig, wh_ents[2].orig);
}

/* ---------------- noquit gate (v28) ----------------
 * After the conquest chain completes and the storm/rendezvous layer fails
 * against our stub listener, the game calls the shared quit wrapper
 * FUN_1401329d0 (exit(-1)) from FUN_140136cf0 (invoked via a computed
 * pointer, observed 0.4-0.8 s after "Storm Online state changed").
 * Patch: wrapper entry -> xor eax,eax; ret (and NOP the int3 that follows
 * the call in FUN_140136cf0) so a failed storm connection is survivable
 * and the conquest session stays up. **DIAGNOSTIC TOOL ONLY, default OFF
 * as of v29 — the real fix is making the storm/rendezvous connection work
 * server-side.** Set UPC_NOQUIT=1 for a specific diagnostic run. NOTE:
 * this also disables this specific abnormal-exit path. */

static int gate_write(unsigned long rva, const unsigned char *expect,
                      const unsigned char *repl, int ncheck, int nwrite);

static void noquit_patch_install(void)
{
    char dis[8] = {0};
    static const unsigned char w_expect[4] = {
        0x48, 0x83, 0xec, 0x28,         /* sub rsp, 0x28 */
    };
    static const unsigned char w_repl[4] = { 0x31, 0xc0, 0xc3, 0x90 }; /* xor eax,eax; ret; nop */
    unsigned char i_expect[1], i_repl[2] = { 0x90, 0x90 };
    unsigned char *t;

    if (GetEnvironmentVariableA("UPC_NOQUIT", dis, sizeof(dis)) > 0 && dis[0] == '1') {
        probe_log("noquit patch: enabled (UPC_NOQUIT=1 diagnostic mode)");
    }
    else {
        probe_log("noquit patch: default OFF (UPC_NOQUIT=1 enables for diagnostics)");
        return;
    }
    /* wrapper entry */
    t = (unsigned char *)GetModuleHandleA(NULL) + 0x1329d0u;
    if (!safe_readable(t, 8) || memcmp(t, w_expect, 4) != 0) {
        probe_log("noquit patch: wrapper bytes mismatch at %p - skipped", (void *)t);
        return;
    }
    /* the 7-byte int3 padding after the call (ret addr 0x136cf9): replace
     * with `add rsp,0x28; ret` so FUN_140136cf0 returns cleanly to its
     * caller instead of walking into the remaining int3s (this killed the
     * process silently in the v28 test run). */
    t = (unsigned char *)GetModuleHandleA(NULL) + 0x136cf9u;
    if (!safe_readable(t, 8) || t[0] != 0xCC || t[6] != 0xCC || t[7] == 0xCC) {
        probe_log("noquit patch: tail bytes mismatch at %p - skipped", (void *)t);
        return;
    }
    i_expect[0] = 0xCC;
    {
        unsigned char tail[7] = { 0x48, 0x83, 0xc4, 0x28, 0xc3, 0x90, 0x90 };
        gate_write(0x136cf9u, i_expect, tail, 1, 7);
    }
    gate_write(0x1329d0u, w_expect, w_repl, 4, 4);
    probe_log("noquit patch: installed (exit(-1) wrapper is now a clean return)");
}

/* ---------------- optional "force parameters ready" gate patch ----------------
 *
 * Escape hatch, OFF by default: create an empty file named
 * "force-params-ready.txt" next to Anno5.exe and the known gate sites are
 * rewired so the "parameters are ready" test always takes its success path.
 * Used to discover the NEXT failure when the runtime probe is inconclusive.
 * Every patch verifies the original bytes first.
 */

static int gate_write(unsigned long rva, const unsigned char *expect,
                      const unsigned char *repl, int ncheck, int nwrite)
{
    unsigned char *target = (unsigned char *)GetModuleHandleA(NULL) + rva;
    DWORD old = 0;
    if (!safe_readable(target, (size_t)nwrite)) {
        probe_log("gate patch: target %p not readable - skipped", target);
        return 0;
    }
    if (memcmp(target, expect, (size_t)ncheck) != 0) {
        probe_log("gate patch: byte mismatch at %p (rva 0x%lx) - skipped", target, rva);
        return 0;
    }
    if (!VirtualProtect(target, (SIZE_T)nwrite, PAGE_EXECUTE_READWRITE, &old)) return 0;
    memcpy(target, repl, (size_t)nwrite);
    VirtualProtect(target, (SIZE_T)nwrite, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, (SIZE_T)nwrite);
    probe_log("gate patch: applied at rva 0x%lx", rva);
    return 1;
}

/* short conditional jump (76/75 rel8) -> 2 x NOP : never take the fail branch */
static void gate_drop_jcc8(unsigned long rva)
{
    unsigned char expect[1];
    unsigned char repl[2] = {0x90, 0x90};
    unsigned char *target = (unsigned char *)GetModuleHandleA(NULL) + rva;
    if (!safe_readable(target, 2)) {
        probe_log("gate patch: target %p not readable - skipped", target);
        return;
    }
    expect[0] = target[0];
    if (expect[0] != 0x76 && expect[0] != 0x75) {
        probe_log("gate patch: unexpected opcode %02x at rva 0x%lx - skipped", expect[0], rva);
        return;
    }
    gate_write(rva, expect, repl, 1, 2);
}

/* 0F 85 rel32 ("jne success") -> E9 rel32 + NOP : always jump to success */
static void gate_force_jmp32(unsigned long rva, unsigned long target_rva)
{
    unsigned char expect[2] = {0x0f, 0x85};
    unsigned char repl[6];
    long long rel = (long long)target_rva - (long long)(rva + 5);
    int rel32 = (int)rel;
    repl[0] = 0xe9;
    memcpy(repl + 1, &rel32, 4);
    repl[5] = 0x90;
    gate_write(rva, expect, repl, 2, 6);
}

/* v62 (2026-11-04): RV store-readiness gate.
 *
 * FUN_14119c380 (the RV core routine) calls FUN_140edfe30, the SDK's
 * "is my store live" check:
 *      sess = *(handle+0x10);
 *      ready = sess->f58 && sess->f59
 *              && *(u64*)(*(sess+0x20)+0x20) != 0
 *              && now_ms <= *(u64*)(sess+0x50);
 * In this environment that store is built with f58/f59 = 0 and the deadline
 * left uninitialised (0xbaadf00d...), and NOTHING ever writes them (hardware
 * write watchpoints in cdb: zero hits in 100 s; the store-apply path
 * FUN_140f4d940 / FUN_140f3b120 / FUN_140f50500 is never entered). So the
 * check always fails, FUN_14119c380 raises component "Core" code 4, the RV
 * login aborts and the game falls back to OFFLINE MODE (which is what gates
 * ARK/online).
 *
 * Proven in cdb 16:32: poking those three fields ready at the RV call site
 * makes the RV layer proceed and the game prints
 *      Storm Online state changed: Storm Ready: 1 / Ubiservices: 1
 * for the first time ever (previously Ubiservices: 0, RVREPORT "Core" 4).
 *
 * This patch makes the same outcome permanent by forcing the success branch
 * only at the RV call site (75 0D -> EB 0D at rva 0x119c438), leaving the
 * game's own online-state polls untouched. UPC_RVSTORE=0 disables it.
 */
static int g_rvstore_logged = 0;
static int g_connect_wait_store = 0;

/* Replacement for FUN_140edfe30: mark the store live, then report "ready".
 * The original returns 0 unless store+0x58 && store+0x59 && a live deadline at
 * store+0x50 && *(u64*)(*(store+0x20)+0x20) != 0 -- none of which this
 * environment ever gets, so the RV layer always aborts with "Core" 4.
 * Proven by cdb 16:32: poking exactly these fields at the RV call site makes
 * the game print "Ubiservices: 1" for the first time. */
static unsigned char rvstore_hook(void *handle)
{
    unsigned char *sess;
    unsigned long long *holder;

    if (!handle) return 1;
    sess = *(unsigned char **)((char *)handle + 0x10);
    if (sess && safe_readable(sess, 0x60)) {
        DWORD old = 0;
        if (VirtualProtect(sess + 0x50, 0x10, PAGE_READWRITE, &old)) {
            sess[0x58] = 1;
            sess[0x59] = 1;
            *(unsigned long long *)(sess + 0x50) = ~0ull;
            VirtualProtect(sess + 0x50, 0x10, old, &old);
        }
        holder = *(unsigned long long **)(sess + 0x20);
        if (holder && safe_readable(holder, 0x28)) {
            if (VirtualProtect((char *)holder + 0x20, 8, PAGE_READWRITE, &old)) {
                *(unsigned long long *)((char *)holder + 0x20) = 1;
                VirtualProtect((char *)holder + 0x20, 8, old, &old);
            }
        }
        if (!g_rvstore_logged) {
            g_rvstore_logged = 1;
            probe_log("rvstore: store marked live (f58=1 f59=1 deadline=max holder+0x20=1) at %p",
                      (void *)sess);
        }
    }
    return 1;
}

static void rvstore_patch_install(void)
{
    char dis[8] = {0};
    unsigned char *target;
    unsigned char patch[14];
    DWORD old = 0;
    unsigned long long hookaddr = (unsigned long long)(uintptr_t)rvstore_hook;

    if (GetEnvironmentVariableA("UPC_RVSTORE", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("rvstore: disabled (UPC_RVSTORE=0)");
        return;
    }
    target = hook_target("TestShift", 0xedfe30);
    if (!safe_readable(target, 16)) {
        probe_log("rvstore: target %p not readable - skipped", (void *)target);
        return;
    }
    if (!VirtualProtect(target, 16, PAGE_EXECUTE_READWRITE, &old)) {
        probe_log("rvstore: VirtualProtect failed (%lu) - skipped", GetLastError());
        return;
    }
    memset(patch, 0x90, sizeof(patch));
    patch[0] = 0xff; patch[1] = 0x25;              /* jmp qword ptr [rip+0] */
    *(unsigned int *)(patch + 2) = 0;
    *(unsigned long long *)(patch + 6) = hookaddr;
    memcpy(target, patch, sizeof(patch));
    VirtualProtect(target, 16, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    probe_log("rvstore: hooked FUN_140edfe30 at %p (rva 0xedfe30) -> always ready, "
              "store fields forced live", (void *)target);
}

/* v63: suppress the login error dialog.
 *
 * FUN_1400fd900 is the game's "error table -> modal dialog" mapper; ("LOGIN", 7)
 * is what produces "The connection to the server has been lost." and the jump to
 * OFFLINE MODE. With the offline fallback disabled (UPC_FORCEONLINE=1) the game
 * otherwise sits on that dialog for ever, so a diagnostic run needs it gone.
 * Patching the mapper entry to a single `ret` keeps every other code path intact
 * and simply means "no error dialog is ever shown".
 * Enable with UPC_NOMODAL=1 (off by default).
 */
static void nomodal_patch_install(void)
{
    char dis[8] = {0};
    static const unsigned char expect[1] = { 0x48 };   /* 48 89 5c 24 08 ... */
    static const unsigned char repl[1]   = { 0xc3 };   /* ret                 */

    if (GetEnvironmentVariableA("UPC_NOMODAL", dis, sizeof(dis)) == 0 || dis[0] != '1') {
        probe_log("nomodal patch: off (UPC_NOMODAL=1 enables)");
        return;
    }
    if (gate_write(0xfd900, expect, repl, 1, 1))
        probe_log("nomodal patch: FUN_1400fd900 (error modal mapper) -> ret");
    else
        probe_log("nomodal patch: bytes mismatch at rva 0xfd900 - skipped");
}

/* v64 (diagnostic): neutralise the freed-object refcount that kills the
 * conquest/websocket path.
 *
 * At rva 0xE7D03D the SDK does `lock inc dword ptr [rdx+8]` on an object that
 * has already been freed (rdx = baadf00d...), which is the long-known
 * 0xE7D03D access violation. With the startup conquest connect suppressed the
 * crash never happens - but the notification websocket is only opened *by*
 * that conquest path, so suppressing it costs us the SDK connection we need
 * for the RV login. This patch turns the refcount increment into a no-op so
 * the path can survive long enough to be observed.
 * Enable with UPC_NOE7D=1 (diagnostic; off by default).
 */
static void noe7d_patch_install(void)
{
    char dis[8] = {0};
    static const unsigned char expect[4] = { 0xf0, 0xff, 0x42, 0x08 };  /* lock inc [rdx+8] */
    static const unsigned char repl[4]   = { 0x90, 0x90, 0x90, 0x90 };

    if (GetEnvironmentVariableA("UPC_NOE7D", dis, sizeof(dis)) == 0 || dis[0] != '1') {
        probe_log("noe7d patch: off (UPC_NOE7D=1 enables)");
        return;
    }
    if (gate_write(0xe7d03d, expect, repl, 4, 4))
        probe_log("noe7d patch: freed-object refcount at rva 0xe7d03d -> nops");
    else
        probe_log("noe7d patch: bytes mismatch at rva 0xe7d03d - skipped");
}

/* ---------------- RV step-sequence probe (v67) ----------------
 * FUN_1413605a0 is rdv::StepSequenceJob's step runner: called once per step
 * invocation with rcx = the job. We log the step index (job+0x40), the job
 * state (job+0x38; 4 = pending) and the job vtable, so the *live* run - where
 * the SDK websocket is connected - shows which step the RV login sits in.
 * The debugger cannot see this configuration (see PROTOCOL-NOTES 44), which is
 * exactly why this lives in the stub.
 * UPC_RVSTEPS=0 disables. */
#define RVSTEP_RVA   0x13605a0u
/* v69: 16 bytes = exactly the first five instructions of the prologue
 *   40 53             push rbx
 *   48 83 ec 20       sub  rsp,0x20
 *   48 8b 02          mov  rax,[rdx]
 *   48 8b d9          mov  rbx,rcx
 *   48 89 41 48       mov  [rcx+0x48],rax
 * (6 was used before, which produced a corrupt thunk - see hook_install). */
#define RVSTEP_COPY  16u

/* v74: the target is StepSequenceJob::Run(job, step_descriptor) - rdx is live
 * on entry (the prologue dereferences it: "mov rax,[rdx]"). The v69-v73 hook
 * only forwarded rcx, so the trampoline faulted reading [rdx]=0 and the game
 * died in its catch-all (_exit(1)) the moment the login sequence started.
 * That crash was OURS, not the game's. Forward both arguments. */
typedef void (__cdecl *rvstep_fn)(void *job, void *step);

static unsigned char *g_rvstep_tramp = NULL;
static unsigned long g_rvstep_n = 0;
static int g_rvstep_on = 1;

__attribute__((noinline))
static void __cdecl rvstep_hook(void *job, void *step_arg)
{
    unsigned char *j = (unsigned char *)job;
    char line[224];
    g_rvstep_n++;
    /* v69: log EVERY transition while the sequence is short enough to read (a
     * login sequence has a handful of steps), then thin out. Also dump the
     * current step object (job+0x48) - the step descriptor is what tells us
     * WHERE in the login the sequence sits. */
    if (g_rvstep_n <= 120 || (g_rvstep_n % 60) == 0) {
        int idx = (j && safe_readable(j, 0x50)) ? *(int *)(j + 0x40) : -1;
        int st  = (j && safe_readable(j, 0x50)) ? *(int *)(j + 0x38) : -1;
        void *vt = (j && safe_readable(j, 0x10)) ? *(void **)j : NULL;
        void *step = (j && safe_readable(j, 0x50)) ? *(void **)(j + 0x48) : NULL;
        unsigned long long s0 = 0, s1 = 0, s2 = 0, s3 = 0;
        if (step && safe_readable(step, 0x20)) {
            s0 = *(unsigned long long *)((char *)step + 0x00);
            s1 = *(unsigned long long *)((char *)step + 0x08);
            s2 = *(unsigned long long *)((char *)step + 0x10);
            s3 = *(unsigned long long *)((char *)step + 0x18);
        }
        snprintf(line, sizeof(line),
                 "rvstep[%lu]: idx=%d state=%d job=%p vt=%p step=%p [%llx %llx %llx %llx]",
                 (unsigned long)g_rvstep_n, idx, st, job, vt, step, s0, s1, s2, s3);
        probe_log("%s", line);
    }
    ((rvstep_fn)g_rvstep_tramp)(job, step_arg);
}

/* ---------------- MethodCallJob delegate trace (v73) ----------------
 * The login step job (vtable rva 0x16472d0 = MethodCallJob<CallContextRegister>)
 * dispatches through FUN_141354300:
 *     movups xmm1,[rcx+0x60]      ; 16-byte delegate {fn, self}
 *     mov rax,xmm1                ; fn
 *     add  rcx,[rdx+0x70]         ; this = base + (int)(dword [rcx+0x6c])
 *     jmp  rax                    ; <- AV when fn is wrong
 * The live crash is a jump to a garbage address from here, so log the whole
 * delegate + the job header. UPC_MCJOB=0 disables. */
#define MCJOB_RVA  0x1354300u
#define MCJOB_COPY 16u

typedef void (__cdecl *mcjob_fn)(void *job);

static unsigned char *g_mcjob_tramp = NULL;
static unsigned long g_mcjob_n = 0;

__attribute__((noinline))
static void __cdecl mcjob_hook(void *job)
{
    unsigned char *j = (unsigned char *)job;
    g_mcjob_n++;
    if (g_mcjob_n <= 40 && j && safe_readable(j, 0x80)) {
        probe_log("mcjob[%lu]: job=%p vt=%p fn=%p self=%p delta=%d base=%p op=%d "
                  "hdr=[%p %p %p %p]",
                  (unsigned long)g_mcjob_n, job, *(void **)j,
                  *(void **)(j + 0x60), *(void **)(j + 0x68),
                  *(int *)(j + 0x6c), *(void **)(j + 0x70), *(int *)(j + 0x78),
                  *(void **)(j + 0x20), *(void **)(j + 0x28),
                  *(void **)(j + 0x30), *(void **)(j + 0x38));
    }
    ((mcjob_fn)g_mcjob_tramp)(job);
}

static void mcjob_probe_install(void)
{
    static const unsigned char expect[MCJOB_COPY] = {
        0x0f, 0x10, 0x49, 0x60,             /* movups xmm1,[rcx+0x60] */
        0x48, 0x8b, 0xd1,                   /* mov    rdx,rcx         */
        0x66, 0x0f, 0x6f, 0xc1,             /* movdqa xmm0,xmm1       */
        0x66, 0x0f, 0x73, 0xd8, 0x08        /* psrldq xmm0,8          */
    };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_MCJOB", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("mcjob probe: off (UPC_MCJOB=0)");
        return;
    }
    hook_install("mcjob probe", MCJOB_RVA, expect, MCJOB_COPY,
                 &g_mcjob_tramp, (void *)mcjob_hook);
}

/* ---------------- RV request submit trace (v77) ----------------
 * FUN_14134c2d0(self, ctx, url_string, ...) is what a login step calls to
 * submit its async request. It bails out immediately (return 0, no request at
 * all) when the url std::string is EMPTY:
 *     if (FUN_141199220(url) == 0) return 0;
 * and then FUN_14134c420(self,ctx) must hold, else return 0 again.
 * Logging both tells us why the RV login dies in ~1 ms without any socket.
 * UPC_RVSUBMIT=0 disables. */
#define RVSUBMIT_RVA 0x134c2d0u
#define RVSUBMIT_COPY 15u

typedef unsigned char (__cdecl *rvsubmit_fn)(void *self, void *ctx, void *url, void *a4,
                                             unsigned short a5, void *a6, unsigned long long a7,
                                             void *a8);
static unsigned char *g_rvsubmit_tramp = NULL;
static int g_rvsubmit_n = 0;

__attribute__((noinline))
static unsigned char __cdecl rvsubmit_hook(void *self, void *ctx, void *url, void *a4,
                                           unsigned short a5, void *a6, unsigned long long a7,
                                           void *a8)
{
    char txt[200];
    txt[0] = 0;
    if (url && safe_readable(url, 0x20)) {
        unsigned long long *o = (unsigned long long *)url;
        unsigned long long len = o[1];
        const char *sp = NULL;
        if (len < 16 && safe_readable((char *)url + 16, 16)) sp = (char *)url + 16;
        else if (safe_readable((void *)(uintptr_t)o[0], 190)) sp = (const char *)(uintptr_t)o[0];
        if (sp) {
            size_t k = 0;
            while (k < 180 && sp[k]) { txt[k] = sp[k]; k++; }
            txt[k] = 0;
        }
    }
    {
        unsigned char rv = ((rvsubmit_fn)g_rvsubmit_tramp)(self, ctx, url, a4, a5, a6, a7, a8);
        if (g_rvsubmit_n < 30) {
            g_rvsubmit_n++;
            probe_log("rvsubmit[%d]: self=%p ctx=%p len=%llu rc=%u url=\"%s\"",
                      g_rvsubmit_n, self, ctx,
                      (url && safe_readable(url, 0x10)) ? *(unsigned long long *)((char *)url + 8) : 0,
                      (unsigned)rv, txt);
        }
        return rv;
    }
}

/* ---------------- login-job teardown probe (v120, PN 138) -----------------
 * FUN_141348f70 is the login job's release/teardown: it early-outs on
 * byte[job+0x3c8], sets that byte to 1, then aborts the endpoint, clears
 * op+0x48 and cancels the job's CallContext. Live traces show it running ~1 ms
 * BEFORE the client faults at 0x1362214 (rdv::WebSocketEndPoint delivering a
 * ws-close event to a NULL operation callback at endpoint+0x78), i.e. the crash
 * is a CONSEQUENCE of the teardown, and the teardown is a consequence of
 * whatever ended the login job. So: log who calls it and with which job state.
 * Prologue (15 bytes, ends on an instruction boundary):
 *     53              push rbx
 *   4883ec20          sub rsp,0x20
 *   80b9c803000000    cmp byte [rcx+0x3c8],0
 */
#define JOBEND_RVA 0x1348f70u
#define JOBEND_COPY 16
static unsigned char *g_jobend_tramp = NULL;
static int g_jobend_n = 0;

typedef void (__cdecl *jobend_fn)(void *job);

__attribute__((noinline))
static void __cdecl jobend_hook(void *job)
{
    int n = ++g_jobend_n;
    if (n <= 40) {
        unsigned f3c8 = 0, a4 = 0, f1f0 = 0, d0 = 0, f248 = 0, f88 = 0;
        char bt[600];
        void *frames[16];
        unsigned nf = bt_capture(frames, 16, 1);
        bt_format(frames, nf, bt, sizeof(bt));
        if (job && safe_readable(job, 0x3d0)) {
            f3c8 = *(unsigned char *)((char *)job + 0x3c8);
            a4   = *(unsigned int *)((char *)job + 0xa4);
            f1f0 = (unsigned)*(unsigned char *)((char *)job + 0x1f0);
            f88  = *(unsigned int *)((char *)job + 0x88);
            d0   = (unsigned)(uintptr_t)(*(void **)((char *)job + 0xd0) != 0);
            if (safe_readable(*(void **)((char *)job + 0x248), 8))
                f248 = (unsigned)(*(void **)((char *)job + 0x248) != 0);
        }
        probe_log("jobend[%d]: job=%p flag3c8=%u a4=%u a88=%u comp_d0=%u str248=%u f1f0=%u stack:%s",
                  n, job, f3c8, a4, f88, d0, f248, f1f0, bt);
    }
    ((jobend_fn)g_jobend_tramp)(job);
}

static void jobend_probe_install(void)
{
    static const unsigned char expect[JOBEND_COPY] = {
        0x40, 0x53,                               /* push rbx (REX 40) */
        0x48, 0x83, 0xec, 0x20,                   /* sub rsp,0x20      */
        0x80, 0xb9, 0xc8, 0x03, 0x00, 0x00, 0x00,  /* cmp [rcx+0x3c8],0 */
        0x48, 0x8b, 0xd9                    /* mov rbx,rcx       */
    };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_JOBEND", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("jobend probe: off (UPC_JOBEND=0)");
        return;
    }
    hook_install("jobend probe", JOBEND_RVA, expect, JOBEND_COPY,
                 &g_jobend_tramp, (void *)jobend_hook);
}

/* ---------------- RV login entry trace (v78) ----------------
 * FUN_14119c380(self, callctx, a3, a4, u16 a5, std::string *url, char a7)
 * is the RV "login" entry. When the caller's url string is EMPTY it is
 * derived from the store:
 *     global = FUN_140ecfa60();
 *     view   = FUN_140eda820(global, tmp);      // shared_ptr wrapper
 *     obj    = FUN_140edc340(view) = *(view+0x10) + 0x20
 *     str    = FUN_140edda40(obj);
 *     FUN_141198b50(url, str);                  // fill the caller's string
 * We log the string before and after, plus the store handle, so we can see
 * whether the derived url is empty (store has no content) or not.
 * UPC_RVLOGIN=0 disables. */
#define RVLOGIN_RVA 0x119c380u
#define RVLOGIN_COPY 14u

typedef unsigned char (__cdecl *rvlogin_fn)(void *self, void *callctx, void *a3, void *a4,
                                            unsigned short a5, void *url, char a7);
static unsigned char *g_rvlogin_tramp = NULL;
static int g_rvlogin_n = 0;

static void scan_markers(const void *base, unsigned long long span, const char *tag);

static void str_peek(const void *sp, char *out, int cap)
{
    unsigned long long *o = (unsigned long long *)sp;
    unsigned long long len;
    const char *p = NULL;
    int k = 0;
    out[0] = 0;
    if (!sp || !safe_readable(sp, 0x20)) return;
    len = o[1];
    if (len < 16 && safe_readable((const char *)sp + 16, 16)) p = (const char *)sp + 16;
    else if (safe_readable((const void *)(uintptr_t)o[0], cap)) p = (const char *)(uintptr_t)o[0];
    if (!p) return;
    while (k < cap - 1 && p[k]) { out[k] = p[k]; k++; }
    out[k] = 0;
}

__attribute__((noinline))
static unsigned char __cdecl rvlogin_hook(void *self, void *callctx, void *a3, void *a4,
                                          unsigned short a5, void *url, char a7)
{
    char before[220], after[220];
    unsigned long long store = 0, s0 = 0, s10 = 0, s20 = 0, s30 = 0;
    unsigned char rv;
    str_peek(url, before, sizeof(before));
    /* v80: EXPERIMENT. The SDK derives the RV login url from the (empty)
     * primary store, so the login never submits anything. Optionally plant our
     * own station URL into that string with the game's own std::string ctor
     * (FUN_141198920) - the same "poke the field, prove the path" trick that
     * produced the v62 store breakthrough. Opt-in: UPC_RVURLPOKE=<url>. */
    if (!before[0]) {
        static const char *poke = NULL;
        static int poke_checked = 0;
        if (!poke_checked) {
            static char env[256];
            poke_checked = 1;
            if (GetEnvironmentVariableA("UPC_RVURLPOKE", env, sizeof(env)) > 0 && env[0])
                poke = env;
        }
        if (poke && url && safe_readable(url, 0x20)) {
            typedef void (__cdecl *str_ctor_fn)(void *self, const char *cstr);
            str_ctor_fn ctor = (str_ctor_fn)(uintptr_t)((unsigned char *)GetModuleHandleA(NULL) + 0x1198920u);
            ctor(url, poke);
            str_peek(url, before, sizeof(before));
            probe_log("rvlogin: planted url \"%s\"", before);
        }
    }
    if (self && safe_readable(self, 0x40)) {
        store = *(unsigned long long *)((char *)self + 0x18);
        if (store && safe_readable((const void *)(uintptr_t)store, 0x38)) {
            s0  = *(unsigned long long *)(uintptr_t)store;
            s10 = *(unsigned long long *)(uintptr_t)(store + 0x10);
            s20 = *(unsigned long long *)(uintptr_t)(store + 0x20);
            s30 = *(unsigned long long *)(uintptr_t)(store + 0x30);
        }
    }
    rv = ((rvlogin_fn)g_rvlogin_tramp)(self, callctx, a3, a4, a5, url, a7);
    str_peek(url, after, sizeof(after));
    if (g_rvlogin_n < 20) {
        if (store) scan_markers((const void *)(uintptr_t)store, 0x4000, "rvlogin store");
        if (self)  scan_markers(self, 0x2000, "rvlogin self");
        g_rvlogin_n++;
        probe_log("rvlogin[%d]: self=%p store=%p [%llx %llx %llx %llx] a5=%u a7=%d rc=%u "
                  "url_before=\"%s\" url_after=\"%s\"",
                  g_rvlogin_n, self, (void *)(uintptr_t)store, s0, s10, s20, s30,
                  (unsigned)a5, (int)a7, (unsigned)rv, before, after);
    }
    return rv;
}

/* Scan a memory region for marker strings; used to find out whether the RV
 * url data reached the object at all (and at which offset). */
static void scan_markers(const void *base, unsigned long long span, const char *tag)
{
    const unsigned char *p = (const unsigned char *)base;
    unsigned long long i;
    int hits = 0;
    if (!base || !span) return;
    for (i = 0; i + 32 < span && hits < 6; i += 4) {
        if ((i & 0xfff) == 0 && !safe_readable(p + i, 0x1000)) continue;
        if (!safe_readable(p + i, 8)) continue;
        if (memcmp(p + i, "prudp", 5) == 0 || memcmp(p + i, "http://127", 10) == 0 ||
            memcmp(p + i, "rvRendezVous", 12) == 0) {
            char txt[120];
            int k = 0;
            while (k < 110 && safe_readable(p + i + k, 1) && p[i + k] >= 0x20 && p[i + k] < 0x7f) {
                txt[k] = (char)p[i + k];
                k++;
            }
            txt[k] = 0;
            probe_log("%s: marker at +0x%llx \"%s\"", tag, i, txt);
            hits++;
        }
    }
    if (!hits) probe_log("%s: no prudp/http/rendezvous marker in the first %llu bytes",
                         tag, span);
}

/* ---------------- SDK connection-job trace (v82) ----------------
 * The SDK's own "connection" job is what turns the configured URLs into real
 * endpoints (and therefore what would make the client dial). Two entry points
 * from the earlier debugger work:
 *   FUN_140eb3660  JobInitiateConnection::createUrl
 *   FUN_140f45560  JobInitWebsocket step
 * Logging whether they are reached at all decides the next investigation:
 * never reached -> the job is not scheduled (switch/session data missing);
 * reached       -> the failure is inside the URL/endpoint handling.
 * UPC_SDKJOBS=0 disables. */
#define SDKCONN_RVA 0xeb3660u
#define SDKWS_RVA   0xf45560u
#define SDKJOB_COPY 15u

typedef void *(__cdecl *sdkjob_fn)(void *, void *, void *, void *);
static unsigned char *g_sdkconn_tramp = NULL;
static unsigned char *g_sdkws_tramp = NULL;
static int g_sdkconn_n = 0, g_sdkws_n = 0;

static void sdkjob_log(const char *tag, int *counter, void *a, void *b, void *c)
{
    char sb[160], sc[160];
    int n = ++(*counter);
    if (n > 12) return;
    str_peek(b, sb, sizeof(sb));
    str_peek(c, sc, sizeof(sc));
    probe_log("%s[%d]: a=%p b=%p(\"%s\") c=%p(\"%s\")", tag, n, a, b, sb, c, sc);
}

__attribute__((noinline))
static void *__cdecl sdkconn_hook(void *a, void *b, void *c, void *d)
{
    sdkjob_log("sdkconn", &g_sdkconn_n, a, b, c);
    return ((sdkjob_fn)g_sdkconn_tramp)(a, b, c, d);
}

__attribute__((noinline))
static void *__cdecl sdkws_hook(void *a, void *b, void *c, void *d)
{
    sdkjob_log("sdkws", &g_sdkws_n, a, b, c);
    return ((sdkjob_fn)g_sdkws_tramp)(a, b, c, d);
}

/* ---------------- SDK parameter-key trace (v83) ----------------
 * FUN_140e97780(map, out, key) is the SDK's member lookup (verified by every
 * call site: rcx = the map object, rdx = the destination, r8 = a key string).
 * Logging the key names tells us exactly which configuration entries the client
 * asks for - in particular whether it ever asks for the sandbox/connection
 * entries that would schedule JobInitiateConnection (PN 57).
 * UPC_PARAMKEY=0 disables. */
#define PARAMKEY_RVA 0xe97780u
#define PARAMKEY_COPY 21u

typedef void *(__cdecl *paramkey_fn)(void *map, void *out, const char *key);
static unsigned char *g_paramkey_tramp = NULL;
static int g_paramkey_n = 0;

__attribute__((noinline))
static void *__cdecl paramkey_hook(void *map, void *out, const char *key)
{
    if (g_paramkey_n < 400 && key && safe_readable(key, 1)) {
        char k[96];
        int i = 0;
        while (i < 90 && safe_readable(key + i, 1) && key[i]) { k[i] = key[i]; i++; }
        k[i] = 0;
        g_paramkey_n++;
        probe_log("paramkey[%d]: map=%p key=\"%s\"", g_paramkey_n, map, k);
    }
    return ((paramkey_fn)g_paramkey_tramp)(map, out, key);
}

/* ---------------- rendezvous-configuration URL builder (v84) ----------------
 * FUN_141348a80 builds the request URL for the rendezvous configuration:
 *     GetParamString("sandboxes") + sandboxId([r15+0x448]) + "/rendezvous/configuration"
 * (found with tools/strref.py at the only `lea r8,'/rendezvous/configuration'`
 * site in the exe). If this function fires, the RV component IS trying to fetch
 * the config and the failure is downstream (submit/response); if it never
 * fires, nothing ever asks the RV component for endpoints at all.
 * UPC_RVCFGURL=0 disables. */
#define RVCFGURL_RVA 0x1348a80u
#define RVCFGURL_COPY 16u

typedef void *(__cdecl *rvcfgurl_fn)(void *a, void *b, void *c);
static unsigned char *g_rvcfgurl_tramp = NULL;
static int g_rvcfgurl_n = 0;

__attribute__((noinline))
static void *__cdecl rvcfgurl_hook(void *a, void *b, void *c)
{
    if (g_rvcfgurl_n < 20) {
        g_rvcfgurl_n++;
        probe_log("rvcfgurl[%d]: a=%p b=%p c=%p", g_rvcfgurl_n, a, b, c);
    }
    return ((rvcfgurl_fn)g_rvcfgurl_tramp)(a, b, c);
}

/* ---------------- game-side online-login entry (v85) ----------------
 * FUN_140d92cf0 is the Anno-side "online login" (a vtable method of the
 * RV client wrapper, table at 0x171ede8). It takes the server address as a
 * WIDE STRING in the 4th argument, converts it (FUN_140d8ddc0) and hands it to
 * the RV login. It is empty in every run, which is why the RV login has no url
 * and therefore submits nothing. Logging the value plus the call stack tells us
 * which game code supplies it - and therefore which data file can point it at
 * our PRUDP responder.
 * UPC_RVLOGIN2=0 disables. */
#define RVLOGIN2_RVA 0xd92cf0u
#define RVLOGIN2_COPY 20u

typedef void *(__cdecl *rvlogin2_fn)(void *a, void *b, void *c, const wchar_t *wide, int flag);
static unsigned char *g_rvlogin2_tramp = NULL;
static int g_rvlogin2_n = 0;

__attribute__((noinline))
static void *__cdecl rvlogin2_hook(void *a, void *b, void *c, const wchar_t *wide, int flag)
{
    if (g_rvlogin2_n < 10) {
        char w[200];
        void *frames[16];
        unsigned nf;
        int i = 0;
        w[0] = 0;
        if (wide) {
            while (i < 190 && wide[i]) { w[i] = (wide[i] < 128) ? (char)wide[i] : '?'; i++; }
            w[i] = 0;
        }
        g_rvlogin2_n++;
        nf = bt_capture(frames, 16, 1);
        {
            char st[420];
            bt_format(frames, nf, st, sizeof(st));
            probe_log("rvlogin2[%d]: wide=\"%s\" flag=%d a=%p b=%p c=%p stack:%s",
                      g_rvlogin2_n, w, flag, a, b, c, st);
        }
    }
    return ((rvlogin2_fn)g_rvlogin2_tramp)(a, b, c, wide, flag);
}

/* ---------------- ConnectionManager method trace (v88) ----------------
 * rdv::ConnectionManager lives at vtable rva 0x1648e90; its three methods in the
 * 0x136xxxx job-code region (0x1360e00, 0x1361020, 0x13612c0) are the candidates
 * for "initiate/attach connection" - the piece that leaves
 * ClientProtocolRequestBroker[+0x28] null and produces RV error 133 (PN 67/68).
 * Logging whether they run tells us if the manager is active at all.
 * UPC_CONNMGR=0 disables. */
static int g_cmgr_n[3] = {0, 0, 0};

static void cmgr_log(int which, void *self, void *a, void *b)
{
    if (g_cmgr_n[which] < 8) {
        g_cmgr_n[which]++;
        probe_log("connmgr[%d] m%d: self=%p a=%p b=%p", g_cmgr_n[which], which, self, a, b);
    }
}

typedef void *(__cdecl *cmgr_fn)(void *, void *, void *, void *);
static unsigned char *g_cmgr0_tramp = NULL, *g_cmgr1_tramp = NULL, *g_cmgr2_tramp = NULL;

__attribute__((noinline)) static void *__cdecl cmgr0_hook(void *s_, void *a, void *b, void *c)
{ cmgr_log(0, s_, a, b); return ((cmgr_fn)g_cmgr0_tramp)(s_, a, b, c); }
__attribute__((noinline)) static void *__cdecl cmgr1_hook(void *s_, void *a, void *b, void *c)
{ cmgr_log(1, s_, a, b); return ((cmgr_fn)g_cmgr1_tramp)(s_, a, b, c); }
__attribute__((noinline)) static void *__cdecl cmgr2_hook(void *s_, void *a, void *b, void *c)
{ cmgr_log(2, s_, a, b); return ((cmgr_fn)g_cmgr2_tramp)(s_, a, b, c); }

/* ---------------- skip the online login (v89) ----------------
 * FUN_140d92cf0 is the game-side online-login entry that hands the RV layer the
 * server address. It cannot succeed today (no RV connection object, PN 66/69),
 * and its failure is what puts the blocking "The connection to the server has
 * been lost." modal in front of the player at every boot (PN 65).
 * UPC_NOLOGIN=1 turns the entry into `mov eax,1; ret`, i.e. "report success
 * without doing anything", to see whether the game then boots *cleanly* into
 * offline mode. Diagnostic only - it does not make anything online work.
 * UPC_NOLOGIN=0 disables. */
#define NOLOGIN_RVA 0xd92cf0u
static void nologin_patch_install(void)
{
    static const unsigned char expect[6] = { 0x48, 0x89, 0x5c, 0x24, 0x08, 0x48 };
    unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
    unsigned char *p;
    DWORD old = 0;
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_NOLOGIN", dis, sizeof(dis)) == 0 || dis[0] != '1') return;
    if (!base) return;
    p = base + NOLOGIN_RVA;
    if (!safe_readable(p, 6)) { probe_log("nologin: target unreadable"); return; }
    if (memcmp(p, expect, 6) != 0) { probe_log("nologin: prologue mismatch at %p", p); return; }
    if (!VirtualProtect(p, 8, PAGE_EXECUTE_READWRITE, &old)) return;
    p[0] = 0xb8; p[1] = 0x01; p[2] = 0x00; p[3] = 0x00; p[4] = 0x00;  /* mov eax,1 */
    p[5] = 0xc3;                                                      /* ret       */
    p[6] = 0x90; p[7] = 0x90;
    VirtualProtect(p, 8, old, &old);
    FlushInstructionCache(GetCurrentProcess(), p, 8);
    probe_log("nologin: FUN_140d92cf0 -> mov eax,1; ret (online login skipped)");
}

/* ---------------- WebSocketStream pump trace (v91) ----------------
 * rdv::WebSocketStream (vtable 0x1649060) is the RV websocket transport. Two of
 * its methods can be hooked safely:
 *   0x1361710  big method (prologue ends at 15 bytes)
 *   0x1362590  the pump step (14 bytes: sub rsp / mov rcx,rdx / call / mov al,1)
 * Logging them tells us whether the client even pumps inbound data on the
 * socket we answer - the reply-shape work (PN 76-81) is exhausted, so this is
 * the next measurement: if the pump never runs after our frame, our bytes are
 * not reaching the stream at all.
 * UPC_WSSTREAM=1 enables (default off - hot path). */
static int g_wss_n[2] = {0, 0};

static void wss_log(int which, void *a, void *b)
{
    if (g_wss_n[which] < 40) {
        g_wss_n[which]++;
        probe_log("wsstream[%d] m%d: a=%p b=%p", g_wss_n[which], which, a, b);
    }
}

typedef void *(__cdecl *wss_fn)(void *, void *);
static unsigned char *g_wss0_tramp = NULL, *g_wss1_tramp = NULL;

__attribute__((noinline)) static void *__cdecl wss0_hook(void *a, void *b)
{ wss_log(0, a, b); return ((wss_fn)g_wss0_tramp)(a, b); }
__attribute__((noinline)) static void *__cdecl wss1_hook(void *a, void *b)
{ wss_log(1, a, b); return ((wss_fn)g_wss1_tramp)(a, b); }

static unsigned char *g_wss2_tramp = NULL, *g_wss3_tramp = NULL;

static void wss_bt(const char *tag, void *a, void *b)
{
    static int n = 0;
    if (n < 30) {
        void *frames[12];
        unsigned nf;
        char st[320];
        n++;
        nf = bt_capture(frames, 12, 1);
        bt_format(frames, nf, st, sizeof(st));
        probe_log("%s[%d]: a=%p b=%p stack:%s", tag, n, a, b, st);
    }
}

__attribute__((noinline)) static void *__cdecl wss2_hook(void *a, void *b, void *c)
{ wss_bt("wsstream send0", a, b);
  return ((void *(__cdecl *)(void *, void *, void *))g_wss2_tramp)(a, b, c); }
static unsigned char *g_wss4_tramp = NULL;

__attribute__((noinline)) static void *__cdecl wss4_hook(void *a, void *b)
{ wss_bt("wsstream send2", a, b); return ((wss_fn)g_wss4_tramp)(a, b); }

__attribute__((noinline)) static void *__cdecl wss3_hook(void *a)
{ wss_bt("wsstream send1", a, NULL);
  return ((void *(__cdecl *)(void *))g_wss3_tramp)(a); }

/* ---------------- RMC inbound dispatch trace (v92) ----------------
 * FUN_14136ea20(self, name, msg, out, char *handled, ctx) is
 * rdv::ClientProtocolRequestBroker's receive/dispatch. It looks the protocol up
 * by name and, if found, deserialises the message (FUN_14135ef70); on the
 * failure branch it sets *handled = 1 and records a "Core" error, on success it
 * calls the protocol and then leaves *handled at 0/1 depending on the parse.
 * Reading *handled after the call tells us exactly which branch an inbound frame
 * took - the one measurement PN 84 asked for.
 * UPC_RMCDISP=0 disables. */
#define RMCDISP_RVA 0x136ea20u
#define RMCDISP_COPY 15u

typedef void (__cdecl *rmcdisp_fn)(void *self, void *name, void *msg, void *out,
                                   char *handled, void *ctx);
static unsigned char *g_rmcdisp_tramp = NULL;
static int g_rmcdisp_n = 0;

__attribute__((noinline))
static void __cdecl rmcdisp_hook(void *self, void *name, void *msg, void *out,
                                 char *handled, void *ctx)
{
    char nm[120];
    unsigned char before = (handled && safe_readable(handled, 1)) ? (unsigned char)*handled : 0;
    str_peek(name, nm, sizeof(nm));
    ((rmcdisp_fn)g_rmcdisp_tramp)(self, name, msg, out, handled, ctx);
    if (g_rmcdisp_n < 40) {
        unsigned char after = (handled && safe_readable(handled, 1)) ? (unsigned char)*handled : 0;
        g_rmcdisp_n++;
        probe_log("rmcdisp[%d]: name=\"%s\" msg=%p before=%u after=%u", g_rmcdisp_n, nm, msg,
                  (unsigned)before, (unsigned)after);
    }
}

/* ---------------- RV send-flavour trace (v94) ----------------
 * FUN_141356700(self, u16, u8, byte flags, u16) is the RV layer's send dispatcher
 * (PN 86): `flags & 1` becomes the frame's flags byte and `flags & 2` selects the
 * send mode. Logging it for EVERY message tells us, in particular, which value
 * the client itself uses when it sends a *result/error* - the one datum the reply
 * experiments (PN 84) could not establish from our side.
 * UPC_RVSEND=0 disables. */
#define RVSEND_RVA 0x1356700u
#define RVSEND_COPY 15u

typedef void *(__cdecl *rvsend_fn)(void *self, unsigned short a2, unsigned char a3,
                                   unsigned char flags, unsigned short a5);
static unsigned char *g_rvsend_tramp = NULL;
static int g_rvsend_n = 0;

__attribute__((noinline))
static void *__cdecl rvsend_hook(void *self, unsigned short a2, unsigned char a3,
                                 unsigned char flags, unsigned short a5)
{
    if (g_rvsend_n < 30) {
        g_rvsend_n++;
        probe_log("rvsend[%d]: self=%p a2=%u a3=%u flags=0x%02x (call=%u mode=%u) a5=%u",
                  g_rvsend_n, self, (unsigned)a2, (unsigned)a3, (unsigned)flags,
                  (unsigned)(flags & 1), (unsigned)((flags >> 1) & 1), (unsigned)a5);
    }
    return ((rvsend_fn)g_rvsend_tramp)(self, a2, a3, flags, a5);
}

static void rvsend_probe_install(void)
{
    static const unsigned char expect[RVSEND_COPY] = {
        0x4c, 0x8b, 0xdc,                           /* mov r11,rsp      */
        0x49, 0x89, 0x5b, 0x10,                     /* mov [r11+0x10],rbx */
        0x49, 0x89, 0x6b, 0x18,                     /* mov [r11+0x18],rbp */
        0x49, 0x89, 0x73, 0x20                      /* mov [r11+0x20],rsi */
    };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_RVSEND", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("rvsend probe: off (UPC_RVSEND=0)");
        return;
    }
    hook_install("rvsend probe", RVSEND_RVA, expect, RVSEND_COPY,
                 &g_rvsend_tramp, (void *)rvsend_hook);
}

static void rmcdisp_probe_install(void)
{
    static const unsigned char expect[RMCDISP_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x08,
        0x48, 0x89, 0x6c, 0x24, 0x10,
        0x48, 0x89, 0x74, 0x24, 0x18
    };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_RMCDISP", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("rmcdisp probe: off (UPC_RMCDISP=0)");
        return;
    }
    hook_install("rmcdisp probe", RMCDISP_RVA, expect, RMCDISP_COPY,
                 &g_rmcdisp_tramp, (void *)rmcdisp_hook);
}

static void wsstream_probes_install(void)
{
    static const unsigned char e0[15] = {
        0x48, 0x89, 0x5c, 0x24, 0x08, 0x48, 0x89, 0x6c, 0x24, 0x10,
        0x48, 0x89, 0x74, 0x24, 0x18
    };
    static const unsigned char e1[14] = {
        0x48, 0x83, 0xec, 0x28, 0x48, 0x8b, 0xca, 0xe8, 0x44, 0xe7, 0xff, 0xff, 0xb0, 0x01
    };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_WSSTREAM", dis, sizeof(dis)) == 0 || dis[0] != '1') {
        probe_log("wsstream probes: off (UPC_WSSTREAM=1 enables)");
        return;
    }
    static const unsigned char e2[16] = {
        0x48, 0x89, 0x5c, 0x24, 0x08, 0x57, 0x48, 0x83, 0xec, 0x20,
        0x48, 0x8b, 0xc1, 0x49, 0x8b, 0xd8
    };
    static const unsigned char e3[16] = {
        0x40, 0x53, 0x48, 0x83, 0xec, 0x20, 0x48, 0x8b, 0xd9,
        0x48, 0x8b, 0x49, 0x18, 0x48, 0x81, 0xc1
    };
    hook_install("wsstream m0", 0x1361710u, e0, 15u, &g_wss0_tramp, (void *)wss0_hook);
    hook_install("wsstream m1", 0x1362590u, e1, 14u, &g_wss1_tramp, (void *)wss1_hook);
    /* v93: the stream's send wrappers. If the client's login call / NAK goes
     * through these, the backtrace names the code that drives the transport. */
    static const unsigned char e4[18] = {
        0x40, 0x53, 0x48, 0x83, 0xec, 0x20, 0x48, 0x8b, 0x01,
        0x48, 0x8b, 0xd9, 0xff, 0x90, 0xf8, 0x00, 0x00, 0x00
    };
    hook_install("wsstream send2", 0x13622a0u, e4, 18u, &g_wss4_tramp, (void *)wss4_hook);
    hook_install("wsstream send0", 0x1380e60u, e2, 16u, &g_wss2_tramp, (void *)wss2_hook);
    hook_install("wsstream send1", 0x1380ec0u, e3, 16u, &g_wss3_tramp, (void *)wss3_hook);
}

static void cmgr_probes_install(void)
{
    static const unsigned char e0[17] = {
        0x48, 0x89, 0x5c, 0x24, 0x08, 0x57, 0x48, 0x83, 0xec, 0x20,
        0x48, 0x8d, 0x05, 0x87, 0x80, 0x2e, 0x00
    };
    static const unsigned char e1[20] = {
        0x48, 0x89, 0x5c, 0x24, 0x08, 0x48, 0x89, 0x6c, 0x24, 0x10,
        0x48, 0x89, 0x74, 0x24, 0x18, 0x48, 0x89, 0x7c, 0x24, 0x20
    };
    static const unsigned char e2[15] = {
        0x48, 0x89, 0x5c, 0x24, 0x08, 0x57, 0x48, 0x83, 0xec, 0x40,
        0x48, 0x83, 0x79, 0x38, 0x00
    };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_CONNMGR", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("connmgr probes: off (UPC_CONNMGR=0)");
        return;
    }
    /* PN 194: THIS HOOK MUST STAY OFF. Its 17 displaced bytes contain
     * `lea rax,[rip+0x2e8087]` (the vtable load at 0x1360e0a) followed by
     * `mov [rcx],rax`. Copied into a VirtualAlloc'd trampoline, that lea
     * resolves against the TRAMPOLINE, so every boot stored a garbage vtable
     * pointer on the rdv::ConnectionManager object - a nondeterministic crash
     * waiting to happen. Probes must not change semantics; this one did. */
    if (GetEnvironmentVariableA("UPC_CONNMGR_M0", dis, sizeof(dis)) > 0 && dis[0] == '1') {
        hook_install("connmgr m0", 0x1360e00u, e0, 17u, &g_cmgr0_tramp, (void *)cmgr0_hook);
    } else {
        probe_log("connmgr m0 probe: off (displaced bytes hold a RIP-relative "
                  "lea - the hook corrupts the vtable; UPC_CONNMGR_M0=1 to force)");
    }
    hook_install("connmgr m1", 0x1361020u, e1, 20u, &g_cmgr1_tramp, (void *)cmgr1_hook);
    hook_install("connmgr m2", 0x13612c0u, e2, 15u, &g_cmgr2_tramp, (void *)cmgr2_hook);
}


/* ---------------- connection-initiation probes (v102, PN 110/111) ----------
 * After the (now working) RV login the client must open the game connection:
 *   JobBackEndServicesLogin step 0x1349480
 *     -> builds "prudp:/address=<host>;port=<port>" from job+0x250/+0x258
 *     -> transport selector job+0x25c (3 = websocket) / flag job+0x458
 *     -> FUN_14134fcf0 "checkRequirements" (bool)
 *        -> ws:    FUN_1413619f0
 *        -> prudp: FUN_141360ed0 -> JobInitiateConnection
 * These three probes say which branch is taken and why it stops.
 * UPC_CONNINIT=0 disables. */
typedef void *(__cdecl *ci_fn)(void *, void *, void *, void *);
typedef void *(__cdecl *ci5_fn)(void *, void *, void *, void *, void *);
typedef void *(__cdecl *ci8_fn)(void *, void *, void *, void *, void *, void *, void *, void *);

static unsigned char *g_besl_tramp = NULL;
static unsigned char *g_chkre_tramp = NULL;
static unsigned char *g_jinit_tramp = NULL;
static int g_besl_n = 0, g_chkre_n = 0, g_jinit_n = 0;

static void ci_str(void *p, char *out, int n)
{
    int i = 0;
    out[0] = 0;
    if (!p || !safe_readable(p, 1)) return;
    for (; i < n - 1; i++) {
        char c;
        if (!safe_readable((char *)p + i, 1)) break;
        c = ((char *)p)[i];
        if (!c) break;
        out[i] = (c >= 32 && c < 127) ? c : '.';
    }
    out[i] = 0;
}

__attribute__((noinline))
static void *__cdecl besl_hook(void *job, void *b, void *c, void *d)
{
    if (g_besl_n < 12 && job && safe_readable(job, 0x480)) {
        char host[48], url[96];
        unsigned char *p = (unsigned char *)job;
        unsigned sel = *(unsigned *)(p + 0x25c);
        unsigned port = *(unsigned short *)(p + 0x258);
        unsigned long long len = *(unsigned long long *)(p + 0x250 + 0x10);
        const char *hp;
        g_besl_n++;
        hp = (len >= 16) ? *(const char **)(p + 0x250) : (const char *)(p + 0x250);
        if (len > 60) len = 60;
        ci_str((void *)hp, host, (int)len + 1);
        ci_str((void *)(p + 0x3d0), url, 40);
        probe_log("besl step[%d]: job=%p sel(0x25c)=%u f280=%u f3c8=%u f458=%u "
                  "host=\"%s\" port=%u v3d0=\"%s\"",
                  g_besl_n, job, sel, p[0x280], p[0x3c8], p[0x458], host, port, url);
    }
    return ((ci_fn)g_besl_tramp)(job, b, c, d);
}

__attribute__((noinline))
static void *__cdecl chkre_hook(void *self, void *b, void *c, void *d)
{
    void *rv = ((ci_fn)g_chkre_tramp)(self, b, c, d);
    if (g_chkre_n < 12) {
        g_chkre_n++;
        probe_log("checkRequirements[%d]: self=%p -> %d", g_chkre_n, self, (int)(long long)rv);
    }
    return rv;
}

__attribute__((noinline))
static void *__cdecl jinit_hook(void *a, void *b, void *c, void *d,
                                void *e, void *f, void *g, void *h)
{
    void *rv = ((ci8_fn)g_jinit_tramp)(a, b, c, d, e, f, g, h);
    if (g_jinit_n < 12) {
        g_jinit_n++;
        probe_log("submitJobInitiateConnection[%d]: a=%p b=%p c=%p d=%p -> %d",
                  g_jinit_n, a, b, c, d, (int)(long long)rv);
    }
    return rv;
}


/* ---------------- mgwa (game-side online adapter) probes (v103, PN 112) -----
 * After a successful RV login the game's own layer (rd::mgwa::
 * CMetaGameWebAdapterImpl) is what should turn it into "online". Three probes:
 *   mgwacb  0x140d94200  the login completion callback (caller of the queue)
 *   mgwaq   0x140dcd9f0  pushes the completed CallContext onto mgwa+0x18
 *   mgwast  0x140d92cf0  the status/state handler (state at impl+4)
 * UPC_MGWA=0 disables. */
static unsigned char *g_mgwacb_tramp = NULL;
static unsigned char *g_mgwaq_tramp = NULL;
static unsigned char *g_mgwast_tramp = NULL;
static int g_mgwacb_n = 0, g_mgwaq_n = 0, g_mgwast_n = 0;
static void *g_mgwa_impl = NULL;   /* set on the first queue push (PN 117) */

/* PN 117: does anything ever CONSUME the completed-call queue? The mgwa keeps a
 * vector at impl+0x18 (begin) / +0x20 (end); a consumer pops/closes it and the
 * depth returns to 0. Watching the depth tells "nobody consumes it" apart from
 * "consumed, and the stall is later". */
static DWORD WINAPI mgwa_watch(LPVOID unused)
{
    int last = -1, i;
    (void)unused;
    for (i = 0; i < 320; i++) {
        void *impl = g_mgwa_impl;
        if (impl && safe_readable((char *)impl + 0x18, 0x10)) {
            unsigned long long b = *(unsigned long long *)((char *)impl + 0x18);
            unsigned long long e = *(unsigned long long *)((char *)impl + 0x20);
            int n = (e > b) ? (int)((e - b) / 8) : 0;
            if (n != last) {
                probe_log("mgwa queue depth=%d begin=%p end=%p", n,
                          (void *)(uintptr_t)b, (void *)(uintptr_t)e);
                last = n;
            }
        }
        Sleep(250);
    }
    return 0;
}

__attribute__((noinline))
static void *__cdecl mgwacb_hook(void *a, void *b, void *c, void *d)
{
    if (g_mgwacb_n < 20) {
        g_mgwacb_n++;
        probe_log("mgwacb[%d]: a=%p b=%p c=%p", g_mgwacb_n, a, b, c);
    }
    return ((ci_fn)g_mgwacb_tramp)(a, b, c, d);
}

static int g_mgwaq_dead = 0;

__attribute__((noinline))
static void *__cdecl mgwaq_hook(void *self, void *ctx, void *c, void *d)
{
    unsigned code = 0xFFFFFFFFu, kind = 0xFFFFFFFFu;
    /* PN 194 (crash fix): FUN_140dcd9f0 takes the CallContext POOL (embedded at
     * CBackEndServiceImpl+0x50) and enters its CRITICAL_SECTION at +0x2d. When
     * the service has been destroyed the pool's first qword (the CS pointer) is
     * NULL, and the call becomes EnterCriticalSection(NULL) -> the 0xc0000005
     * write to +8 (0xdcda1d) that kills ~1 launch in 3.
     *
     * [pool] == NULL is a UNIQUE post-destruction signature: only the teardown
     * path writes it (0x13454b0, reached from FUN_140dcd890), and it is set at
     * construction. A live-but-empty pool has begin == end == 0 while the CS
     * pointer is still valid, so this test can never mask a real queue push.
     * Skipping is correct: there is no service left to notify. */
    if (self && safe_readable(self, 8) && *(void **)self == NULL) {
        if (g_mgwaq_dead < 5) {
            g_mgwaq_dead++;
            probe_log("mgwaq: pool destroyed (CS=NULL) - skipped the queue push "
                      "that would have crashed at 0xdcda1d");
        }
        return NULL;
    }
    if (self) g_mgwa_impl = self;
    if (ctx && safe_readable(ctx, 0x60)) {
        code = *(unsigned *)((char *)ctx + 0x14);
        kind = *(unsigned *)((char *)ctx + 0x50);
    }
    if (g_mgwaq_n < 20) {
        g_mgwaq_n++;
        probe_log("mgwaq[%d]: impl=%p ctx=%p ctxcode=%u ctx+0x50=%u",
                  g_mgwaq_n, self, ctx, code, kind);
    }
    return ((ci_fn)g_mgwaq_tramp)(self, ctx, c, d);
}

__attribute__((noinline))
static void *__cdecl mgwast_hook(void *impl, void *st, void *c, void *d, void *e)
{
    unsigned state = 0xFFFFFFFFu, code = 0xFFFFFFFFu;
    if (impl && safe_readable((char *)impl + 0x20, 4)) state = *(unsigned *)((char *)impl + 0x20);
    if (st && safe_readable((char *)st + 0x30, 4)) code = *(unsigned *)((char *)st + 0x30);
    if (g_mgwast_n < 40) {
        g_mgwast_n++;
        probe_log("mgwast[%d]: impl=%p state=%u out_code=%u a5=%p",
                  g_mgwast_n, impl, state, code, e);
    }
    /* FUN_140d92cf0 takes FIVE args: a 4-arg forward corrupts r9 (PN 112). */
    return ((ci5_fn)g_mgwast_tramp)(impl, st, c, d, e);
}


/* ---------------- PRUDP inbound packet processor (v104, PN 116) -----------
 * FUN_14136d500(stream, u16, u8, pkt): the client's handler for a received
 * datagram. It dispatches on pkt+0x22 & 7 (0 = SYN) and, for a SYN, looks the
 * packet up in a STRING-keyed connection map using the std::string at pkt+0x48.
 * Logging here answers two questions at once: do our SYN-ACKs arrive at all, and
 * what key does the client compute for them? UPC_PRUDPSYN=0 disables. */
static unsigned char *g_prudp_tramp = NULL;
static int g_prudp_n = 0;

__attribute__((noinline))
static void *__cdecl prudp_hook(void *stream, void *a2, void *a3, void *pkt, void *a5)
{
    if (g_prudp_n < 30 && pkt && safe_readable(pkt, 0x70)) {
        unsigned char *q = (unsigned char *)pkt;
        unsigned type = q[0x22] & 7, ack = (q[0x22] >> 3) & 1;
        unsigned long long len = *(unsigned long long *)(q + 0x58);
        const char *sp = (len >= 16) ? *(const char **)(q + 0x48) : (const char *)(q + 0x48);
        char key[64];
        g_prudp_n++;
        if (len > 60) len = 60;
        ci_str((void *)sp, key, (int)len + 1);
        probe_log("prudp pkt[%d]: stream=%p type=%u ack=%u dst=%u dword1b=%u key=\"%s\"",
                  g_prudp_n, stream, type, ack, q[0x21], *(unsigned *)(q + 0xd8), key);
    }
    return ((ci5_fn)g_prudp_tramp)(stream, a2, a3, pkt, a5);
}


/* ---------------- RV websocket connection endpoint (v109, PN 126) ---------
 * FUN_1413619f0(stream, url_str, ...) builds a rdv::WebSocketEndPoint for the
 * rendezvous LB url, adds X-Rdv-Sec-Public-Key when the url starts with "ws://"
 * and registers it with the connection manager. The login stream we serve has NO
 * headers, so it is a different endpoint - this probe says whether the connection
 * endpoint is created at all in ws mode, and what url it gets.
 * UPC_WSENDPT=0 disables. */
static unsigned char *g_wsep_tramp = NULL;
static int g_wsep_n = 0;

__attribute__((noinline))
static void *__cdecl wsep_hook(void *stream, void *urlh, void *c, void *d)
{
    void *rv = ((ci_fn)g_wsep_tramp)(stream, urlh, c, d);
    if (g_wsep_n < 8) {
        char url[96];
        g_wsep_n++;
        ci_str(urlh, url, sizeof(url));
        probe_log("wsendpoint[%d]: stream=%p url=\"%s\" -> %p", g_wsep_n, stream, url, rv);
    }
    return rv;
}


/* ---------------- the RV "registrar" object (v111, PN 127) ----------------
 * FUN_1413619f0 registers the WebSocketEndPoint through
 *   (*(vtable+0x20))(registrar, ...)  with registrar = FUN_140ede0b0(...)
 * which thunks into FUN_140ede0c0. Logging the object it returns plus its
 * vtable slot +0x20 tells us which function the registration actually calls -
 * and whether that is one of the connmgr slots (0x1361020 / 0x13612c0) that we
 * already know never fire. UPC_WSEPT=0 disables. */
static unsigned char *g_edc_tramp = NULL;
static int g_edc_n = 0;

__attribute__((noinline))
static void *__cdecl edc_hook(void *a, void *b, void *c, void *d)
{
    void *rv = ((ci_fn)g_edc_tramp)(a, b, c, d);
    if (g_edc_n < 8) {
        void *vt = 0, *slot20 = 0;
        g_edc_n++;
        if (rv && safe_readable(rv, 8)) vt = *(void **)rv;
        if (vt && safe_readable((char *)vt + 0x20, 8)) slot20 = *(void **)((char *)vt + 0x20);
        probe_log("rvregistrar[%d]: obj=%p vt=%p slot+0x20=%p", g_edc_n, rv, vt, slot20);
    }
    return rv;
}


/* ---------------- the endpoint registration (v112, PN 128) ---------------
 * The registrar object's vtable+0x20 resolves to RVA 0xeed770 in the SDK layer,
 * i.e. NOT one of the connmgr slots. This hooks it and logs the arguments plus
 * the return value, which is the last step before the manager would connect.
 * UPC_WREG=0 disables. */
static unsigned char *g_wreg_tramp = NULL;
static int g_wreg_n = 0;

__attribute__((noinline))
static void *__cdecl wreg_hook(void *a, void *b, void *c, void *d, void *e, void *f)
{
    void *rv = ((ci8_fn)g_wreg_tramp)(a, b, c, d, e, f, NULL, NULL);
    if (g_wreg_n < 10) {
        g_wreg_n++;
        probe_log("wsregister[%d]: a=%p b=%p c=%p d=%p -> %d",
                  g_wreg_n, a, b, c, d, (int)(long long)rv);
    }
    return rv;
}


/* ---------------- JobConnectEndPoint step (v113, PN 129) ------------------
 * FUN_141380100 is the step that DECIDES whether to connect: it looks the job up
 * by id, checks the flag at job+0x1f0, and either schedules the next step or calls
 * FUN_141361380(job+0xd0) = initiate the connection on the endpoint. Logging the
 * branch tells us whether the client ever tries to connect our endpoint.
 * UPC_JCE=0 disables. */
static unsigned char *g_jce_tramp = NULL;
static int g_jce_n = 0;

__attribute__((noinline))
static void *__cdecl jce_hook(void *job, void *b, void *c, void *d)
{
    if (g_jce_n < 20 && job && safe_readable(job, 0x200)) {
        unsigned char *p = (unsigned char *)job;
        unsigned flag = p[0x1f0];
        void *ep = *(void **)(p + 0xd0);
        unsigned state = *(unsigned *)(p + 0x38);
        g_jce_n++;
        probe_log("jobconnect[%d]: job=%p flag1f0=%u endpoint(d0)=%p state=%u",
                  g_jce_n, job, flag, ep, state);
    }
    return ((ci_fn)g_jce_tramp)(job, b, c, d);
}


/* ---------------- conquest createSession (v114, PN 130) -------------------
 * FacadeInternal::createSession (FUN_140ebfe00) is what the conquest client
 * submits before it can dial :3080. If this never fires, the conquest client
 * never even asks; if it fires, the stall is downstream. UPC_CREATESESS=0 off. */
static unsigned char *g_csess_tramp = NULL;
static int g_csess_n = 0;

__attribute__((noinline))
static void *__cdecl csess_hook(void *a, void *b, void *c, void *d)
{
    if (g_csess_n < 10) {
        g_csess_n++;
        probe_log("createSession[%d]: a=%p b=%p c=%p d=%p", g_csess_n, a, b, c, d);
    }
    return ((ci_fn)g_csess_tramp)(a, b, c, d);
}


/* ---------------- who closes the RV stream? (v115, PN 131) ---------------
 * The RV login websocket is torn down 150-480 ms after our reply, always with
 * ws close 1000. Hooking closesocket with a backtrace names the code that does
 * it - the missing link between "login done" and "connection never opened".
 * UPC_WHOCLOSE=0 disables. */
static int g_whoclose_n = 0;

static int __cdecl whoclose_hook(unsigned long long sk)
{
    int rc = g_closesock_orig(sk);
    if (g_whoclose_n < 6) {
        void *frames[14];
        char st[320];
        unsigned nf = bt_capture(frames, 14, 1);
        g_whoclose_n++;
        bt_format(frames, nf, st, sizeof(st));
        probe_log("closesocket[%d]: sk=%llu stack:%s", g_whoclose_n, sk, st);
    }
    return rc;
}

static void rvregistrar_install(void)
{
    static const unsigned char e[17] = {
        0x40, 0x57,                         /* push rdi            */
        0x48, 0x83, 0xec, 0x20,             /* sub rsp,0x20        */
        0x48, 0x83, 0xb9, 0x28, 0x01, 0x00, 0x00, 0x00,  /* cmp [rcx+0x128],0 */
        0x48, 0x8b, 0xf9                    /* mov rdi,rcx         */
    };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_WSEPT", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("rvregistrar probe: off (UPC_WSEPT=0)");
        return;
    }
    hook_install("rv registrar", 0xede0c0u, e, 17u, &g_edc_tramp, (void *)edc_hook);
    {
        static const unsigned char e4[14] = {
            0x48, 0x89, 0x5c, 0x24, 0x10,   /* mov [rsp+0x10],rbx */
            0x48, 0x89, 0x6c, 0x24, 0x18,   /* mov [rsp+0x18],rbp */
            0x56, 0x57,                     /* push rsi/rdi       */
            0x41, 0x56                      /* push r14           */
        };
        hook_install("FacadeInternal::createSession", 0xebfe00u, e4, 14u,
                     &g_csess_tramp, (void *)csess_hook);
    }
    {
        /* PN 128: the registrar's vtable+0x20 -> RVA 0xeed770 (measured live) */
        static const unsigned char e2[16] = {
            0x48, 0x89, 0x74, 0x24, 0x20,   /* mov [rsp+0x20],rsi */
            0x55, 0x57,                     /* push rbp/rsi       */
            0x41, 0x54, 0x41, 0x55,         /* push r12/r13       */
            0x41, 0x56,                     /* push r14           */
            0x48, 0x8b, 0xec                /* mov rbp,rsp        */
        };
        hook_install("ws register", 0xeed770u, e2, 16u, &g_wreg_tramp, (void *)wreg_hook);
    }
    {
        static const unsigned char e3[16] = {
            0x40, 0x53,                     /* push rbx        */
            0x48, 0x83, 0xec, 0x40,         /* sub rsp,0x40    */
            0x48, 0x8b, 0xd9,               /* mov rbx,rcx     */
            0xe8, 0x02, 0x84, 0xa6, 0xff    /* call FUN_140de8510 */
        };
        hook_install("JobConnectEndPoint step", 0x1380100u, e3, 16u, &g_jce_tramp, (void *)jce_hook);
    }
}

static void wsepdpoint_install(void)
{
    static const unsigned char e[16] = {
        0x48, 0x89, 0x5c, 0x24, 0x20,   /* mov [rsp+0x20],rbx */
        0x55, 0x56, 0x57,               /* push rbp/rsi/rdi    */
        0x41, 0x54, 0x41, 0x55,         /* push r12/r13        */
        0x41, 0x56, 0x41, 0x57          /* push r14/r15        */
    };
    /* PN 154: THIS HOOK IS OFF BY DEFAULT AND MUST STAY OFF FOR PRODUCTION RUNS.
     *
     * It is an inline 16-byte hook at the rdv::WebSocketEndPoint constructor
     * (0x13619f0). Measured across 71 runs: with this hook installed the client
     * faulted at Anno5+0x1362214 in 21 of 21 runs (it dereferences endpoint+0x78,
     * the operation callback, which this hook's displaced prologue prevents the
     * constructor from initialising); the single run where the hook was skipped
     * (prologue mismatch) and all 49 runs on a build without it never crashed.
     * It cost this project an entire session of "the client crashes in its own
     * code" debugging. Opt in with UPC_WSENDPT=1 only when specifically probing
     * endpoint construction, and never leave it on for a verification run. */
    char dis[8] = {0};
    if (!(GetEnvironmentVariableA("UPC_WSENDPT", dis, sizeof(dis)) > 0 && dis[0] == '1')) {
        probe_log("wsendpoint probe: off by default (UPC_WSENDPT=1 enables; "
                  "installing it crashes the client, see PN 154)");
        return;
    }
    hook_install("ws connection endpoint", 0x13619f0u, e, 16u, &g_wsep_tramp, (void *)wsep_hook);
}

/* ---------------- conquest login trigger (v127, PN 171) -------------------
 * The conquest client (CServerPHPImplementation) is constructed unconditionally
 * at game+0x808 and ticked every frame, but it never ISSUES anything until its
 * login FUN_140590370 has run - and the only callers of that are the
 * conquest-lobby UI callback, a news action and the invitation responses, none of
 * which a fresh single-player profile reaches.
 *
 * v125 tried to nudge it by HOOKING the per-frame tick (0x0d4b40) and crashed the
 * client twice on a clean state before its own trigger even fired - hooking a
 * function the client calls every frame is not worth that risk. This version
 * hooks NOTHING: a background thread waits for the front-end to settle and then
 * calls the login once. The callee re-derives its profile pointer from a global
 * (FUN_1405ce480 at 0x5ce4c0) and never dereferences the argument, so passing the
 * node object is safe.
 *
 * Opt-in: UPC_CONQLOGIN=1. Optional: UPC_CONQLOGIN_DELAY (ms, default 20000).
 */
#define CONQLOGIN_RVA 0x590370u

static void conqlogin_thread(void)
{
    char env[16] = {0};
    int delay_ms = 90000;   /* PN 173: the real call site FUN_140335010 calls
                             * FUN_140590370() with NO arguments and only from the
                             * conquest-lobby entry, i.e. long after the RV session
                             * is up. Firing at 20 s crashed inside the callee; the
                             * one run that did NOT crash fired at ~45 s with the
                             * session already established. Default 90 s. */
    if (GetEnvironmentVariableA("UPC_CONQLOGIN_DELAY", env, sizeof(env)) > 0 && env[0])
        delay_ms = atoi(env);
    if (delay_ms < 0) delay_ms = 0;
    Sleep((DWORD)delay_ms);
    {
        /* PN 172: FUN_140590370 DEREFERENCES its CMainControl argument (the v127
         * NULL call crashed inside it), so before calling we log the two candidate
         * globals the earlier passes identified: DAT_141ba1428 (game object) and
         * DAT_141ba1518 (menu manager). Log-only by default; UPC_CONQLOGIN_CALL=1
         * also performs the call with the first non-zero candidate. */
        typedef void (__cdecl *conqlogin_fn)(void *ctl);
        unsigned char *base = (unsigned char *)GetModuleHandleA(NULL);
        void *g_game = *(void **)(base + 0x1ba1428u);
        void *g_menu = *(void **)(base + 0x1ba1518u);
        void *ctl = g_game ? g_game : g_menu;
        char call[8] = {0};
        conqlogin_fn login = (conqlogin_fn)(uintptr_t)(base + CONQLOGIN_RVA);
        probe_log("conqlogin: after %d ms g_game=%p g_menu=%p -> ctl=%p",
                  delay_ms, g_game, g_menu, ctl);
        if (GetEnvironmentVariableA("UPC_CONQLOGIN_CALL", call, sizeof(call)) > 0
            && call[0] == '1') {
            /* no argument: that is exactly how the real caller invokes it */
            /* PN 180: do NOT call FUN_140c186b0 from here - it dumps the client
             * the instant it runs off the main thread (PN 178). Now that the
             * rendezvous/entity config serves the document at the ROOT (PN 179),
             * the login alone is enough to see whether the reader finds the
             * address. */
            probe_log("conqlogin: calling conquest login 0x%x() (no args, as the real caller does)",
                      CONQLOGIN_RVA);
            ((void (__cdecl *)(void))login)();
            probe_log("conqlogin: conquest login returned");
        }
    }
}

static void conqlogin_probe_install(void)
{
    char dis[8] = {0};
    if (!(GetEnvironmentVariableA("UPC_CONQLOGIN", dis, sizeof(dis)) > 0 && dis[0] == '1'))
        return;                                   /* off by default */
    probe_log("conqlogin: enabled (UPC_CONQLOGIN=1) - thread trigger, no hooks");
    CreateThread(NULL, 0, (LPTHREAD_START_ROUTINE)conqlogin_thread, NULL, 0, NULL);
}

static void prudp_probe_install(void)
{
    static const unsigned char e[20] = {
        0x40, 0x55,                     /* push rbp */
        0x53, 0x56, 0x57,               /* push rbx/rsi/rdi */
        0x41, 0x54, 0x41, 0x55,         /* push r12/r13 */
        0x41, 0x56, 0x41, 0x57,         /* push r14/r15 */
        0x48, 0x8d, 0xac, 0x24, 0x48, 0xff, 0xff, 0xff   /* lea rbp,[rsp-0xb8] */
    };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_PRUDPSYN", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("prudp pkt probe: off (UPC_PRUDPSYN=0)");
        return;
    }
    hook_install("prudp pkt processor", 0x136d500u, e, 20u, &g_prudp_tramp, (void *)prudp_hook);
}

static void mgwa_probes_install(void)
{
    /* copy_n MUST end on an instruction boundary (PN 47/111): 5+5+1+4 = 15 */
    static const unsigned char e_q[15] = {
        0x48, 0x89, 0x5c, 0x24, 0x08,   /* mov [rsp+8],rbx    */
        0x48, 0x89, 0x54, 0x24, 0x10,   /* mov [rsp+0x10],rdx */
        0x57,                           /* push rdi           */
        0x48, 0x83, 0xec, 0x30          /* sub rsp,0x30       */
    };
    static const unsigned char e_st[16] = {
        0x48, 0x89, 0x5c, 0x24, 0x08,
        0x48, 0x89, 0x74, 0x24, 0x18,
        0x48, 0x89, 0x7c, 0x24, 0x20,
        0x55
    };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_MGWA", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("mgwa probes: off (UPC_MGWA=0)");
        return;
    }
    hook_install("mgwa queue", 0xdcd9f0u, e_q, 15u, &g_mgwaq_tramp, (void *)mgwaq_hook);
    CreateThread(NULL, 0, mgwa_watch, NULL, 0, NULL);
    hook_install("mgwa state", 0xd92cf0u, e_st, 16u, &g_mgwast_tramp, (void *)mgwast_hook);
}

static void conninit_probes_install(void)
{
    static const unsigned char e_besl[14] = {
        0x48, 0x89, 0x5c, 0x24, 0x10,   /* mov [rsp+0x10],rbx */
        0x48, 0x89, 0x74, 0x24, 0x18,   /* mov [rsp+0x18],rsi */
        0x55, 0x57,                     /* push rbp; push rdi  */
        0x41, 0x54                      /* push r12           */
    };
    static const unsigned char e_chk[15] = {
        0x40, 0x53,                     /* push rbx            */
        0x48, 0x83, 0xec, 0x20,         /* sub  rsp,0x20       */
        0x80, 0x79, 0x60, 0x00,         /* cmp  byte [rcx+0x60],0 */
        0x48, 0x8b, 0xd9,               /* mov  rbx,rcx        */
        0x75, 0x0f                      /* jne  +0xf           */
    };
    static const unsigned char e_jin[16] = {
        0x48, 0x8b, 0xc4,               /* mov  rax,rsp        */
        0x53, 0x56,                     /* push rbx; push rsi  */
        0x48, 0x83, 0xec, 0x68,         /* sub  rsp,0x68       */
        0x48, 0x89, 0x68, 0x08,         /* mov  [rax+8],rbp    */
        0x48, 0x8b, 0xea                /* mov  rbp,rdx        */
    };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_CONNINIT", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("conninit probes: off (UPC_CONNINIT=0)");
        return;
    }
    hook_install("besl step", 0x1349480u, e_besl, 14u, &g_besl_tramp, (void *)besl_hook);
    /* PN 112: 0x134fcf0's first 15 bytes END WITH a `jne rel8`, and the
     * displaced-copy trampoline does not relocate relative branches, so the
     * jump lands in the wrong place -> AV inside the prudp connection path.
     * Opt-in only (UPC_CONNINIT2=1); the decision it gates is already visible
     * from the besl/submit probes. */
    if (GetEnvironmentVariableA("UPC_CONNINIT2", dis, sizeof(dis)) > 0 && dis[0] == '1')
        hook_install("checkRequirements", 0x134fcf0u, e_chk, 15u, &g_chkre_tramp, (void *)chkre_hook);
    else
        probe_log("checkRequirements probe: off (UPC_CONNINIT2=1 enables; its prologue has a rel8 branch)");
    hook_install("submitJobInitiateConnection", 0x1360ed0u, e_jin, 16u, &g_jinit_tramp, (void *)jinit_hook);
}

static void rvlogin2_probe_install(void)
{
    static const unsigned char expect[RVLOGIN2_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x08,
        0x48, 0x89, 0x74, 0x24, 0x18,
        0x48, 0x89, 0x7c, 0x24, 0x20,
        0x55, 0x41, 0x54, 0x41, 0x55
    };
    char dis[8] = {0};
    /* v85b: OPT-IN, same reason as rvcfgurl above. */
    if (GetEnvironmentVariableA("UPC_RVLOGIN2", dis, sizeof(dis)) == 0 || dis[0] != '1') {
        probe_log("rvlogin2 probe: off (UPC_RVLOGIN2=1 enables)");
        return;
    }
    hook_install("rvlogin2 probe", RVLOGIN2_RVA, expect, RVLOGIN2_COPY,
                 &g_rvlogin2_tramp, (void *)rvlogin2_hook);
}

static void rvcfgurl_probe_install(void)
{
    static const unsigned char expect[RVCFGURL_COPY] = {
        0x40, 0x55,                                       /* push rbp,rbx */
        0x53, 0x56, 0x57,                                 /* push rsi,rdi */
        0x41, 0x54, 0x41, 0x56, 0x41, 0x57,               /* push r12,r14,r15 */
        0x48, 0x8d, 0x6c, 0x24, 0xd9                      /* lea rbp,[rsp-0x27] */
    };
    char dis[8] = {0};
    /* v85b: OPT-IN. Three runs with this hook installed ended in an AV cascade
     * at the moment the RV login runs (Anno5+0x1074ee0 / 0x108f94d). Whether the
     * hook is the cause is unproven, but a diagnostic probe must not cost a run,
     * so it is off unless asked for. */
    if (GetEnvironmentVariableA("UPC_RVCFGURL", dis, sizeof(dis)) == 0 || dis[0] != '1') {
        probe_log("rvcfgurl probe: off (UPC_RVCFGURL=1 enables)");
        return;
    }
    hook_install("rvcfgurl probe", RVCFGURL_RVA, expect, RVCFGURL_COPY,
                 &g_rvcfgurl_tramp, (void *)rvcfgurl_hook);
}

static void paramkey_probe_install(void)
{
    static const unsigned char expect[PARAMKEY_COPY] = {
        0x40, 0x55,                                       /* push rbp        */
        0x53, 0x56,                                       /* push rbx,rsi    */
        0x48, 0x8b, 0xec,                                 /* mov rbp,rsp     */
        0x48, 0x81, 0xec, 0x80, 0x00, 0x00, 0x00,         /* sub rsp,0x80    */
        0x48, 0x8b, 0x05, 0xd3, 0xb8, 0xb9, 0x00          /* mov rax,[rip+x] */
    };
    char dis[8] = {0};
    /* v86: OPT-IN, same reason as the sdkjob probes. */
    if (GetEnvironmentVariableA("UPC_PARAMKEY", dis, sizeof(dis)) == 0 || dis[0] != '1') {
        probe_log("paramkey probe: off (UPC_PARAMKEY=1 enables)");
        return;
    }
    hook_install("paramkey probe", PARAMKEY_RVA, expect, PARAMKEY_COPY,
                 &g_paramkey_tramp, (void *)paramkey_hook);
}

static void sdkjob_probes_install(void)
{
    static const unsigned char expect[SDKJOB_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x10,
        0x48, 0x89, 0x74, 0x24, 0x18,
        0x48, 0x89, 0x7c, 0x24, 0x20
    };
    char dis[8] = {0};
    /* v86: OPT-IN. Measured 2026-11-05 00:14: with these hooks installed the RV
     * login NEVER RUNS (rvstep=0 in six consecutive runs); with them disabled it
     * runs again immediately (rvstep=3). A diagnostic probe that changes the
     * behaviour it measures is worthless, so it is off by default. */
    if (GetEnvironmentVariableA("UPC_SDKJOBS", dis, sizeof(dis)) == 0 || dis[0] != '1') {
        probe_log("sdkjob probes: off (UPC_SDKJOBS=1 enables; they suppress the RV login)");
        return;
    }
    hook_install("sdkconn probe", SDKCONN_RVA, expect, SDKJOB_COPY,
                 &g_sdkconn_tramp, (void *)sdkconn_hook);
    hook_install("sdkws probe", SDKWS_RVA, expect, SDKJOB_COPY,
                 &g_sdkws_tramp, (void *)sdkws_hook);
}

static void rvlogin_probe_install(void)
{
    static const unsigned char expect[RVLOGIN_COPY] = {
        0x4c, 0x89, 0x4c, 0x24, 0x20,               /* mov [rsp+0x20],r9 */
        0x4c, 0x89, 0x44, 0x24, 0x18,               /* mov [rsp+0x18],r8 */
        0x53, 0x55, 0x56, 0x57                       /* push rbx,rbp,rsi,rdi */
    };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_RVLOGIN", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("rvlogin probe: off (UPC_RVLOGIN=0)");
        return;
    }
    hook_install("rvlogin probe", RVLOGIN_RVA, expect, RVLOGIN_COPY,
                 &g_rvlogin_tramp, (void *)rvlogin_hook);
}

static void rvsubmit_probe_install(void)
{
    static const unsigned char expect[RVSUBMIT_COPY] = {
        0x48, 0x89, 0x5c, 0x24, 0x10,               /* mov [rsp+0x10],rbx */
        0x48, 0x89, 0x6c, 0x24, 0x18,               /* mov [rsp+0x18],rbp */
        0x48, 0x89, 0x74, 0x24, 0x20                /* mov [rsp+0x20],rsi */
    };
    char dis[8] = {0};
    if (GetEnvironmentVariableA("UPC_RVSUBMIT", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("rvsubmit probe: off (UPC_RVSUBMIT=0)");
        return;
    }
    hook_install("rvsubmit probe", RVSUBMIT_RVA, expect, RVSUBMIT_COPY,
                 &g_rvsubmit_tramp, (void *)rvsubmit_hook);
}

static void rvstep_probe_install(void)
{
    static const unsigned char expect[RVSTEP_COPY] = {
        0x40, 0x53,                                 /* push rbx            */
        0x48, 0x83, 0xec, 0x20,                     /* sub  rsp,0x20       */
        0x48, 0x8b, 0x02,                           /* mov  rax,[rdx]      */
        0x48, 0x8b, 0xd9,                           /* mov  rbx,rcx        */
        0x48, 0x89, 0x41, 0x48                      /* mov  [rcx+0x48],rax */
    };
    char dis[8] = {0};

    /* v69: default ON again. The "never fires" result of v67/v68 was a broken
     * thunk (copy_n 6 < 14), not evidence that the live failure precedes the
     * step runner. UPC_RVSTEPS=0 disables. */
    if (GetEnvironmentVariableA("UPC_RVSTEPS", dis, sizeof(dis)) > 0 && dis[0] == '0') {
        probe_log("rvstep probe: off (UPC_RVSTEPS=0)");
        return;
    }
    hook_install("rvstep probe", RVSTEP_RVA, expect, RVSTEP_COPY,
                 &g_rvstep_tramp, (void *)rvstep_hook);
}

static void gate_patch_install(void)
{
    char dir[MAX_PATH], path[MAX_PATH];
    char *p;
    DWORD n = GetModuleFileNameA(NULL, dir, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return;
    p = dir + n;
    while (p > dir && *p != '\\' && *p != '/') p--;
    *p = 0;
    snprintf(path, sizeof(path), "%s\\force-params-ready.txt", dir);
    if (GetFileAttributesA(path) == INVALID_FILE_ATTRIBUTES) return;

    probe_log("force-params-ready.txt found -> patching the readiness gate");
    gate_drop_jcc8(0xf1f12e);              /* JobLogin::reportOutcome          */
    gate_force_jmp32(0xf1f144, 0xf1f45c);
    gate_drop_jcc8(0xf44203);              /* JobRequestParameters             */
    gate_force_jmp32(0xf4421a, 0xf44516);
    gate_drop_jcc8(0xebd490);              /* notification URL builder         */
    gate_force_jmp32(0xebd49e, 0xebd799);
    gate_drop_jcc8(0xee67b8);              /* SessionManager gate method       */
    gate_force_jmp32(0xee67d2, 0xee6a9b);
}

/* ---------------- UPC constants ---------------- */

enum {
    UPC_Result_Ok          = 0,
    UPC_Result_Failed      = -1,
    UPC_Result_NotFound    = -6,
    UPC_Result_Unauthorized= -9,
};

enum { /* product type */
    UPC_ProductType_App  = 1,
    UPC_ProductType_DLC  = 2,
    UPC_ProductType_Item = 4,
};

enum { /* ownership / state / activation */
    UPC_ProductOwnership_Owned     = 1,
    UPC_ProductState_Playable      = 1,
    UPC_ProductActivation_Purchased= 1,
};

typedef void (*UPC_Callback)(int result, void *data);

struct UPC_Product {
    unsigned int id;
    unsigned int type;
    unsigned int ownership;
    unsigned int state;
    unsigned int balance;
    unsigned int activation;
};

struct UPC_ProductList {
    unsigned int count;
    unsigned int padding;
    struct UPC_Product **list;
};

struct UPC_User {
    const char *idUtf8;
    const char *nameUtf8;
    unsigned int relationship;
    void *presence; /* UPC_Presence* — the game dereferences this without checks! */
};

struct UPC_Presence {
    unsigned int onlineStatus;
    const char *detailsUtf8;
    unsigned int titleId;
    const char *titleNameUtf8;
    const char *multiplayerId;
    int multiplayerJoinable;
    unsigned int multiplayerSize;
    unsigned int multiplayerMaxSize;
    void *multiplayerInternalData;
    unsigned int multiplayerInternalDataSize;
};

/* ---------------- state ---------------- */

static HMODULE g_self = NULL;
static unsigned int g_product_id = 22;   /* Anno 2070 standard SKU */
static int g_initialized = 0;

static char g_user_id[64]    = "00000000-0000-0000-0000-0000000000aa";
static char g_user_name[64]  = "Player";
static char g_ticket[128]    = "offline-ticket";
static char g_language[32]   = "en-US";

static struct {
    unsigned int type;
    void *handler;
    void *opt_data;
} g_handlers[16];
static int g_handler_count = 0;

static char g_mp_id[32] = "offline";
static char g_presence_details[64] = "Playing offline";
static char g_presence_title[32] = "Anno 2070";

static struct UPC_Presence g_presence = {
    1,                  /* onlineStatus: online */
    g_presence_details,
    22,                 /* titleId, updated in UPC_Init */
    g_presence_title,
    g_mp_id,
    0,                  /* not joinable */
    0, 0,               /* multiplayer size */
    NULL, 0
};

static struct UPC_User g_user = { g_user_id, g_user_name, 0, &g_presence };

/* fake context: never dereferenced by us; must just be a stable non-null ptr */
static char g_context[64];

/* ---------------- helpers ---------------- */

static void *mem(size_t n)
{
    return HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, n);
}

static void memfree(void *p)
{
    if (p) HeapFree(GetProcessHeap(), 0, p);
}

static char *dup_str(const char *s)
{
    size_t n = strlen(s) + 1;
    char *d = (char *)mem(n);
    if (d) memcpy(d, s, n);
    return d;
}

static void fire(void *cb, void *data, int result)
{
    if (cb) {
        ((UPC_Callback)cb)(result, data);
    }
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = h;
        DisableThreadLibraryCalls(h);
        log_init();
        logf("=== upc_r2 offline stub v96 loaded (pid %u) ===", GetCurrentProcessId());
        veh_install();
        sdk_hook_install();
        store_probe_install();
        storeupd_hook_install();
        primary_hook_install();
        rdname_hook_install();
        conquest_probe_install();
        gate_patch_install();
    rvstore_patch_install();
    nomodal_patch_install();
    noe7d_patch_install();
    rvstep_probe_install();
    mcjob_probe_install();
    rvsubmit_probe_install();
    jobend_probe_install();
    conqlogin_probe_install();
    rvlogin_probe_install();
    sdkjob_probes_install();
    paramkey_probe_install();
    rvcfgurl_probe_install();
    rvlogin2_probe_install();
    cmgr_probes_install();
    conninit_probes_install();
    mgwa_probes_install();
    prudp_probe_install();
    wsepdpoint_install();
    rvregistrar_install();
    prudpsock_install();
    wsstream_probes_install();
    rmcdisp_probe_install();
    rvsend_probe_install();
    nologin_patch_install();
    ws_trace_install();
    conqcustom_patch_install();
    notif_probes_install();
        force_online_patch();
        net_probe_install();
    } else if (reason == DLL_PROCESS_DETACH) {
        logf("=== upc_r2 offline stub unloaded ===");
    }
    return TRUE;
}

/* ---------------- exports ---------------- */

__declspec(dllexport) int __cdecl UPC_Init(unsigned int inVersion, int productId)
{
    logf("UPC_Init(version=0x%x, productId=%d)", inVersion, productId);
    if (productId > 0)
        g_product_id = (unsigned int)productId;
    g_presence.titleId = g_product_id;
    g_initialized = 1;
    return 0; /* UPC_InitResult_Ok */
}

__declspec(dllexport) void __cdecl UPC_Uninit(void)
{
    logf("UPC_Uninit()");
    g_initialized = 0;
}

__declspec(dllexport) void *__cdecl UPC_ContextCreate(unsigned int inVersion, void *inOptSetting)
{
    logf("UPC_ContextCreate(version=0x%x, settings=%p)", inVersion, inOptSetting);
    return (void *)g_context;
}

__declspec(dllexport) int __cdecl UPC_ContextFree(void *inContext)
{
    logf("UPC_ContextFree(ctx=%p)", inContext);
    return UPC_Result_Ok;
}

__declspec(dllexport) const char *__cdecl UPC_InstallLanguageGet(void *inContext)
{
    logf("UPC_InstallLanguageGet(ctx=%p) -> \"%s\"", inContext, g_language);
    return g_language;
}

__declspec(dllexport) int __cdecl UPC_InstallLanguageGet_Extended(void *inContext, const char **outLanguage)
{
    if (outLanguage) *outLanguage = g_language;
    logf("UPC_InstallLanguageGet_Extended() -> \"%s\"", g_language);
    return UPC_Result_Ok;
}

/* The conquest client polls UserGet+UserFree+TicketGet in a tight loop
 * (~3000/s once the online layer starts retrying) - at 90 bytes/line that
 * produced 127 MB logs in 20 min. Log the first calls, then every 5000th. */
static unsigned long g_user_log_n = 0;

static int user_log_wanted(void)
{
    g_user_log_n++;
    return g_user_log_n <= 20 || (g_user_log_n % 5000) == 0;
}

__declspec(dllexport) int __cdecl UPC_UserGet(void *inContext, const char *inOptUserIdUtf8,
                                              void **outUser, void *inCallback, void *inOptCallbackData)
{
    if (user_log_wanted())
        logf("UPC_UserGet(ctx=%p, user=\"%s\", out=%p, cb=%p, data=%p)",
             inContext, inOptUserIdUtf8 ? inOptUserIdUtf8 : "(self)", outUser, inCallback, inOptCallbackData);
    if (!outUser)
        return UPC_Result_Failed;
    *outUser = &g_user;
    fire(inCallback, inOptCallbackData, UPC_Result_Ok);
    return 0x10000; /* request handle, like the real async API */
}

__declspec(dllexport) int __cdecl UPC_UserFree(void *inContext, void *inUser)
{
    if (user_log_wanted())
        logf("UPC_UserFree(ctx=%p, user=%p)", inContext, inUser);
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_TicketGet_Extended(void *inContext, const char **outTicket)
{
    if (user_log_wanted())
        logf("UPC_TicketGet_Extended(ctx=%p)", inContext);
    if (outTicket) *outTicket = g_ticket;
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_ProductListGet(void *inContext, const char *inOptUserIdUtf8,
                                                     unsigned int inFilter, struct UPC_ProductList **outProductList,
                                                     void *inCallback, void *inOptCallbackData)
{
    logf("UPC_ProductListGet(ctx=%p, user=\"%s\", filter=%u, out=%p, cb=%p, data=%p)",
         inContext, inOptUserIdUtf8 ? inOptUserIdUtf8 : "(self)", inFilter,
         outProductList, inCallback, inOptCallbackData);
    if (!outProductList)
        return UPC_Result_Failed;

    struct UPC_ProductList *pl = (struct UPC_ProductList *)mem(sizeof(*pl));
    struct UPC_Product *p = (struct UPC_Product *)mem(sizeof(*p));
    if (!pl || !p) {
        memfree(pl); memfree(p);
        return UPC_Result_Failed;
    }
    p->id         = g_product_id;
    p->type       = UPC_ProductType_App;
    p->ownership  = UPC_ProductOwnership_Owned;
    p->state      = UPC_ProductState_Playable;
    p->balance    = 0;
    p->activation = UPC_ProductActivation_Purchased;

    pl->count   = 1;
    pl->padding = 0;
    pl->list    = (struct UPC_Product **)mem(sizeof(void *));
    if (!pl->list) { memfree(pl); memfree(p); return UPC_Result_Failed; }
    pl->list[0] = p;
    *outProductList = pl;

    fire(inCallback, inOptCallbackData, UPC_Result_Ok);
    return 0x10000;
}

__declspec(dllexport) int __cdecl UPC_ProductListFree(void *inContext, struct UPC_ProductList *inProductList)
{
    logf("UPC_ProductListFree(ctx=%p, list=%p)", inContext, inProductList);
    if (inProductList) {
        for (unsigned int i = 0; i < inProductList->count; i++)
            memfree(inProductList->list[i]);
        memfree(inProductList->list);
        memfree(inProductList);
    }
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_EventRegisterHandler(void *inContext, unsigned int inType,
                                                           void *inHandler, void *inOptData)
{
    logf("UPC_EventRegisterHandler(ctx=%p, type=%u, handler=%p, data=%p)",
         inContext, inType, inHandler, inOptData);
    if (g_handler_count < 16) {
        g_handlers[g_handler_count].type = inType;
        g_handlers[g_handler_count].handler = inHandler;
        g_handlers[g_handler_count].opt_data = inOptData;
        g_handler_count++;
    }
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_EventUnregisterHandler(void *inContext, unsigned int inType)
{
    logf("UPC_EventUnregisterHandler(ctx=%p, type=%u)", inContext, inType);
    for (int i = 0; i < g_handler_count; i++) {
        if (g_handlers[i].type == inType) {
            g_handlers[i] = g_handlers[g_handler_count - 1];
            g_handler_count--;
            break;
        }
    }
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_Update(void *inContext)
{
    /* callbacks are fired synchronously; nothing to pump */
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_EventNextPeek(void *inContext, void *outEvent)
{
    return UPC_Result_Ok; /* no events pending */
}

__declspec(dllexport) int __cdecl UPC_EventNextPoll(void *inContext, void *outEvent)
{
    return UPC_Result_NotFound; /* no events */
}

/* ------- social / multiplayer: offline => not available ------- */

__declspec(dllexport) int __cdecl UPC_FriendListGet(void *inContext, unsigned int inOptOnlineStatusFilter,
                                                    void *outFriendList, void *inCallback, void *inOptCallbackData)
{
    logf("UPC_FriendListGet(ctx=%p, filter=%u)", inContext, inOptOnlineStatusFilter);
    return -13; /* offline: no friend list (matches reference emu) */
}

__declspec(dllexport) int __cdecl UPC_FriendListFree(void *inContext, void *inFriendList)
{
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_FriendAdd(void *inContext, const char *inOptUserIdUtf8,
                                                void *inCallback, void *inOptCallbackData)
{
    return UPC_Result_NotFound;
}

__declspec(dllexport) int __cdecl UPC_MultiplayerInvite(void *inContext, const char *inUserIdUtf8,
                                                        void *inOptCallback, void *inOptCallbackData)
{
    return UPC_Result_NotFound;
}

__declspec(dllexport) int __cdecl UPC_MultiplayerInviteAnswer(void *inContext, const char *inUserIdUtf8,
                                                              int inAccept, void *inOptCallback, void *inOptCallbackData)
{
    return UPC_Result_NotFound;
}

__declspec(dllexport) int __cdecl UPC_MultiplayerSessionSet(void *inContext, void *inMultiplayerSession)
{
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_MultiplayerSessionClear(void *inContext)
{
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_UserPlayedWithAdd(void *inContext, void *inUserIdUtf8List, unsigned int inListLength)
{
    return UPC_Result_Ok;
}

/* ------- overlay / store: no-op ------- */

__declspec(dllexport) int __cdecl UPC_OverlayShow(void *inContext, unsigned int inSection,
                                                  void *inOptCallback, void *inOptCallbackData)
{
    logf("UPC_OverlayShow(section=%u) -> ignored", inSection);
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_StoreProductDetailsShow(void *inContext, unsigned int inProductId,
                                                              void *inOptCallback, void *inOptCallbackData)
{
    logf("UPC_StoreProductDetailsShow(product=%u) -> ignored", inProductId);
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_OverlayFriendInvitationShow(void *inContext, const char *inOptUserIdUtf8,
                                                                  const char *inOptSessionIdUtf8,
                                                                  void *inOptCallback, void *inOptCallbackData)
{
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_ShowBrowserUrl(void *inContext, void *inBrowserUrlUtf8)
{
    logf("UPC_ShowBrowserUrl() -> ignored");
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_LaunchApp(void *inContext, unsigned int inProductId, void *mustBeZero)
{
    logf("UPC_LaunchApp(product=%u) -> ignored", inProductId);
    return 1;
}

/* ------- misc getters ------- */

__declspec(dllexport) int __cdecl UPC_AvatarGet(void *inContext, const char *inOptUserIdUtf8, unsigned int inSize,
                                                void *outImageRGBA, void *inCallback, void *inOptCallbackData)
{
    /* offline: no avatar */
    return -13;
}

__declspec(dllexport) int __cdecl UPC_AvatarFree(void *inContext, void *inAvatar)
{
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_RichPresenceSet(void *inContext, unsigned int inId, void *inOptTokenList)
{
    return UPC_Result_Ok;
}

__declspec(dllexport) const char *__cdecl UPC_NameGet(void *inContext)
{
    return g_user_name;
}

__declspec(dllexport) int __cdecl UPC_NameGet_Extended(void *inContext, const char **outName)
{
    if (outName) *outName = g_user_name;
    return UPC_Result_Ok;
}

__declspec(dllexport) const char *__cdecl UPC_IdGet(void *inContext)
{
    return g_user_id;
}

__declspec(dllexport) int __cdecl UPC_IdGet_Extended(void *inContext, const char **outId)
{
    if (outId) *outId = g_user_id;
    return UPC_Result_Ok;
}

__declspec(dllexport) int __cdecl UPC_ApplicationIdGet(void *inContext, void *outAppId)
{
    return UPC_Result_Ok;
}

__declspec(dllexport) const char *__cdecl UPC_ErrorToString(int inError)
{
    (void)inError;
    return "offline mode";
}

__declspec(dllexport) int __cdecl UPC_IsCrossBootAllowed(void *inContext)
{
    return 0;
}
