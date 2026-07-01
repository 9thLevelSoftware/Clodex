from __future__ import annotations

import runpy
import sys


def main() -> None:
    sys.argv[0] = "clodex"
    try:
        runpy.run_module("clodex", run_name="__main__", alter_sys=True)
    except SystemExit:
        raise
    raise SystemExit(0)


if __name__ == "__main__":
    main()
