"""Entry point: `python -m bot`."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .core.settings import GuildSettings
from .core.spotify import Spotify
from .core.ytdl import YTDL

EXTENSIONS = ("bot.cogs.music", "bot.cogs.media")


def setup_logging(level: str, log_dir: Path = Path("logs")) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    for h in list(root.handlers):          # a reload must not double every log line
        root.removeHandler(h)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    # File logging is a nice-to-have: a read-only or missing directory must not stop the
    # bot from starting, which is what an unguarded mkdir() here used to do.
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        root.warning("file logging disabled (%s): %s", log_dir, e)
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
        # the playlist cap used to be hard-wired to 100 while the queue held 50, so half a
        # long playlist was resolved and then thrown away
        self.ytdl = YTDL(cookies_file=cfg.ytdl_cookies_file, max_playlist=cfg.max_queue_size)
        self.spotify = Spotify(cfg.spotify_client_id, cfg.spotify_client_secret, max_tracks=cfg.max_queue_size)
        self.settings = GuildSettings(cfg.data_dir / "guild_settings.json", media_default=cfg.media_enabled_default)
        self.log = logging.getLogger("bot")

    async def setup_hook(self) -> None:
        # slash failures are dispatched through the tree, not through on_command_error
        self.tree.on_error = self.on_app_command_error  # type: ignore[method-assign]
        for ext in EXTENSIONS:
            await self.load_extension(ext)
            self.log.info("loaded %s", ext)
        await self._sync_commands()
        self.loop.create_task(self._heartbeat(), name="heartbeat")

    def command_signature(self) -> str:
        """Fingerprint of the local app-command surface, to skip no-op global syncs."""
        parts = []
        for cmd in sorted(self.tree.get_commands(), key=lambda c: c.qualified_name):
            params = getattr(cmd, "parameters", ()) or ()
            parts.append("|".join([
                cmd.qualified_name,
                getattr(cmd, "description", "") or "",
                str(getattr(cmd, "default_permissions", None)),
                str(getattr(cmd, "guild_only", False)),
                ",".join(f"{p.name}:{p.type}:{p.required}" for p in params),
            ]))
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()

    async def _sync_commands(self) -> None:
        """Publish slash commands, but only when they actually changed.

        A global sync on every restart is a wasted round trip against a rate-limited
        endpoint; and when it failed the bot used to carry on with no working slash
        commands and only a WARNING to show for it.
        """
        sig = self.command_signature()
        if self.settings.get(0, "command_signature") == sig and not self.cfg.force_sync:
            self.log.info("slash commands unchanged; skipping global sync")
            return
        try:
            synced = await self.tree.sync()
        except Exception as e:
            self.log.error("slash sync failed (%s) — slash commands may be stale; "
                           "run `%ssync` once the API recovers", e, self.cfg.prefix)
            return
        self.settings.set(0, "command_signature", sig)
        self.log.info("synced %d slash commands", len(synced))

    async def _heartbeat(self) -> None:
        """Touch a file while the gateway is actually alive.

        `restart: unless-stopped` only catches the process dying. This catches the other
        failure mode — the process up but the websocket wedged — which the Docker
        HEALTHCHECK then turns into a restart.
        """
        beat = self.cfg.log_dir / "healthy"
        while not self.is_closed():
            try:
                if self.is_ready() and math.isfinite(self.latency):   # NaN before the first heartbeat
                    beat.parent.mkdir(parents=True, exist_ok=True)
                    beat.touch()
            except OSError as e:
                self.log.debug("heartbeat write failed: %s", e)
            await asyncio.sleep(30)

    async def on_ready(self) -> None:
        self.log.info("online as %s (%s) in %d guilds", self.user, self.user.id, len(self.guilds))  # type: ignore[union-attr]
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening,
                                                             name=f"/play · {self.cfg.prefix}help"))

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:  # type: ignore[override]
        if isinstance(error, commands.CommandNotFound):
            return
        # This runs even when the cog already replied (Command.dispatch_error dispatches
        # command_error in a `finally`), so bail out first or the user gets two answers.
        if ctx.cog and ctx.cog.has_error_handler():
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("🚫 You don't have permission for that.")
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("That only works in a server.")
            return
        if isinstance(error, commands.NotOwner):
            await ctx.send("🚫 That's an owner-only command.")
            return
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Slow down — try again in {error.retry_after:.0f}s.")
            return
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send(f"Usage: `{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}`")  # type: ignore[union-attr]
            return
        if isinstance(error, commands.CheckFailure):
            await ctx.send("🚫 You can't use that here.")
            return
        self.log.exception("unhandled command error in %s", ctx.command, exc_info=error)
        await ctx.send("❌ Something went wrong; it's been logged.")

    async def on_app_command_error(self, interaction: discord.Interaction,
                                   error: app_commands.AppCommandError) -> None:
        """Slash-command failures raised outside the prefix pipeline.

        Without this the tree logs to stderr and the user is left staring at a spinner
        that ends in "The application did not respond".
        """
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Slow down — try again in {error.retry_after:.0f}s."
        elif isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = "🚫 You can't use that here."
        else:
            self.log.exception("unhandled app command error in %s",
                               interaction.command and interaction.command.qualified_name, exc_info=error)
            msg = "❌ Something went wrong; it's been logged."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


def build_help(bot: MusicBot) -> None:
    @bot.hybrid_command(name="help", description="Show all commands")
    async def help_cmd(ctx: commands.Context):
        p = bot.cfg.prefix
        e = discord.Embed(title="🎵 Music bot", description=f"Slash commands work too — type `/`. Prefix: `{p}`", color=0x5865F2)
        e.add_field(name="Music", inline=False, value=(
            f"`{p}play <query|url|playlist|spotify link>` (`{p}p`) · `{p}skip` · `{p}stop` · `{p}pause` · `{p}resume`\n"
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
        bot.settings.set(0, "command_signature", bot.command_signature())
        await ctx.send(f"Synced {len(synced)} slash commands.")

    @bot.command(name="reload", hidden=True)
    @commands.is_owner()
    async def reload_cmd(ctx: commands.Context, name: str):
        if name not in ("music", "media"):
            return await ctx.send("Reloadable extensions: `music`, `media`.")
        await bot.reload_extension(f"bot.cogs.{name}")
        await ctx.send(f"Reloaded {name}.")


async def main() -> None:
    cfg = Config.from_env()
    setup_logging(cfg.log_level, cfg.log_dir)
    try:
        cfg.download_dir.mkdir(parents=True, exist_ok=True)
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SystemExit(f"cannot create {cfg.download_dir}: {e}") from e
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
        done, pending = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        try:
            if runner in done:
                runner.result()    # re-raise login errors etc.
            else:
                logging.getLogger("bot").info("shutdown signal received")
                await bot.close()  # lets `start()` finish its own gateway teardown
        finally:
            # Reap the loser, or asyncio warns "Task was destroyed but it is pending".
            # bot.close() makes start() return by itself, so give it a moment to unwind
            # the gateway cleanly before resorting to cancellation.
            for task in pending:
                if not task.done():
                    await asyncio.wait({task}, timeout=10)
                if not task.done():
                    task.cancel()
                try:
                    await task
                except Exception:      # never let the loser mask the real exit reason
                    logging.getLogger("bot").debug("shutdown task ended badly", exc_info=True)
                except asyncio.CancelledError:
                    pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
