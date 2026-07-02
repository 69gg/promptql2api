"""支持 ``python -m registrar`` 调用。"""
from __future__ import annotations

from .cli import main

raise SystemExit(main())
