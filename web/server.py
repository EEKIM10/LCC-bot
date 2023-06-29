import asyncio
import ipaddress
import sys

import discord
import os
import httpx
from pathlib import Path
from datetime import datetime, timezone
from hashlib import sha512

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from http import HTTPStatus

from starlette.websockets import WebSocket, WebSocketDisconnect

from utils import Student, get_or_none, VerifyCode, console, BannedStudentID
from utils.db import AccessTokens
from config import guilds

SF_ROOT = Path(__file__).parent / "static"
if SF_ROOT.exists() and SF_ROOT.is_dir():
    from fastapi.staticfiles import StaticFiles
else:
    StaticFiles = None

try:
    from config import OAUTH_ID, OAUTH_SECRET, OAUTH_REDIRECT_URI
except ImportError:
    OAUTH_ID = OAUTH_SECRET = OAUTH_REDIRECT_URI = None

try:
    from config import WEB_ROOT_PATH
except ImportError:
    WEB_ROOT_PATH = ""

GENERAL = "https://ptb.discord.com/channels/994710566612500550/1018915342317277215/"

OAUTH_ENABLED = OAUTH_ID and OAUTH_SECRET and OAUTH_REDIRECT_URI

app = FastAPI(root_path=WEB_ROOT_PATH)
app.state.bot = None
app.state.states = {}
app.state.http = httpx.Client()

if StaticFiles:
    app.mount("/static", StaticFiles(directory=SF_ROOT), name="static")

try:
    from utils.client import bot
    app.state.bot = bot
except ImportError:
    bot = None


@app.middleware("http")
async def check_bot_instanced(request, call_next):
    if not request.app.state.bot:
        return JSONResponse(
            status_code=503,
            content={"message": "Not ready."},
            headers={
                "Retry-After": "10"
            }
        )
    return await call_next(request)


@app.get("/ping")
def ping():
    bot_started = datetime.now(tz=timezone.utc) - app.state.bot.started_at
    return {
        "ping": "pong", 
        "online": app.state.bot.is_ready(), 
        "latency": max(round(app.state.bot.latency, 2), 0.01),
        "uptime": max(round(bot_started.total_seconds(), 2), 1)
    }


@app.get("/auth")
async def authenticate(req: Request, code: str = None, state: str = None):
    """Begins Oauth flow (browser only)"""
    if not OAUTH_ENABLED:
        raise HTTPException(
            501,
            "OAuth is not enabled."
        )

    if not (code and state) or state not in app.state.states:
        value = os.urandom(4).hex()
        if value in app.state.states:
            print("Generated a state that already exists. Cleaning up", file=sys.stderr)
            # remove any states older than 5 minutes
            removed = 0
            for _value in list(app.state.states):
                if (datetime.now() - app.state.states[_value]).total_seconds() > 300:
                    del app.state.states[_value]
                    removed += 1
            value = os.urandom(4).hex()
            print(f"Removed {removed} states.", file=sys.stderr)

        if value in app.state.states:
            print("Critical: Generated a state that already exists and could not free any slots.", file=sys.stderr)
            raise HTTPException(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Could not generate a state token (state container full, potential (D)DOS attack?). "
                "Please try again later.",
                # Saying a suspected DDOS makes sense, there are 4,294,967,296 possible states, the likelyhood of a
                # collision is 1 in 4,294,967,296.
                headers={
                    "Retry-After": "300"
                }
            )
        app.state.states[value] = datetime.now()
        return RedirectResponse(
            discord.utils.oauth_url(
                OAUTH_ID,
                redirect_uri=OAUTH_REDIRECT_URI,
                scopes=('identify', "connections", "guilds", "email")
            ) + f"&state={value}&prompt=none",
            status_code=HTTPStatus.TEMPORARY_REDIRECT,
            headers={
                "Cache-Control": "no-store, no-cache"
            }
        )
    else:
        app.state.states.pop(state)
        # First, we need to do the auth code flow
        response = app.state.http.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": OAUTH_ID,
                "client_secret": OAUTH_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OAUTH_REDIRECT_URI,
            }
        )
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=response.text
            )
        data = response.json()
        access_token = data["access_token"]
        
        # Now we can generate a token
        token = sha512(access_token.encode()).hexdigest()

        # Now we can get the user's info
        response = app.state.http.get(
            "https://discord.com/api/users/@me",
            headers={
                "Authorization": "Bearer " + data["access_token"]
            }
        )
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=response.text
            )
    
        user = response.json()

        # Now we need to fetch the student from the database
        student = await get_or_none(AccessTokens, user_id=user["id"])
        if not student:
            student = await AccessTokens.objects.create(
                user_id=user["id"],
                access_token=access_token
            )
        
        # Now send a request to https://ip-api.com/json/{ip}?fields=status,city,zip,lat,lon,isp,query
        _host = ipaddress.ip_address(req.client.host)
        if not any((_host.is_loopback, _host.is_private, _host.is_reserved, _host.is_unspecified)):
            response = app.state.http.get(
                f"http://ip-api.com/json/{req.client.host}?fields=status,city,zip,lat,lon,isp,query,proxy,hosting"
            )
            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=response.text
                )
            data = response.json()
            if data["status"] != "success":
                raise HTTPException(
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                    detail=f"Failed to get IP data for {req.client.host}: {data}."
                )
        else:
            data = None
        
        # Now we can update the student entry with this data
        await student.update(ip_info=data, access_token_hash=token)
        document = \
f"""
<!DOCTYPE html>
<html>
<head>
    <title>Redirecting...</title>
</head>
<body>
    <script>
        window.location.href = "{GENERAL}";
    </script>
    <noscript>
        <meta http-equiv="refresh" content="0; url={GENERAL}" />
    </noscript>
    <p>Redirecting you to the general channel...</p>
    <i><a href='{GENERAL}' rel='noopener'>Click here if you are not redirected.</a></i>
</body>
</html>
"""
        # And set it as a cookie
        response = HTMLResponse(
            document,
            status_code=200,
            headers={
                "Location": GENERAL,
                "Cache-Control": "max-age=604800"
            }
        )
        # set the cookie for at most 604800 seconds - expire after that
        response.set_cookie(
            "token",
            token,
            max_age=604800,
            samesite="strict",
            httponly=True,
        )
        return response


@app.get("/verify/{code}")
async def verify(code: str):
    guild = app.state.bot.get_guild(guilds[0])
    if not guild:
        raise HTTPException(
            status_code=503,
            detail="Not ready."
        )

    # First, we need to fetch the code from the database
    verify_code = await get_or_none(VerifyCode, code=code)
    if not verify_code:
        raise HTTPException(
            status_code=404,
            detail="Code not found."
        )

    # Now we need to fetch the student from the database
    student = await get_or_none(Student, user_id=verify_code.bind)
    if student:
        raise HTTPException(
            status_code=400,
            detail="Already verified."
        )

    ban = await get_or_none(BannedStudentID, student_id=verify_code.student_id)
    if ban is not None:
        return await guild.kick(
            reason=f"Attempted to verify with banned student ID {ban.student_id}"
                   f" (originally associated with account {ban.associated_account})"
        )
    await Student.objects.create(
        id=verify_code.student_id, user_id=verify_code.bind, name=verify_code.name
    )
    await verify_code.delete()
    role = discord.utils.find(lambda r: r.name.lower() == "verified", guild.roles)
    member = await guild.fetch_member(verify_code.bind)
    if role and role < guild.me.top_role:
        await member.add_roles(role, reason="Verified")
    try:
        await member.edit(nick=f"{verify_code.name}", reason="Verified")
    except discord.HTTPException:
        pass

    # And delete the code
    await verify_code.delete()

    console.log(f"[green]{verify_code.bind} verified ({verify_code.bind}/{verify_code.student_id})")

    return RedirectResponse(
        GENERAL,
        status_code=308
    )


@app.post("/bridge", include_in_schema=False)
async def bridge(req: Request):
    body = await req.json()
    if body["secret"] != app.state.bot.http.token:
        raise HTTPException(
            status_code=401,
            detail="Invalid secret."
        )

    channel = app.state.bot.get_channel(1032974266527907901)
    if not channel:
        raise HTTPException(
            status_code=404,
            detail="Channel does not exist."
        )

    await channel.send(
        f"**{body['sender']}**:\n>>> {body['message']}"
    )
    return {"status": "ok"}


@app.websocket('/bridge/recv')
async def bridge_recv(ws: WebSocket, secret: str = Header(None)):
    if secret != app.state.bot.http.token:
        raise HTTPException(
            status_code=401,
            detail="Invalid secret."
        )
    queue: asyncio.Queue = app.state.bot.bridge_queue

    await ws.accept()
    while True:
        try:
            data = queue.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0.5)
            continue

        try:
            await ws.send_json(data)
        except WebSocketDisconnect:
            break
        finally:
            queue.task_done()
