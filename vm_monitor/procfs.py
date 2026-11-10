"""Minimal /proc readers for the hot per-VM sampling path.

psutil's ``process_iter(["pid", "name", "cmdline", "status"])`` builds a
Process object and reads the command line of EVERY process on the host --
measured 590 ms per discovery cycle at 6.8k processes, 275 ms at 3.2k. The
monitor only needs three fields, so these open the one file each field lives
in. They never raise: a process that vanished mid-scan returns None.
"""
from __future__ import annotations

# /proc/<pid>/stat state letter -> psutil's status string. Same table psutil
# uses (psutil._pslinux.PROC_STATUSES), kept here so a candidate carries the
# exact string the rest of the monitor (and every existing report) expects.
PROC_STATUSES = {
    "R": "running",
    "S": "sleeping",
    "D": "disk-sleep",
    "T": "stopped",
    "t": "tracing-stop",
    "Z": "zombie",
    "X": "dead",
    "x": "dead",
    "K": "wake-kill",
    "W": "waking",
    "I": "idle",
    "P": "parked",
}


def read_comm_state(pid: int) -> tuple[str, str, int] | None:
    """(comm, state letter, start time) from /proc/<pid>/stat; None if unreadable.

    comm is field 2 in parens -- the kernel caps it at 15 chars, exactly what
    psutil.Process.name() returns -- and state is field 3. rsplit on the LAST
    ')' so a comm containing ')' cannot shift the field positions. Field 22
    (starttime, jiffies since boot) identifies the *process*, not the pid: a
    recycled pid has a different start time, which is how caches downstream
    tell a restarted VM from the dead one that used the same pid.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            head, rest = f.read().rsplit(")", 1)
        fields = rest.split()
        return head.rsplit("(", 1)[1], fields[0], int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def read_cmdline(pid: int) -> str | None:
    """argv of ``pid`` joined like psutil.Process.cmdline(); None if unreadable.

    Accepts both the NUL-separated form 'man proc' specifies and the
    space-separated form some processes rewrite in place.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().rstrip(b"\x00").replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return None
