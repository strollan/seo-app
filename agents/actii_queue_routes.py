"""Route helpers kept separate from main.py's application startup side effects."""
import logging
import os
import sqlite3

from fastapi.responses import JSONResponse

from agents.actii_file_store import StorageError
from agents.actii_queue_agent import ImportQueue, QueueError, configured_queue

logger = logging.getLogger(__name__)


def respond(operation, *, accepted=False, local=False):
    try:
        if local:
            if os.getenv("ACTII_CRM_MODE", "disabled") != "local" or not os.getenv("ACTII_CONNECTOR_BINDINGS_FILE"):
                raise QueueError("Local CRM account binding is not configured.")
            queue = ImportQueue()
        else:
            queue = configured_queue()
        result = operation(queue)
        return JSONResponse(result, status_code=202 if accepted else 200,
                            headers={"Cache-Control": "no-store, private"})
    except QueueError as error:
        # Deliberately exclude credentials, prospect data and arbitrary exception text.
        logger.warning("actii_queue_rejected status=%s", error.status)
        return JSONResponse({"ok": False, "error": str(error)}, status_code=error.status,
                            headers={"Cache-Control": "no-store, private"})
    except (OSError, StorageError, sqlite3.Error, ValueError, KeyError, TypeError):
        logger.error("actii_queue_storage_or_integrity_failure")
        return JSONResponse({"ok": False, "error": "CRM queue requires operator review."}, status_code=503,
                            headers={"Cache-Control": "no-store, private"})


async def limited_json(request, limit):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise QueueError("CRM request is too large.", 413)
    import json
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise QueueError("Invalid CRM request.", 400) from None
