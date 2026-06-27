"""
E2B Bench Module Entry Point

Supports both single benchmark and batch test modes.
"""

import sys
import argparse


def main():
    """Main entry point"""
    # Check if --batch is in args before parsing
    if '--batch' in sys.argv:
        # Batch mode: delegate to batch_scheduler
        from .batch_scheduler import build_arg_parser, BatchScheduler

        batch_parser = build_arg_parser()
        # Remove '--batch' from args for batch parser
        batch_args = sys.argv[1:]
        if '--batch' in batch_args:
            batch_args.remove('--batch')
        batch_args = batch_parser.parse_args(batch_args)

        scheduler = BatchScheduler(
            matrix_path=batch_args.matrix,
            template_path=batch_args.template,
            output_dir=batch_args.output_dir
        )

        report_path = scheduler.run(continue_on_failure=batch_args.continue_on_failure)
        print(f"\nDone. Report: {report_path}")
    else:
        # Single benchmark mode
        from .bench import main as bench_main
        bench_main()


if __name__ == '__main__':
    main()