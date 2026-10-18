"""Game mod catalog and GitHub release announcements for Discord."""
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from pathlib import Path

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from auth_store import AuthStore
from pairing import Pairing, TTL

ROOT = Path(__file__).parent
LOG = logging.getLogger("game_mod_bot")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def env_id(key):
    value = os.getenv(key, "").strip()
    if value and not value.isdecimal():
        raise ValueError(f"{key} must be a numeric Discord ID")
    return int(value) if value else None


def load_mods(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) > 10:
        raise ValueError("mods.json must be an array of at most ten mods")
    mods, names = [], set()
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError("Each mod must be an object")
        name, game, repo = (entry.get(key) for key in ("name", "game", "repo"))
        if not all(isinstance(x, str) and x.strip() for x in (name, game, repo)):
            raise ValueError("Each mod needs name, game and repo strings")
        if len(name) > 50 or len(game) > 50 or not REPO.fullmatch(repo):
            raise ValueError(f"Invalid mod entry: {name}")
        if name.casefold() in names:
            raise ValueError(f"Duplicate mod name: {name}")
        names.add(name.casefold())
        mods.append({"name": name.strip(), "game": game.strip(), "repo": repo})
    return mods


def latest_release(repo):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "discord-game-mod-bot"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError(f"GitHub returned HTTP {error.code}") from error


def release_embed(mod, release):
    url = str(release.get("html_url") or f"https://github.com/{mod['repo']}")
    embed = discord.Embed(
        title=f"{mod['name']} · {release.get('tag_name') or 'new release'}"[:256],
        url=url,
        description=str(release.get("body") or "Release notes on GitHub")[:1800],
        color=0x5865F2,
    )
    embed.add_field(name="Game", value=mod["game"])
    for asset in (release.get("assets") or [])[:3]:
        label = discord.utils.escape_markdown(str(asset.get("name") or "Asset"))
        link = asset.get("browser_download_url") or url
        embed.add_field(name="Download", value=f"[{label}]({link})", inline=False)
    return embed


class GameModBot(commands.Bot):
    def __init__(self, mods, guild_id, channel_id):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.mods, self.guild_id, self.channel_id = mods, guild_id, channel_id
        self.db = sqlite3.connect(ROOT / "bot.db")
        self.db.execute("CREATE TABLE IF NOT EXISTS releases (repo TEXT PRIMARY KEY, release_id TEXT NOT NULL)")
        self.db.commit()
        self.pairing = Pairing(self.db)
        self.auth = AuthStore(self.db)
        self.web_runner = None
        self.attempts = defaultdict(deque)

    def web_app(self):
        app = web.Application(client_max_size=1024)

        async def health(request):
            return web.json_response({"status": "ok"})

        async def demo(request):
            response = web.Response(text="""<!doctype html>
<html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Game link test</title>
<style>body{font:18px system-ui;background:#161b22;color:#eee;max-width:36rem;margin:3rem auto;padding:1rem}
button{font:inherit;padding:.7rem 1rem;border:0;border-radius:.5rem;background:#5865f2;color:white}
#code{font-size:2.5rem;letter-spacing:.2em;font-weight:bold}</style></head>
<body><h1>Discord link test</h1><p>This tests the one-time code. No game credentials are requested.</p>
<button id="begin">Generate code</button><p id="code"></p><p id="status"></p>
<script>
const begin=document.getElementById("begin"), code=document.getElementById("code"),
 status=document.getElementById("status");
begin.onclick=async()=>{
 begin.disabled=true; status.textContent="Starting…";
 try{
  const r=await fetch("/api/link/start",{method:"POST"});
  if(!r.ok)throw Error("Could not generate code ("+r.status+")");
  const data=await r.json();
  code.textContent=data.code;
  status.textContent="In your Discord server, type /link and enter this code. It expires in 10 minutes.";
  const deadline=Date.now()+data.expires_in*1000;
  while(Date.now()<deadline){
   await new Promise(resolve=>setTimeout(resolve,2000));
   const result=await fetch("/api/link/status",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({ticket:data.ticket})});
   if(!result.ok)throw Error("Status check failed ("+result.status+")");
   const state=await result.json();
   if(state.status==="linked"){status.textContent="Account linked.";code.textContent="";return}
   if(state.status==="invalid")break;
  }
  status.textContent="Code expired. Generate a new one.";code.textContent="";
 }catch(error){status.textContent=error.message}
 finally{begin.disabled=false}
};
</script></body></html>""", content_type="text/html")
            response.headers["Cache-Control"] = "no-store"
            return response

        async def start(request):
            ip = request.remote or "unknown"
            now = asyncio.get_running_loop().time()
            attempts = self.attempts[ip]
            while attempts and now - attempts[0] > 60:
                attempts.popleft()
            if len(attempts) >= 30:
                raise web.HTTPTooManyRequests(text="Try again in a minute")
            attempts.append(now)
            code, ticket = self.pairing.start()
            base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
            response = web.json_response({
                "code": code, "ticket": ticket, "expires_in": TTL,
                "browser_url": f"{base}/link/code/{code}" if base else f"/link/code/{code}",
            })
            response.headers["Cache-Control"] = "no-store"
            return response

        async def code_page(request):
            code = request.match_info["code"]
            if not re.fullmatch(r"[0-9]{8}", code):
                raise web.HTTPNotFound()
            row = self.db.execute(
                "SELECT expires, discord_id FROM pairing WHERE code = ?", (code,)
            ).fetchone()
            if not row or row[0] <= time.time():
                raise web.HTTPNotFound()
            status = "Account linked. You can return to the game." if row[1] else (
                f"Use /link code:{code} in your Discord server, then return to the game."
            )
            response = web.Response(
                text=f"<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
                     f"<title>Link game</title></head><body><h1>Link game</h1><p>{status}</p></body></html>",
                content_type="text/html",
            )
            response.headers["Cache-Control"] = "no-store"
            return response

        async def status(request):
            try:
                body = await request.json()
            except (ValueError, web.HTTPException):
                raise web.HTTPBadRequest(text="Expected JSON") from None
            ticket = body.get("ticket", "") if isinstance(body, dict) else ""
            if not isinstance(ticket, str) or not ticket:
                raise web.HTTPBadRequest(text="Missing ticket")
            response = web.json_response(self.pairing.poll(ticket))
            response.headers["Cache-Control"] = "no-store"
            return response

        async def device(request):
            try:
                body = await request.json()
            except (ValueError, web.HTTPException):
                raise web.HTTPBadRequest(text="Expected JSON") from None
            token = body.get("device_token") if isinstance(body, dict) else None
            result = (self.pairing.revoke(token) if request.path.endswith("/revoke")
                      else self.pairing.verify(token))
            response = web.json_response(
                {"status": "revoked" if result else "invalid"}
                if request.path.endswith("/revoke") else result
            )
            response.headers["Cache-Control"] = "no-store"
            return response

        async def auth_selection(request):
            try:
                body = await request.json()
            except (ValueError, web.HTTPException):
                raise web.HTTPBadRequest(text="Expected JSON") from None
            token = body.get("device_token") if isinstance(body, dict) else None
            linked = self.pairing.verify(token)
            if linked["status"] != "linked":
                raise web.HTTPUnauthorized(text="Invalid device token")
            response = web.json_response(self.auth.selection(linked["discord_id"]))
            response.headers["Cache-Control"] = "no-store"
            return response

        app.router.add_get("/health", health)
        app.router.add_get("/link/demo", demo)
        app.router.add_post("/api/link/start", start)
        app.router.add_get("/link/code/{code}", code_page)
        app.router.add_post("/api/link/status", status)
        app.router.add_post("/api/link/verify", device)
        app.router.add_post("/api/link/revoke", device)
        app.router.add_post("/api/auth/selection", auth_selection)
        return app

    async def setup_hook(self):
        port = int(os.getenv("SERVER_PORT", "8080"))
        self.web_runner = web.AppRunner(self.web_app())
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, "0.0.0.0", port)
        await site.start()
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        if self.mods and self.channel_id:
            self.watch_releases.start()

    async def close(self):
        if self.watch_releases.is_running():
            self.watch_releases.cancel()
        await super().close()
        if self.web_runner:
            await self.web_runner.cleanup()
        self.db.close()

    @tasks.loop(minutes=15)
    async def watch_releases(self):
        channel = None
        for mod in self.mods:
            try:
                release = await asyncio.to_thread(latest_release, mod["repo"])
                if not release:
                    continue
                current = str(release["id"])
                old = self.db.execute("SELECT release_id FROM releases WHERE repo = ?", (mod["repo"],)).fetchone()
                if old and old[0] == current:
                    continue
                # First check records the current version without announcing an old release.
                if old:
                    channel = channel or self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
                    if not isinstance(channel, discord.abc.Messageable):
                        raise RuntimeError("Release channel cannot receive messages")
                    await channel.send(embed=release_embed(mod, release), allowed_mentions=discord.AllowedMentions.none())
                self.db.execute(
                    "INSERT INTO releases (repo, release_id) VALUES (?, ?) ON CONFLICT(repo) DO UPDATE SET release_id = excluded.release_id",
                    (mod["repo"], current),
                )
                self.db.commit()
            except (OSError, RuntimeError, discord.HTTPException, KeyError, ValueError) as error:
                LOG.warning("Release check failed for %s: %s", mod["repo"], error)

    @watch_releases.before_loop
    async def before_watch_releases(self):
        await self.wait_until_ready()


def main():
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "put-your-bot-token-here":
        raise SystemExit("Set DISCORD_TOKEN in the host environment")
    bot = GameModBot(load_mods(ROOT / "mods.json"), env_id("GUILD_ID"), env_id("RELEASE_CHANNEL_ID"))

    def linked(user_id: int) -> bool:
        return bot.db.execute(
            "SELECT 1 FROM game_devices WHERE discord_id = ? LIMIT 1", (str(user_id),)
        ).fetchone() is not None

    class TokenModal(discord.ui.Modal, title="Store Gorilla Tag session tokens"):
        bearer = discord.ui.TextInput(label="Bearer", style=discord.TextStyle.paragraph,
                                      min_length=16, max_length=4000)
        refresh = discord.ui.TextInput(label="Refresh", style=discord.TextStyle.paragraph,
                                       min_length=16, max_length=4000)

        async def on_submit(self, interaction: discord.Interaction):
            if not linked(interaction.user.id):
                await interaction.response.send_message("Link a game device with /link first.", ephemeral=True)
                return
            try:
                slot = bot.auth.add(interaction.user.id, str(self.bearer), str(self.refresh))
            except (RuntimeError, ValueError) as error:
                await interaction.response.send_message(str(error), ephemeral=True)
                return
            await interaction.response.send_message(
                f"Stored as token #{slot}. Authentication and refresh are not verified yet.", ephemeral=True
            )

    auto = app_commands.Group(name="auto", description="Manage game-session token slots")
    use = app_commands.Group(name="use", description="Select a game-session token slot")

    @auto.command(name="refresh_token", description="Privately store bearer and refresh tokens")
    @app_commands.guild_only()
    async def refresh_token(interaction: discord.Interaction):
        if not linked(interaction.user.id):
            await interaction.response.send_message("Link a game device with /link first.", ephemeral=True)
            return
        try:
            bot.auth.require_key()
        except RuntimeError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.send_modal(TokenModal())

    @auto.command(name="tokens", description="List your stored token slots")
    @app_commands.guild_only()
    async def tokens(interaction: discord.Interaction):
        slots, selected = bot.auth.list_slots(interaction.user.id)
        message = ("Slots: " + ", ".join(f"#{n}{' (selected)' if n == selected else ''}" for n in slots)) if slots else "No token slots stored."
        await interaction.response.send_message(message, ephemeral=True)

    @auto.command(name="remove_token", description="Delete one stored token slot")
    @app_commands.guild_only()
    async def remove_token(interaction: discord.Interaction, slot: int):
        removed = bot.auth.delete(interaction.user.id, slot)
        await interaction.response.send_message("Token slot deleted." if removed else "No such token slot.", ephemeral=True)

    @use.command(name="auto_refresh_token", description="Select a stored token for your linked game device")
    @app_commands.guild_only()
    async def use_token(interaction: discord.Interaction, slot: int):
        if not linked(interaction.user.id):
            await interaction.response.send_message("Link a game device with /link first.", ephemeral=True)
            return
        try:
            bot.auth.select(interaction.user.id, slot)
        except (RuntimeError, ValueError) as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Selected token #{slot} for linked devices. Game authentication is pending verification.",
            ephemeral=True,
        )

    bot.tree.add_command(auto)
    bot.tree.add_command(use)

    @bot.tree.command(name="mods", description="List available game mods")
    @app_commands.guild_only()
    async def mods(interaction: discord.Interaction, game: str | None = None):
        entries = [item for item in bot.mods if not game or game.casefold() in item["game"].casefold()]
        if not entries:
            await interaction.response.send_message("No mods found. Add entries to mods.json.", ephemeral=True)
            return
        embed = discord.Embed(title="Game mods", color=0x5865F2)
        for item in entries:
            embed.add_field(name=item["name"], value=f"{item['game']} · [GitHub](https://github.com/{item['repo']})", inline=False)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name="mod", description="Get the latest release and downloads for a mod")
    @app_commands.guild_only()
    async def mod(interaction: discord.Interaction, name: str):
        item = next((x for x in bot.mods if x["name"].casefold() == name.casefold()), None)
        if item is None:
            await interaction.response.send_message("Unknown mod. Use /mods.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            release = await asyncio.to_thread(latest_release, item["repo"])
        except (OSError, RuntimeError) as error:
            await interaction.followup.send(f"GitHub lookup failed: {error}", ephemeral=True)
            return
        if release is None:
            await interaction.followup.send(f"No published release yet: https://github.com/{item['repo']}", ephemeral=True)
        else:
            await interaction.followup.send(embed=release_embed(item, release), allowed_mentions=discord.AllowedMentions.none())

    @mod.autocomplete("name")
    async def mod_autocomplete(interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=x["name"], value=x["name"])
                for x in bot.mods if current.casefold() in x["name"].casefold()][:10]

    @bot.tree.command(name="link", description="Link your Discord account with the game code")
    @app_commands.guild_only()
    async def link(interaction: discord.Interaction, code: str):
        if bot.pairing.link(code.strip(), interaction.user.id):
            await interaction.response.send_message("Account linked. Return to the game.", ephemeral=True)
        else:
            await interaction.response.send_message("Invalid, expired, or already used code.", ephemeral=True)

    @bot.tree.command(name="unlink", description="Unlink your Discord account from the game")
    @app_commands.guild_only()
    async def unlink(interaction: discord.Interaction):
        count = bot.pairing.unlink_discord(interaction.user.id)
        bot.auth.clear(interaction.user.id)
        if count:
            message = (f"Unlinked {count} game device{'s' if count != 1 else ''}. "
                       "Restart the game to get a new linking code.")
        else:
            message = ("No linked game devices were found for your Discord account. "
                       "Any pending code was canceled.")
        await interaction.response.send_message(message, ephemeral=True)

    @bot.tree.error
    async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        LOG.error("Command error: %s", error)
        message = "Command failed. Check the bot logs."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    bot.run(token)


if __name__ == "__main__":
    main()
