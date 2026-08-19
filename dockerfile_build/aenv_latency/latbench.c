/*
 * latbench - snapshot lazy-load memory latency probe for E2B/AgentENV sandboxes.
 *
 * Measures the first-touch (lazy page-in from a restored snapshot) vs second-touch
 * (resident) latency of a working set, per the customer's benchmark shape:
 *   - working set = N x 4KiB pages
 *   - sequential modes (seq_read/seq_write): touch the FULL 4KiB of every page
 *     (all 512 x 8-byte slots), pages in linear order — a bandwidth-style traversal
 *   - random modes (rand_read/rand_write): touch ONE 8-byte slot per page, pages in
 *     shuffled order — a latency-style traversal
 *   - report first-pass ms and second-pass ms
 *
 * The working set MUST be populated before pause so the snapshot stores real page
 * contents; otherwise first-touch degenerates to zero-page mapping and you measure
 * nothing. See docs/README.md in this directory.
 *
 * Backing store:
 *   shm  - POSIX shared memory under /dev/shm (default). Persists across pause/resume
 *          because tmpfs lives in guest RAM, which the snapshot captures.
 *   file - a plain file under /opt/latbench. Use this only if shm does not survive
 *          resume on your backend (then first-touch becomes page-cache/disk load).
 *
 * Usage:
 *   latbench populate  <mib> [shm|file]   create + dirty every page (run BEFORE pause)
 *   latbench measure   <mib> <mode> [shm|file]   re-open, traverse first+second (run AFTER resume)
 *   latbench stress    <mib> <mode> <iters> [shm|file]   populate + loop resident traversal
 *                                                       N times (multi-second profile window)
 *   latbench cleanup   [shm|file]                 unlink the backing object
 *   latbench help
 *
 * Output of measure: "<mode> first_ms second_ms pages mib" (one line, parseable).
 *
 * Determinism: traversal order and in-page offsets use a fixed-seed xorshift32 so
 * x86 and arm traverse byte-identical patterns, making arch results comparable.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define PAGE 4096UL
#define SHM_NAME "/latbench_ws"
#define FILE_PATH "/opt/latbench/ws"
#define SLOT sizeof(uint64_t) /* 8 bytes */

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1.0e6;
}

/* Fixed-seed PRNG so both arches walk the same pattern. */
static uint32_t rng_state = 0x12345678u;
static uint32_t xorshift32(void) {
    uint32_t x = rng_state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    rng_state = x;
    return x;
}

static size_t pages_for(size_t mib) {
    return (mib * 1024UL * 1024UL) / PAGE;
}

static int backing_is_file(const char *backing) {
    return backing && strcmp(backing, "file") == 0;
}

/* open + size + mmap the working set. populate=1 -> create/truncate + MAP_SHARED.
 * populate=0 -> open existing; shared=0 -> MAP_PRIVATE (reads map the shared page
 * read-only, no copy), shared=1 -> MAP_SHARED (writes hit the restored shared page
 * in place, no COW copy — matches a customer payload that writes anonymous memory
 * restored by the snapshot, instead of paying a per-page COW copy on first write). */
static void *open_ws(size_t mib, int populate, int write_mode, const char *backing,
                     int shared, int *out_fd) {
    size_t size = mib * 1024UL * 1024UL;
    int fd;
    void *p;
    int prot = write_mode ? (PROT_READ | PROT_WRITE) : PROT_READ;
    int flags = (populate || shared) ? MAP_SHARED : MAP_PRIVATE;

    if (backing_is_file(backing)) {
        const char *path = FILE_PATH;
        if (populate) {
            fd = open(path, O_CREAT | O_RDWR | O_TRUNC, 0600);
        } else {
            fd = open(path, write_mode ? O_RDWR : O_RDONLY);
        }
        if (fd < 0) {
            fprintf(stderr, "open(%s): %s\n", path, strerror(errno));
            return NULL;
        }
        if (populate && ftruncate(fd, (off_t)size) != 0) {
            fprintf(stderr, "ftruncate: %s\n", strerror(errno));
            close(fd);
            return NULL;
        }
    } else {
        if (populate) {
            /* shm_unlink any stale object first so O_EXCL create is clean. */
            shm_unlink(SHM_NAME);
            fd = shm_open(SHM_NAME, O_CREAT | O_RDWR, 0600);
        } else {
            fd = shm_open(SHM_NAME, O_RDWR, 0600);
        }
        if (fd < 0) {
            fprintf(stderr, "shm_open: %s\n", strerror(errno));
            return NULL;
        }
        if (populate && ftruncate(fd, (off_t)size) != 0) {
            fprintf(stderr, "ftruncate: %s\n", strerror(errno));
            close(fd);
            return NULL;
        }
    }

    p = mmap(NULL, size, prot, flags, fd, 0);
    if (p == MAP_FAILED) {
        fprintf(stderr, "mmap: %s\n", strerror(errno));
        close(fd);
        return NULL;
    }
    *out_fd = fd;
    return p;
}

static int do_populate(size_t mib, const char *backing) {
    int fd = -1;
    void *p = open_ws(mib, 1, 1, backing, 0, &fd);
    if (!p) {
        return 1;
    }
    size_t pages = pages_for(mib);
    /* Dirty every page with a unique value so the snapshot stores real content
     * (not the zero page). One 8-byte write per page is enough to force backing. */
    for (size_t i = 0; i < pages; i++) {
        *((volatile uint64_t *)((char *)p + i * PAGE)) =
            (uint64_t)i * 0x9E3779B97F4A7C15ULL + 1;
    }
    if (backing_is_file(backing)) {
        msync(p, mib * 1024UL * 1024UL, MS_SYNC);
    }
    munmap(p, mib * 1024UL * 1024UL);
    close(fd);
    printf("POPULATED %zu pages %zuMiB backing=%s\n", pages, mib, backing);
    return 0;
}

/* Build traversal order + per-page in-page offsets (deterministic). */
static void build_plan(size_t pages, int random_order, uint32_t **order_out,
                       uint32_t **off_out) {
    uint32_t *order = malloc(pages * sizeof(uint32_t));
    uint32_t *off = malloc(pages * sizeof(uint32_t));
    if (!order || !off) {
        free(order);
        free(off);
        *order_out = NULL;
        *off_out = NULL;
        return;
    }
    for (size_t i = 0; i < pages; i++) {
        order[i] = (uint32_t)i;
    }
    if (random_order) {
        /* Fisher-Yates with fixed-seed xorshift. */
        for (size_t i = pages - 1; i > 0; i--) {
            size_t j = (size_t)(xorshift32() % (uint32_t)(i + 1));
            uint32_t t = order[i];
            order[i] = order[j];
            order[j] = t;
        }
    }
    /* 8-byte aligned offset within a 4KiB page: 0..511 * 8. */
    for (size_t i = 0; i < pages; i++) {
        off[i] = (xorshift32() % 512) * (uint32_t)SLOT;
    }
    *order_out = order;
    *off_out = off;
}

static double traverse_read(char *base, size_t pages, const uint32_t *order,
                             const uint32_t *off, int full_page) {
    /* Independent register accumulators (NOT a single volatile stack sink) so the
     * load loop issues parallel independent loads and we measure real read
     * bandwidth. A `volatile uint64_t sink |= x` chains every load through one
     * stack slot via store-to-load forwarding, serializing the stream and
     * under-measuring bandwidth — and arm's forwarding latency makes the cap bite
     * harder than x86, which was the read-specific slowdown seen in the data
     * (arm seq read ~6GB/s vs write ~18GB/s on the same pages). 4 accumulators
     * break the chain the way STREAM does. */
    uint64_t a0 = 0, a1 = 0, a2 = 0, a3 = 0;
    double t0 = now_ms();
    if (full_page) {
        /* Sequential: touch every 8-byte slot of every 4KiB page (full-page access),
         * pages in linear order. The first slot of each page triggers the lazy
         * page-in; the remaining 511 slots are within the now-resident page. */
        for (size_t i = 0; i < pages; i++) {
            const uint64_t *page = (const uint64_t *)(base + i * PAGE);
            for (size_t s = 0; s < PAGE / SLOT; s += 4) {
                a0 ^= page[s];
                a1 ^= page[s + 1];
                a2 ^= page[s + 2];
                a3 ^= page[s + 3];
            }
        }
    } else {
        /* Random: one 8-byte slot per page, in shuffled page order. Each page is
         * touched exactly once, so first pass = one fault per page. Latency-bound
         * (TLB / page-crossing), so a single register accumulator suffices. */
        for (size_t i = 0; i < pages; i++) {
            a0 ^= *((const uint64_t *)(base + (size_t)order[i] * PAGE + off[i]));
        }
    }
    double t1 = now_ms();
    uint64_t sink = a0 ^ a1 ^ a2 ^ a3;
    /* keep the loads live (use __asm__ so -std=c11, not gnu11, compiles) */
    __asm__ __volatile__("" : : "r"(sink) : "memory");
    (void)sink;
    return t1 - t0;
}

static double traverse_write(char *base, size_t pages, const uint32_t *order,
                             const uint32_t *off, int full_page) {
    double t0 = now_ms();
    if (full_page) {
        /* Sequential full-page write. The mapping is MAP_SHARED for measure
         * writes, so the first write to a page lazy-restores it from the snapshot
         * and writes in place (no COW); remaining slots write the resident page. */
        for (size_t i = 0; i < pages; i++) {
            volatile uint64_t *page = (volatile uint64_t *)(base + i * PAGE);
            for (size_t s = 0; s < PAGE / SLOT; s++) {
                page[s] = 0xDEADBEEFCAFEBABEULL;
            }
        }
    } else {
        /* Random one-slot-per-page write in shuffled order. */
        for (size_t i = 0; i < pages; i++) {
            *((volatile uint64_t *)(base + (size_t)order[i] * PAGE + off[i])) =
                0xDEADBEEFCAFEBABEULL;
        }
    }
    double t1 = now_ms();
    return t1 - t0;
}

static int do_measure(size_t mib, const char *mode, const char *backing) {
    int is_write = 0;
    int is_random = 0;
    if (strcmp(mode, "seq_read") == 0) {
        is_write = 0;
        is_random = 0;
    } else if (strcmp(mode, "seq_write") == 0) {
        is_write = 1;
        is_random = 0;
    } else if (strcmp(mode, "rand_read") == 0) {
        is_write = 0;
        is_random = 1;
    } else if (strcmp(mode, "rand_write") == 0) {
        is_write = 1;
        is_random = 1;
    } else {
        fprintf(stderr, "unknown mode '%s' (want seq_read|seq_write|rand_read|rand_write)\n",
                mode);
        return 2;
    }

    int fd = -1;
    /* Write modes open MAP_SHARED so the first write hits the restored page in
     * place (no COW copy) — matches a customer payload writing anonymous memory
     * the snapshot restored. Read modes stay MAP_PRIVATE (read-only, no COW
     * either way; the read residual vs anonymous is a separate backing-store
     * matter, not COW). */
    void *p = open_ws(mib, 0, is_write, backing, is_write, &fd);
    if (!p) {
        return 1;
    }
    size_t pages = pages_for(mib);

    uint32_t *order = NULL;
    uint32_t *off = NULL;
    build_plan(pages, is_random, &order, &off);
    if (!order || !off) {
        fprintf(stderr, "out of memory\n");
        munmap(p, mib * 1024UL * 1024UL);
        close(fd);
        free(order);
        free(off);
        return 1;
    }

    char *base = (char *)p;
    double first_ms;
    double second_ms;
    /* Sequential modes traverse the full 4KiB of each page; random modes touch
     * one 8-byte slot per page. The access pattern is what distinguishes a
     * bandwidth traversal (seq) from a latency traversal (rand). */
    int full_page = !is_random;
    if (is_write) {
        first_ms = traverse_write(base, pages, order, off, full_page);
        second_ms = traverse_write(base, pages, order, off, full_page);
    } else {
        first_ms = traverse_read(base, pages, order, off, full_page);
        second_ms = traverse_read(base, pages, order, off, full_page);
    }

    printf("%s %.6f %.6f %zu %zu\n", mode, first_ms, second_ms, pages, mib);

    munmap(p, mib * 1024UL * 1024UL);
    close(fd);
    free(order);
    free(off);
    return 0;
}

/* Populate, then loop the traversal `iters` times on the resident (MAP_PRIVATE)
 * mapping. The first traversal faults the private pages in (COW from snapshot if
 * run after resume, or from the populated backing if run live); every subsequent
 * traversal is a pure second-touch (resident) — the path we want to profile. This
 * stretches the ~0.7ms second-touch into a multi-second window so an external
 * profiler (devkit_mem / ksys / perf pinned to the host VMM process) can sample
 * the guest resident-touch path: EPT/TLB walk, L2/L3 cache behavior.
 *
 * Run while attaching a profiler to the host VMM PID, e.g. on the AgentENV host:
 *   perf stat -p <fc_pid> -e cache-misses,dtlb_load_misses.walks -I 1000 &
 *   latbench stress 256 seq_read 2000 shm
 */
static int do_stress(size_t mib, const char *mode, long iters, const char *backing) {
    int is_write = 0;
    int is_random = 0;
    if (strcmp(mode, "seq_read") == 0) {
        is_write = 0;
        is_random = 0;
    } else if (strcmp(mode, "seq_write") == 0) {
        is_write = 1;
        is_random = 0;
    } else if (strcmp(mode, "rand_read") == 0) {
        is_write = 0;
        is_random = 1;
    } else if (strcmp(mode, "rand_write") == 0) {
        is_write = 1;
        is_random = 1;
    } else {
        fprintf(stderr, "unknown mode '%s' (want seq_read|seq_write|rand_read|rand_write)\n", mode);
        return 2;
    }
    if (iters <= 0) {
        fprintf(stderr, "iters must be > 0\n");
        return 2;
    }

    /* Phase 1: populate the backing (MAP_SHARED) so pages hold real content. */
    int pfd = -1;
    void *pp = open_ws(mib, 1, 1, backing, 0, &pfd);
    if (!pp) {
        return 1;
    }
    size_t pages = pages_for(mib);
    for (size_t i = 0; i < pages; i++) {
        *((volatile uint64_t *)((char *)pp + i * PAGE)) = (uint64_t)i * 0x9E3779B97F4A7C15ULL + 1;
    }
    munmap(pp, mib * 1024UL * 1024UL);
    close(pfd);

    /* Phase 2: reopen MAP_PRIVATE and warm the private mapping (first traversal,
     * not counted), then loop iters times for the steady-state second-touch
     * profile window. full_page matches the customer口径: seq = full 4KiB page,
     * rand = one 8-byte slot per page. */
    int mfd = -1;
    void *p = open_ws(mib, 0, is_write, backing, 0, &mfd);
    if (!p) {
        return 1;
    }
    uint32_t *order = NULL;
    uint32_t *off = NULL;
    build_plan(pages, is_random, &order, &off);
    if (!order || !off) {
        fprintf(stderr, "out of memory\n");
        munmap(p, mib * 1024UL * 1024UL);
        close(mfd);
        free(order);
        free(off);
        return 1;
    }
    char *base = (char *)p;
    int full_page = !is_random;
    /* first traversal: faults the private pages in (excluded from the loop) */
    if (is_write) {
        traverse_write(base, pages, order, off, full_page);
    } else {
        traverse_read(base, pages, order, off, full_page);
    }
    /* steady-state second-touch loop — this is the window to profile */
    double t0 = now_ms();
    for (long i = 0; i < iters; i++) {
        if (is_write) {
            traverse_write(base, pages, order, off, full_page);
        } else {
            traverse_read(base, pages, order, off, full_page);
        }
    }
    double total_ms = now_ms() - t0;
    printf("STRESS %s iters=%ld total_ms=%.6f per_iter_ms=%.6f pages=%zu mib=%zu\n",
           mode, iters, total_ms, total_ms / (double)iters, pages, mib);

    munmap(p, mib * 1024UL * 1024UL);
    close(mfd);
    free(order);
    free(off);
    return 0;
}

static int do_cleanup(const char *backing) {
    if (backing_is_file(backing)) {
        unlink(FILE_PATH);
    } else {
        shm_unlink(SHM_NAME);
    }
    printf("CLEANED backing=%s\n", backing);
    return 0;
}

static void usage(void) {
    fprintf(stderr,
            "usage:\n"
            "  latbench populate <mib> [shm|file]\n"
            "  latbench measure  <mib> <seq_read|seq_write|rand_read|rand_write> [shm|file]\n"
            "  latbench stress   <mib> <mode> <iters> [shm|file]   (resident-traversal profile loop)\n"
            "  latbench cleanup  [shm|file]\n");
}

int main(int argc, char **argv) {
    if (argc < 2) {
        usage();
        return 2;
    }
    const char *cmd = argv[1];

    if (strcmp(cmd, "help") == 0 || strcmp(cmd, "--help") == 0 || strcmp(cmd, "-h") == 0) {
        usage();
        return 0;
    }

    if (strcmp(cmd, "cleanup") == 0) {
        const char *backing = (argc >= 3) ? argv[2] : "shm";
        return do_cleanup(backing);
    }

    if (strcmp(cmd, "populate") == 0) {
        if (argc < 3) {
            usage();
            return 2;
        }
        size_t mib = (size_t)strtoul(argv[2], NULL, 10);
        const char *backing = (argc >= 4) ? argv[3] : "shm";
        if (mib == 0) {
            fprintf(stderr, "mib must be > 0\n");
            return 2;
        }
        return do_populate(mib, backing);
    }

    if (strcmp(cmd, "measure") == 0) {
        if (argc < 4) {
            usage();
            return 2;
        }
        size_t mib = (size_t)strtoul(argv[2], NULL, 10);
        const char *mode = argv[3];
        const char *backing = (argc >= 5) ? argv[4] : "shm";
        if (mib == 0) {
            fprintf(stderr, "mib must be > 0\n");
            return 2;
        }
        return do_measure(mib, mode, backing);
    }

    if (strcmp(cmd, "stress") == 0) {
        if (argc < 5) {
            usage();
            return 2;
        }
        size_t mib = (size_t)strtoul(argv[2], NULL, 10);
        const char *mode = argv[3];
        long iters = strtol(argv[4], NULL, 10);
        const char *backing = (argc >= 6) ? argv[5] : "shm";
        if (mib == 0) {
            fprintf(stderr, "mib must be > 0\n");
            return 2;
        }
        return do_stress(mib, mode, iters, backing);
    }

    usage();
    return 2;
}
