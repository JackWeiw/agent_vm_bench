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
 * populate=0 -> open existing + MAP_PRIVATE (so writes COW from the snapshot). */
static void *open_ws(size_t mib, int populate, int write_mode, const char *backing,
                     int *out_fd) {
    size_t size = mib * 1024UL * 1024UL;
    int fd;
    void *p;
    int prot = write_mode ? (PROT_READ | PROT_WRITE) : PROT_READ;
    int flags = populate ? MAP_SHARED : MAP_PRIVATE;

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
    void *p = open_ws(mib, 1, 1, backing, &fd);
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
    volatile uint64_t sink = 0;
    double t0 = now_ms();
    if (full_page) {
        /* Sequential: touch every 8-byte slot of every 4KiB page (full-page access),
         * pages in linear order. The first slot of each page triggers the lazy
         * page-in; the remaining 511 slots are within the now-resident page. */
        for (size_t i = 0; i < pages; i++) {
            volatile uint64_t *page = (volatile uint64_t *)(base + i * PAGE);
            for (size_t s = 0; s < PAGE / SLOT; s++) {
                sink |= page[s];
            }
        }
    } else {
        /* Random: one 8-byte slot per page, in shuffled page order. Each page is
         * touched exactly once, so first pass = one fault per page. */
        for (size_t i = 0; i < pages; i++) {
            sink |= *((volatile uint64_t *)(base + (size_t)order[i] * PAGE + off[i]));
        }
    }
    double t1 = now_ms();
    (void)sink; /* prevent dead-code elimination */
    return t1 - t0;
}

static double traverse_write(char *base, size_t pages, const uint32_t *order,
                             const uint32_t *off, int full_page) {
    double t0 = now_ms();
    if (full_page) {
        /* Sequential full-page write; first write to a page COWs it from the
         * snapshot, the remaining slots write the now-private resident page. */
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
    void *p = open_ws(mib, 0, is_write, backing, &fd);
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

    usage();
    return 2;
}
