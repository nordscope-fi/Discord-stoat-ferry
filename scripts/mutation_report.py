"""Report cosmic-ray survivors with the tests that ran against each.

A count is not a report. `lesson-a-mutation-harness-reports-shape-not-just-pass`
records the reason: a mutant that kills fewer, or different, tests than expected
is itself the finding, and a harness printing only KILLED or SURVIVED hides it.
So this prints test names.

This is a query over cosmic-ray's documented API, not a mutation engine. That
split is why cosmic-ray was chosen over writing one: `WorkResult` carries the
captured test output and `WorkDB.completed_work_items` is documented, while
mutmut records only killed/survived status in `.meta` files.

Usage:  uv run --extra mutation python scripts/mutation_report.py <session.sqlite>
Exit 0  the run completed and no mutant survived
Exit 1  survivors found (triage them; a survivor is not always a delete instruction)
Exit 2  the session is incomplete or unreadable, so no report is possible
"""

from __future__ import annotations

import re
import sys

TEST_NAME = re.compile(r"(tests/[\w/]+\.py::[\w\[\]-]+)")


def _tests_named(output: str | None) -> list[str]:
    """Test node ids visible in a mutant's captured pytest output.

    Requires `-v` in the configured test command. Without it pytest prints dots
    and this returns nothing, which is why the config comments call `-v` load
    bearing rather than cosmetic.
    """
    if not output:
        return []
    return sorted({m.group(1) for m in TEST_NAME.finditer(output)})


def main(path: str) -> int:
    try:
        from cosmic_ray.work_db import WorkDB, use_db
        from cosmic_ray.work_item import TestOutcome
    except ImportError:
        print(
            "mutation_report: cosmic-ray is not installed.\n  Run: uv sync --extra mutation",
            file=sys.stderr,
        )
        return 2

    with use_db(path, WorkDB.Mode.open) as db:
        pending = len(db.pending_work_items)
        if pending:
            # Refusing beats under-reporting. A partial run presented as complete
            # understates survivors, and understating them is the direction that
            # matters: it turns an unfinished sweep into a clean bill of health.
            print(
                f"mutation_report: REFUSING to report, {pending} work items still pending.\n"
                "  A partial run reported as complete understates survivors.",
                file=sys.stderr,
            )
            return 2

        total = db.num_work_items
        survivors = [
            (item, result)
            for item, result in db.completed_work_items
            if result.test_outcome == TestOutcome.SURVIVED
        ]

        print(f"Mutants: {total}   Survivors: {len(survivors)}")
        if not survivors:
            print("Every mutant was killed.")
            return 0

        print()
        for item, result in survivors:
            for mutation in item.mutations:
                print(f"SURVIVED  {mutation.module_path}")
                print(f"  operator   {mutation.operator_name}  occurrence {mutation.occurrence}")
                names = _tests_named(result.output)
                if names:
                    print(f"  tests run  {len(names)}: {', '.join(names[:6])}")
                else:
                    print("  tests run  none named (is -v set in the test command?)")
                if result.diff:
                    first = next(
                        (
                            ln
                            for ln in result.diff.splitlines()
                            if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
                        ),
                        "",
                    )
                    if first:
                        print(f"  change     {first.strip()}")
                print()

        print("A surviving mutant is not always a delete instruction. It can mean")
        print("unreachable by construction, where the line's guard duplicates one that")
        print("already ran. Diagnose, then write the reason into the code.")
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: mutation_report.py <session.sqlite>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
