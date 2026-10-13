"""支持 `python -m tender_notice_cn …`。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
