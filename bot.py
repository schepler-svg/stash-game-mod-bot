"""Game mod catalog and GitHub release announcements for Discord."""
import asyncio
import json
import logging
import os
import re
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

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

    async def setup_hook(self):
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
