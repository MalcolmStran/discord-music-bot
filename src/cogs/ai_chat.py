"""
AI chat cog that lets users @mention the bot to ask questions or chat.
Uses xAI Grok 4 for reasoning with Live Search enabled, and Grok 2 Vision
to extract context from any images found in the recent message history.
"""

import os
import logging
import asyncio
from typing import List, Dict, Optional, Tuple, Any

import discord
from discord.ext import commands

import sys
import pathlib

# Import config from project root
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import config  # type: ignore

logger = logging.getLogger(__name__)


def _is_image_attachment(att: discord.Attachment) -> bool:
    try:
        if att.content_type and att.content_type.startswith("image/"):
            return True
    except Exception:
        pass
    name = (att.filename or "").lower()
    return any(name.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"))


class AIChat(commands.Cog, name="AI"):
    """Responds to @mentions with Grok 4, using recent context and image understanding."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api_key = getattr(config, "XAI_API_KEY", None)
        self.enabled = False
        self.model_text = os.getenv("XAI_TEXT_MODEL", "grok-4")
        self.model_vision = os.getenv("XAI_VISION_MODEL", "grok-2-vision")
        self.history_count = int(os.getenv("AI_HISTORY_COUNT", getattr(config, "AI_HISTORY_COUNT", 10)))
        self.reply_surround = int(os.getenv("AI_REPLY_SURROUND", getattr(config, "AI_REPLY_SURROUND", 2)))
        self._client: Any = None
        self._xai_chat: Dict[str, Any] = {}
        if self.api_key:
            try:
                # Lazy import to avoid hard dependency if feature unused
                from xai_sdk import Client  # type: ignore
                from xai_sdk.chat import (  # type: ignore
                    user as x_user,
                    system as x_system,
                    assistant as x_assistant,
                    image as x_image,
                )
                from xai_sdk.search import SearchParameters  # type: ignore
                # Longer timeout for reasoning
                self._client = Client(api_key=self.api_key, timeout=3600)
                self._xai_chat = {
                    "user": x_user,
                    "system": x_system,
                    "assistant": x_assistant,
                    "image": x_image,
                    "SearchParameters": SearchParameters,
                }
                self.enabled = True
            except Exception as e:
                logger.error(f"Failed to initialize xAI Client: {e}")
                self.enabled = False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bot messages or DMs
        if not message.guild or message.author.bot:
            return

        # Only respond if the bot is mentioned
        if not message.mentions or self.bot.user not in message.mentions:
            return

        if not self.enabled or not self._client:
            return  # Silently ignore if not configured

        # Remove the bot mention from the prompt content
        prompt = message.content
        try:
            for m in message.mentions:
                prompt = prompt.replace(m.mention, "").strip()
        except Exception:
            pass

        # If there's nothing to ask after removing mentions, do nothing
        if not prompt:
            return

        # Run the processing without blocking other handlers
        asyncio.create_task(self._handle_ai_request(message, prompt))

    async def _handle_ai_request(self, message: discord.Message, prompt: str):
        async with message.channel.typing():
            try:
                # 1) Collect recent context and any images
                ctx_messages, image_urls = await self._collect_context(message)

                # Include images on the mention message itself
                try:
                    for att in message.attachments:
                        if _is_image_attachment(att):
                            image_urls.append(att.url)
                except Exception:
                    pass

                # 2) Use Grok 2 Vision to summarize images into text context
                image_summaries = []
                if image_urls:
                    image_summaries = await self._summarize_images_with_vision(prompt, image_urls)

                # 3) Build Grok 4 chat with Live Search enabled
                SearchParameters = self._xai_chat.get("SearchParameters")
                if not SearchParameters:
                    await message.reply("⚠️ AI is not fully configured. Missing xAI SDK.")
                    return
                chat = self._client.chat.create(
                    model=self.model_text,
                    search_parameters=SearchParameters(
                        mode="on",  # Always enable live search
                        return_citations=True,
                        # Enable web/news/X sources explicitly
                        sources=[{"type": "web"}, {"type": "news"}, {"type": "x"}],
                        max_search_results=10,
                    ),
                )

                # System prompt to guide behavior
                chat.append(
                    self._xai_chat["system"](
                        "You are Grok, a helpful Discord assistant. "
                        "Answer clearly and concisely. Cite sources when using Live Search. "
                        "Be robust to informal tone and extract intent from short prompts."
                    )
                )

                # Add recent conversation context (last N messages)
                for author_is_bot, content in ctx_messages:
                    if not content:
                        continue
                    if author_is_bot:
                        chat.append(self._xai_chat["assistant"](content))
                    else:
                        chat.append(self._xai_chat["user"](content))

                # Include any image summaries as extra context
                if image_summaries:
                    chat.append(
                        self._xai_chat["user"](
                            "Image context from recent messages:\n" + "\n".join(f"- {s}" for s in image_summaries)
                        )
                    )

                # Finally, the user's mention prompt
                chat.append(self._xai_chat["user"](prompt))

                # 4) Sample response
                response = chat.sample()
                content = response.content if hasattr(response, "content") else str(response)

                # Append citations if available
                citations = getattr(response, "citations", None)
                if citations:
                    content = f"{content}\n\nSources:\n" + "\n".join(f"- {url}" for url in citations)

                # Respect Discord 2000 char limit
                await self._send_long_reply(message, content)

            except Exception as e:
                logger.error(f"AI mention handling failed: {e}")
                try:
                    await message.reply("❌ I couldn't process that right now. Please try again in a bit.")
                except Exception:
                    pass

    async def _collect_context(self, message: discord.Message) -> Tuple[List[Tuple[bool, str]], List[str]]:
        """Collect last N messages (excluding the mention) and optional surrounding reply context.
        Returns list of (author_is_bot, content) and image URLs found.
        """
        context: List[Tuple[bool, str]] = []
        image_urls: List[str] = []

        # Last N messages before the mention
        try:
            msgs = [m async for m in message.channel.history(limit=self.history_count, before=message)]
            msgs.reverse()  # chronological order
        except Exception as e:
            logger.debug(f"Failed to fetch history: {e}")
            msgs = []

        # Include reply target and its surrounding messages
        try:
            if message.reference and message.reference.message_id:
                replied = await message.channel.fetch_message(message.reference.message_id)

                before_msgs = [m async for m in message.channel.history(limit=self.reply_surround, before=replied)]
                after_msgs = [m async for m in message.channel.history(limit=self.reply_surround, after=replied, oldest_first=True)]

                combined_ids = {m.id for m in msgs}
                for m in before_msgs + [replied] + after_msgs:
                    if m.id not in combined_ids:
                        msgs.append(m)
        except Exception as e:
            logger.debug(f"Failed to fetch reply surroundings: {e}")

        # De-duplicate while preserving order
        seen: set = set()
        ordered_msgs: List[discord.Message] = []
        for m in msgs:
            if m.id not in seen:
                ordered_msgs.append(m)
                seen.add(m.id)

        # Extract contents and image URLs
        for m in ordered_msgs:
            txt = (m.content or "").strip()
            # Truncate overly long context lines
            if len(txt) > 800:
                txt = txt[:800] + "…"
            context.append((m.author.bot, txt))
            for att in m.attachments:
                if _is_image_attachment(att):
                    image_urls.append(att.url)

        return context, image_urls

    async def _summarize_images_with_vision(self, prompt: str, image_urls: List[str]) -> List[str]:
        """Use Grok 2 Vision to extract concise context from images.
        Returns a list of short bullet summaries (strings).
        """
        summaries: List[str] = []
        try:
            # Process up to 5 images to control cost
            for url in image_urls[:5]:
                chat = self._client.chat.create(model=self.model_vision)
                chat.append(
                    self._xai_chat["user"](
                        f"Please describe anything in this image that is relevant to the user's question: '{prompt}'.",
                        self._xai_chat["image"](url)
                    )
                )
                resp = chat.sample()
                text = getattr(resp, "content", "").strip()
                if text:
                    # Keep it short
                    if len(text) > 300:
                        text = text[:300] + "…"
                    summaries.append(text)
        except Exception as e:
            logger.debug(f"Vision summarization failed: {e}")
        return summaries

    async def _send_long_reply(self, message: discord.Message, content: str):
        if not content:
            return
        # Split into 2000-char chunks
        limit = 2000
        chunks: List[str] = []
        while content:
            chunks.append(content[:limit])
            content = content[limit:]
        # Reply first, then follow up
        sent: Optional[discord.Message] = None
        for i, chunk in enumerate(chunks):
            try:
                if i == 0:
                    sent = await message.reply(chunk)
                else:
                    if sent is not None:
                        await message.channel.send(chunk, reference=sent)
                    else:
                        await message.channel.send(chunk)
            except Exception as e:
                logger.debug(f"Failed to send chunk {i}: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChat(bot))
