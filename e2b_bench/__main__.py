"""
E2B Bench Module Entry Point

Supports both single benchmark and batch test modes.
"""

import logging
import sys

from .utils import setup_logging

logger = logging.getLogger(__name__)


def main():
    """Main entry point"""
    # Configure logging before any subsystem prints would have fired.
    setup_logging()

    # Check if --batch is in args before parsing
    if "--batch" in sys.argv:
        # Batch mode: delegate to batch_scheduler
        from .batch_scheduler import BatchScheduler, build_arg_parser, offline_summary

        batch_parser = build_arg_parser()
        # Remove '--batch' from args for batch parser
        batch_args = sys.argv[1:]
        if "--batch" in batch_args:
            batch_args.remove("--batch")
        batch_args = batch_parser.parse_args(batch_args)

        # Offline mode: generate summary from existing results
        if batch_args.offline:
            if not batch_args.result_dir:
                logger.error("--result-dir is required for offline mode")
                return
            report_path = offline_summary(batch_args.result_dir, batch_args.output)
            if report_path:
                logger.info(f"Done. Report: {report_path}")
            return

        # Online mode: run batch tests
        if not batch_args.matrix:
            logger.error("--matrix is required for online mode")
            return

        scheduler = BatchScheduler(matrix_path=batch_args.matrix)
        report_path = scheduler.run(continue_on_failure=batch_args.continue_on_failure)
        logger.info(f"Done. Report: {report_path}")
    else:
        # Single benchmark mode
        from .bench import main as bench_main

        bench_main()


if __name__ == "__main__":
    main()
