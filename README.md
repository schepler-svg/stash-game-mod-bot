# Game mod Discord bot

Lists game mods from GitHub, provides release downloads, and announces new versions.

## Commands

- `/mods` lists all mods; `/mods game:<name>` filters by game.
- `/mod name:<name>` shows the latest published release, notes, and asset downloads.

## Setup

1. Edit `mods.json` with up to ten public GitHub repositories. Example:

   ```json
   [
     {"name": "My Texture Pack", "game": "My Game", "repo": "myname/my-texture-pack"},
     {"name": "My Map Pack", "game": "My Game", "repo": "myname/my-map-pack"}
   ]
   ```

   Create a GitHub **Release** with uploaded assets for each version. Commits and tags alone are not releases.
2. Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications). Under **Installation**, add `bot` and `applications.commands` scopes. Give it **View Channels**, **Send Messages**, and **Embed Links**, then install it in the server.
3. Push these files to GitHub. At [bot-hosting.net](https://bot-hosting.net/), create a **Python** deployment from the repository. Set **Entry File (STARTUP_FILE)** to `bot.py`; the host installs `requirements.txt` on startup.
4. Set `DISCORD_TOKEN` in the host environment. Optionally set `GUILD_ID` to the server ID for fast command sync and `RELEASE_CHANNEL_ID` to announce releases every 15 minutes. Never commit the token.
5. Start the deployment and try `/mods`, then `/mod`. The first check records existing releases without announcing old versions.

`bot.db` holds last-seen release IDs. Back it up before deleting deployment files. Only public repositories are supported. With ten mods, background checks make forty GitHub API requests per hour; heavy use of `/mod` can hit GitHub's unauthenticated API rate limit.
