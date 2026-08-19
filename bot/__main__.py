"""Entry point: `python -m bot`."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from discord.ext import commands

from .config import Config
from .core.settings import GuildSettings
from .core.ytdl import YTDL

EXTENSIONS = ("bot.cogs.music", "bot.cogs.media")


def setup_logging(level: str) -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    fh = RotatingFileHandler("logs/bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.voice_state").setLevel(logging.INFO)


class MusicBot(commands.Bot):
    def __init__(self, cfg: Config):
        intents = discord.Intents.default()
        intents.message_content = True      # prefix commands + link detection
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned_or(cfg.prefix), intents=intents,
                         help_command=None, owner_ids=set(cfg.owner_ids) or None,
                         allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
        self.cfg = cfg
        self.ytdl = YTDL(cookies_file=cfg.ytdl_cookies_file)
        self.settings = GuildSettings(cfg.data_dir / "guild_settings.json", media_default=cfg.media_enabled_default)
        self.log = logging.getLogger("bot")

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            await self.load_extension(ext)
            self.log.info("loaded %s", ext)
        try:
            synced = await self.tree.sync()
            self.log.info("synced %d slash commands", len(synced))
        except Exception as e:
            self.log.warning("slash sync failed: %s", e)

    async def on_ready(self) -> None:
        self.log.info("online as %s (%s) in %d guilds", self.user, self.user.id, len(self.guilds))  # type: ignore[union-attr]
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening,
                                                             name=f"/play · {self.cfg.prefix}help"))

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:  # type: ignore[override]
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("🚫 You don't have permission for that.")
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("That only works in a server.")
            return
        if ctx.cog and ctx.cog.has_error_handler():
            return  # cog handled it
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send(f"Usage: `{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}`")  # type: ignore[union-attr]
            return
        self.log.exception("unhandled command error in %s", ctx.command, exc_info=error)
        await ctx.send("❌ Something went wrong; it's been logged.")


def build_help(bot: MusicBot) -> None:
    @bot.hybrid_command(name="help", description="Show all commands")
    async def help_cmd(ctx: commands.Context):
        p = bot.cfg.prefix
        e = discord.Embed(title="🎵 Music bot", description=f"Slash commands work too — type `/`. Prefix: `{p}`", color=0x5865F2)
        e.add_field(name="Music", inline=False, value=(
            f"`{p}play <query|url|playlist>` (`{p}p`) · `{p}skip` · `{p}stop` · `{p}pause` · `{p}resume`\n"
            f"`{p}queue [page]` (`{p}q`) · `{p}nowplaying` (`{p}np`) · `{p}volume [0-150]`\n"
            f"`{p}loop [off|one|all]` · `{p}shuffle` · `{p}remove <n>` · `{p}move <a> <b>` · `{p}clear`\n"
            f"`{p}join` · `{p}leave` · `{p}status`"))
        e.add_field(name="Media (Twitter/X & TikTok → MP4)", inline=False, value=(
            f"Just paste a link — it gets converted automatically.\n"
            f"`{p}convert <url>` · `{p}mediainfo` · `{p}media-toggle` (admin) · `{p}media-cleanup` (admin)"))
        e.set_footer(text="Bot leaves voice when idle or when everyone's gone.")
        await ctx.send(embed=e)

    @bot.command(name="sync", hidden=True)
    @commands.is_owner()
    async def sync_cmd(ctx: commands.Context):
        synced = await bot.tree.sync()
        await ctx.send(f"Synced {len(synced)} slash commands.")

    @bot.command(name="reload", hidden=True)
    @commands.is_owner()
    async def reload_cmd(ctx: commands.Context, name: str):
        await bot.reload_extension(f"bot.cogs.{name}")
        await ctx.send(f"Reloaded {name}.")


async def main() -> None:
    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    cfg.download_dir.mkdir(parents=True, exist_ok=True)
    bot = MusicBot(cfg)
    build_help(bot)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    async with bot:
        runner = asyncio.create_task(bot.start(cfg.token))
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if runner in done:
            runner.result()        # re-raise login errors etc.
        else:
            logging.getLogger("bot").info("shutdown signal received")
            await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
