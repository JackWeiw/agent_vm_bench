"""In-image benchmark looper for the browser / coding-go / coding-ts scenarios.

This package moves the host-side E2B-API single-sandbox drivers
(e2b_bench/task_runner.py for the browser workflow, e2b_bench/coding_task_runner.py
for the coding workflows) into the image, so a container runs one scenario
end-to-end via a single entry point. It mirrors the document-bench in-image
looper: a baked operations config + a Python loop that replaces the E2B
Files/Commands API with local subprocess / Path calls.

Installed layout (each of the three images vendors this package):
    /opt/bench-looper/
        bench_looper/
            core.py          loop control, timing, JSON results
            coding_base.py   shared find->read->edit->verify->diff skeleton
            browser.py        browser scenario plugin
            coding_go.py      coding (Go) scenario plugin
            coding_ts.py      coding (TypeScript) scenario plugin
            runner.py         CLI entry point (scenario dispatch)
            operations/       baked scenario configs (urls, pairs, templates)

Entry points (created by the Dockerfiles as /usr/local/bin shims):
    browser-bench       -> python3 runner.py browser   "$@"
    coding-bench-go     -> python3 runner.py coding-go "$@"
    coding-bench-ts     -> python3 runner.py coding-ts "$@"

Default CMD remains `sleep infinity` (long-running container for slicing
attachment); the entry points run one scenario end-to-end and exit.
"""
