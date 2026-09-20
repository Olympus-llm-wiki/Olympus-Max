"""Authenticated HTTP upload/download and MCP; model work belongs to the worker."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import os
from pathlib import Path
import tempfile

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .common import ReelError, read_json
from .contracts import Profile


def public_job(job):
    return {k: job[k] for k in ("id", "asset_id", "state", "created", "updated", "error", "parent_id", "rerun_track")}


def mcp_server(store):
    mcp = MCPServer("reel-analysis", instructions="Submit an already uploaded video by asset ID. Processing continues in a separate sequential worker. Use status then result after completion; task execution is not semantic verification.")

    @mcp.tool()
    async def submit_reel(asset_id: str, profile: dict | None = None, course: list[dict] | None = None) -> dict:
        """Queue a server-side asset. A local path on the client's machine is not an asset."""
        return public_job(await asyncio.to_thread(store.submit, asset_id, Profile(**(profile or {})), course))

    @mcp.tool()
    async def reel_status(job_id: str) -> dict:
        """Read execution status; this does not call Gemini."""
        return public_job(store.get(job_id))

    @mcp.tool()
    async def reel_result(job_id: str) -> dict:
        """Get locations of the completed report, or fail if it is not ready."""
        await asyncio.to_thread(store.result, job_id)
        return {"job_id": job_id, "json": f"/jobs/{job_id}/artifacts/analysis.json", "html": f"/jobs/{job_id}/artifacts/review.html", "authorization": "same bearer token as this service"}

    @mcp.tool()
    async def rerun_reel(job_id: str, track: str, profile: dict | None = None) -> dict:
        """Create a version for speech, visual or all; preserve old results."""
        return public_job(await asyncio.to_thread(store.rerun, job_id, track, Profile(**profile) if profile else None))

    return mcp


class Auth:
    def __init__(self, app, token):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            auth = headers.get(b"authorization", b"")
            if not hmac.compare_digest(auth, ("Bearer " + self.token).encode("ascii")):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
            # MCP/JSON requests are bounded too; upload has its own streaming limit.
            limit = 110 * 1024 * 1024 if scope["path"] == "/assets" else 1024 * 1024
            size = 0
            async def bounded():
                nonlocal size
                message = await receive()
                size += len(message.get("body", b""))
                if size > limit:
                    raise ReelError("request_too_large")
                return message
            await self.app(scope, bounded, send)
        else:
            await self.app(scope, receive, send)


def create_app(store, token=None, allowed_hosts=None):
    token = token or os.environ.get("REEL_ACCESS_TOKEN")
    if not token or len(token) < 24 or not token.isascii():
        raise ReelError("set_strong_reel_access_token")
    hosts = allowed_hosts or os.environ.get("REEL_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if "*" in hosts:
        raise ReelError("explicit_server_hosts_required")
    mcp = mcp_server(store)
    security = TransportSecuritySettings(allowed_hosts=[v for h in hosts for v in (h, h + ":*")], allowed_origins=[v for h in hosts for v in ("https://" + h, "http://" + h, "http://" + h + ":*")])
    mcp_app = mcp.streamable_http_app(stateless_http=True, transport_security=security)

    async def error(request, exc):
        code = str(exc) if isinstance(exc, ReelError) else "invalid_request"
        return JSONResponse({"error": code}, status_code=400)

    async def upload(request: Request):
        length = request.headers.get("content-length", "")
        expected = request.headers.get("x-content-sha256", "")
        if not length.isdigit() or not 0 < int(length) <= Profile().max_bytes or len(expected) != 64 or any(x not in "0123456789abcdef" for x in expected):
            raise ReelError("length_and_sha256_required")
        if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
            raise ReelError("binary_file_body_required")
        fd, name = tempfile.mkstemp(dir=store.root / "incoming", suffix=".video")
        path, size, h = Path(name), 0, hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > int(length):
                        raise ReelError("upload_length_mismatch")
                    h.update(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            if size != int(length) or h.hexdigest() != expected:
                raise ReelError("upload_hash_or_length_mismatch")
            asset = await asyncio.to_thread(store.ingest, path)
            return JSONResponse({"asset_id": asset["id"], "sha256": asset["sha256"], "duration_s": asset["duration_s"]}, status_code=201)
        finally:
            path.unlink(missing_ok=True)

    async def submit(request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ReelError("object_body_required")
        if set(data) - {"asset_id", "profile", "course"}:
            raise ReelError("unknown_submission_field")
        job = await asyncio.to_thread(store.submit, data["asset_id"], Profile(**data.get("profile", {})), data.get("course"))
        return JSONResponse(public_job(job), status_code=202)

    async def status(request):
        return JSONResponse(public_job(store.get(request.path_params["job_id"])))

    async def artifact(request):
        folder = await asyncio.to_thread(store.result, request.path_params["job_id"])
        name = request.path_params["name"]
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ReelError("invalid_artifact")
        file = folder / name
        if name != "manifest.json" and name not in read_json(folder / "manifest.json")["files"]:
            raise ReelError("artifact_not_found")
        if not file.is_file() or file.is_symlink() or folder.resolve() not in file.resolve().parents:
            raise ReelError("artifact_not_found")
        return FileResponse(file, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})

    async def limits(request):
        return JSONResponse({"max_bytes": Profile().max_bytes, "max_duration_s": Profile().max_duration_s, "input": "binary upload", "parallel_jobs": 1})

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    routes = [Route("/limits", limits), Route("/assets", upload, methods=["POST"]), Route("/jobs", submit, methods=["POST"]), Route("/jobs/{job_id}", status), Route("/jobs/{job_id}/artifacts/{name:path}", artifact), Mount("/", app=mcp_app)]
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={ReelError: error, ValueError: error, KeyError: error})
    return TrustedHostMiddleware(Auth(app, token), allowed_hosts=hosts)
