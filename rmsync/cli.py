"""rmsync: two paths to the tablet.

  rmsync md  init|push|pull ...   markdown <-> native reMarkable text (editable on the tablet)
  rmsync pdf init|push|pull ...   Typst -> PDF on the tablet, annotations pulled back as SVG overlays
"""
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "md":
        from . import sync
        return sync.main(argv[1:])
    if argv and argv[0] == "pdf":
        from . import pdfsync
        return pdfsync.main(argv[1:])
    sys.exit(__doc__)
