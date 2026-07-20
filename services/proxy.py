"""Reverse proxy for /live/{slug}/* — mounted by the MAIN app in embedded
single-service mode (when RUNNER_SERVICE_URL is unset and the runner lives
inside this very process).

The handlers are imported VERBATIM from runner.app and only re-registered on
this router: the /live gateway is the most protected, highest-value piece of
the product, so there must be exactly ONE implementation of it — never a
forked copy that can drift. Two-service mode simply doesn't include this
router (the gateway stays on the runner service, unchanged).
"""
from fastapi import APIRouter

import runner.app as _runner

router = APIRouter()

router.api_route("/live/{slug}", methods=_runner._LIVE_METHODS, include_in_schema=False)(_runner.live_http)
router.api_route("/live/{slug}/{full_path:path}", methods=_runner._LIVE_METHODS, include_in_schema=False)(_runner.live_http)
router.websocket("/live/{slug}")(_runner.live_ws)
router.websocket("/live/{slug}/{full_path:path}")(_runner.live_ws)
