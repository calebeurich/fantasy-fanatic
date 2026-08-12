import sys

from . import DEFAULT_MAX_PER_POSITION, main

owner_arg = sys.argv[2] if len(sys.argv) > 2 else None
limit_arg = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_MAX_PER_POSITION
main(sys.argv[1], owner_arg, limit_arg)
