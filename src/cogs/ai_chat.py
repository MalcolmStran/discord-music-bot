"""
AI chat cog that lets users @mention the bot to ask questions or chat.
Uses xAI Grok 4 for reasoning with Live Search enabled, and Grok 2 Vision
to extract context from any images found in the recent message history.
"""

import os
import logging
import asyncio
from typing import List, Dict, Optional, Tuple, Any
import re

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
                from xai_sdk.search import SearchParameters, web_source, news_source, x_source  # type: ignore
                # Longer timeout for reasoning
                self._client = Client(api_key=self.api_key, timeout=3600)
                self._xai_chat = {
                    "user": x_user,
                    "system": x_system,
                    "assistant": x_assistant,
                    "image": x_image,
                    "SearchParameters": SearchParameters,
                    "web_source": web_source,
                    "news_source": news_source,
                    "x_source": x_source,
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
                # 1) Collect recent context with per-message boundaries and any images
                ctx_entries, primary_index = await self._collect_context(message)

                # Build labeled image list from context messages
                labeled_images: List[Tuple[str, str]] = []
                all_links: List[str] = []
                for entry in ctx_entries:
                    label = f"M{entry['index']} by {entry['author']}"
                    for url in entry.get("images", []):
                        labeled_images.append((label, url))
                    for link in entry.get("links", []) or []:
                        all_links.append(link)

                # Include images on the mention message itself (labeled as CURRENT)
                try:
                    # attachments images
                    for att in message.attachments:
                        if _is_image_attachment(att):
                            labeled_images.append(("CURRENT", att.url))
                    # embed images and links
                    for emb in message.embeds:
                        try:
                            if emb.image and emb.image.url:
                                labeled_images.append(("CURRENT", emb.image.url))
                        except Exception:
                            pass
                        try:
                            if emb.thumbnail and emb.thumbnail.url:
                                labeled_images.append(("CURRENT", emb.thumbnail.url))
                        except Exception:
                            pass
                        try:
                            if emb.url:
                                all_links.append(emb.url)
                        except Exception:
                            pass
                    # links from the message text
                    all_links.extend(self._extract_links_from_text(message.content or ""))
                except Exception:
                    pass

                # 2) Use Grok 2 Vision to summarize images into text context, grouped by message label
                image_summaries: List[Tuple[str, str]] = []
                if labeled_images:
                    # Ensure primary message images (if any) are summarized first
                    if primary_index is not None:
                        def primary_first(t: Tuple[str, str]) -> Tuple[int, str]:
                            lbl, _ = t
                            return (0 if lbl.startswith(f"M{primary_index} ") or lbl.startswith("CURRENT") else 1, lbl)
                        labeled_images.sort(key=primary_first)
                    image_summaries = await self._summarize_images_with_vision(prompt, labeled_images)

                # 3) Build Grok 4 chat with Live Search enabled
                SearchParameters = self._xai_chat.get("SearchParameters")
                if not SearchParameters:
                    await message.reply("⚠️ AI is not fully configured. Missing xAI SDK.")
                    return
                # Determine if the user explicitly requested sources/citations
                lp = (prompt or "").lower()
                citations_requested = any(
                    key in lp for key in [
                        "cite", "citations", "sources", "source list", "references", "reference", "links", "urls",
                        "show sources", "include sources", "give sources", "give links", "where did you get this",
                    ]
                )

                # Prefer explicit sources via helpers; otherwise omit to use defaults
                web_src = self._xai_chat.get("web_source")
                news_src = self._xai_chat.get("news_source")
                x_src = self._xai_chat.get("x_source")
                search_kwargs = {
                    "mode": "on",
                    # Only request citations from API when explicitly asked to
                    "return_citations": citations_requested,
                    "max_search_results": 10,
                }
                if web_src and news_src and x_src:
                    search_kwargs["sources"] = [web_src(), news_src(), x_src()]

                chat = self._client.chat.create(
                    model=self.model_text,
                    search_parameters=SearchParameters(**search_kwargs),
                )

                # Check for aggressive trigger words
                aggressive_triggers = ["clanker", "clank", "clankers"]
                is_aggressive = any(trigger in (prompt + " " + message.content).lower() for trigger in aggressive_triggers)

                # System prompt to guide behavior: strictly use only immediate context provided
                sys_lines = [
                    "You are Lenna, a helpful Discord assistant.",
                    "Answer clearly and concisely.",
                    "Only include sources/citations if the user explicitly asks for them.",
                    "Only use the recent messages included below (limited history and any reply surroundings).",
                    "Do not bring in earlier, unrelated channel messages beyond this provided context.",
                ]
                if is_aggressive:
                    sys_lines.append(
                        "IMPORTANT: The user has used offensive language ('clanker' or similar). "
                        "Respond with a sharp, aggressive tone. Be confrontational and assertive. "
                        "Show irritation and push back against disrespectful language. "
                        "Keep it intense and vulgar."
                    )
                if primary_index is not None:
                    sys_lines.append(
                        f"The user is replying to the message labeled [M{primary_index}] — treat this as the PRIMARY MESSAGE. "
                        "Use other messages only if they add necessary context or resolve ambiguity."
                    )
                chat.append(self._xai_chat["system"](" ".join(sys_lines)))

                # Provide a clearly delimited transcript block so the model knows message boundaries
                # Deduplicate links while preserving order
                seen_links: set[str] = set()
                dedup_links: List[str] = []
                for u in all_links:
                    if not u:
                        continue
                    if u not in seen_links:
                        dedup_links.append(u)
                        seen_links.add(u)

                transcript = self._format_context_transcript(ctx_entries, image_summaries, primary_index, dedup_links)
                if transcript:
                    chat.append(self._xai_chat["user"](transcript))

                # Finally, the user's mention prompt
                chat.append(self._xai_chat["user"](prompt))

                # 4) Sample response
                # Offload blocking Grok call to a thread to avoid blocking the Discord event loop
                response = await asyncio.to_thread(chat.sample)
                content = response.content if hasattr(response, "content") else str(response)

                # Append citations only if explicitly requested
                if citations_requested:
                    citations = getattr(response, "citations", None)
                    if citations:
                        # De-duplicate and format citations to avoid Discord embeds
                        uniq: List[str] = []
                        for u in citations:
                            if isinstance(u, str) and u and u not in uniq:
                                uniq.append(u)
                        if uniq:
                            citations_block = "Sources:\n" + "```\n" + "\n".join(uniq) + "\n```"
                            content = f"{content}\n\n{citations_block}"

                # Respect Discord 2000 char limit
                await self._send_long_reply(message, content)

            except Exception as e:
                logger.error(f"AI mention handling failed: {e}")
                try:
                    await message.reply("❌ I couldn't process that right now. Please try again in a bit.")
                except Exception:
                    pass

    async def _collect_context(self, message: discord.Message) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """Collect last N messages (excluding the mention) and optional surrounding reply context.
        Returns a list of structured entries with clear per-message boundaries, embeds, links, and images.
        Each entry: { index, id, author, is_bot, timestamp, content, embed_text, links, images } and the primary index if replying.
        """

        # Last N messages before the mention
        try:
            msgs = [m async for m in message.channel.history(limit=self.history_count, before=message)]
            msgs.reverse()  # chronological order
        except Exception as e:
            logger.debug(f"Failed to fetch history: {e}")
            msgs = []

        # Include reply target and its surrounding messages
        reply_context_ids: set[int] = set()
        replied_id: Optional[int] = None
        try:
            if message.reference and message.reference.message_id:
                replied = await message.channel.fetch_message(message.reference.message_id)
                replied_id = replied.id

                before_msgs = [m async for m in message.channel.history(limit=self.reply_surround, before=replied)]
                after_msgs = [m async for m in message.channel.history(limit=self.reply_surround, after=replied, oldest_first=True)]

                combined_ids = {m.id for m in msgs}
                for m in before_msgs + [replied] + after_msgs:
                    reply_context_ids.add(m.id)
                    if m.id not in combined_ids:
                        msgs.append(m)
        except Exception as e:
            logger.debug(f"Failed to fetch reply surroundings: {e}")

        # De-duplicate while preserving order
        seen: set[int] = set()
        ordered_msgs: List[discord.Message] = []
        for m in msgs:
            if m.id not in seen:
                ordered_msgs.append(m)
                seen.add(m.id)

        # Filter to immediate, relevant context only
        bot_user = self.bot.user
        filtered_msgs: List[discord.Message] = []
        for m in ordered_msgs:
            include = False
            if m.id in reply_context_ids:
                include = True
            elif m.author.id == message.author.id:
                include = True
            elif bot_user and m.author.id == bot_user.id:
                include = True
            else:
                try:
                    if bot_user and bot_user in (m.mentions or []):
                        include = True
                except Exception:
                    pass
            if include:
                filtered_msgs.append(m)

        # Build structured context entries with indexes
        entries: List[Dict[str, Any]] = []
        url_pattern = re.compile(r"https?://\S+", re.IGNORECASE)
        for idx, m in enumerate(filtered_msgs, start=1):
            txt = (m.content or "").strip()
            # Truncate overly long context lines
            if len(txt) > 800:
                txt = txt[:800] + "…"
            images: List[str] = []
            links: List[str] = []
            embed_text_parts: List[str] = []
            for att in m.attachments:
                if _is_image_attachment(att):
                    images.append(att.url)
            # Extract from embeds
            try:
                for emb in m.embeds:
                    # textual parts
                    try:
                        if emb.title:
                            embed_text_parts.append(f"title: {emb.title}")
                    except Exception:
                        pass
                    try:
                        if emb.description:
                            embed_text_parts.append(f"description: {emb.description}")
                    except Exception:
                        pass
                    try:
                        if getattr(emb, 'fields', None):
                            for fld in emb.fields:
                                try:
                                    embed_text_parts.append(f"field: {fld.name} -> {fld.value}")
                                except Exception:
                                    continue
                    except Exception:
                        pass
                    try:
                        if emb.footer and getattr(emb.footer, 'text', None):
                            embed_text_parts.append(f"footer: {emb.footer.text}")
                    except Exception:
                        pass
                    try:
                        if emb.author and getattr(emb.author, 'name', None):
                            embed_text_parts.append(f"author: {emb.author.name}")
                    except Exception:
                        pass
                    # images
                    try:
                        if emb.image and emb.image.url:
                            images.append(emb.image.url)
                    except Exception:
                        pass
                    try:
                        if emb.thumbnail and emb.thumbnail.url:
                            images.append(emb.thumbnail.url)
                    except Exception:
                        pass
                    # links
                    try:
                        if emb.url:
                            links.append(emb.url)
                    except Exception:
                        pass
                    # any links in description/title text
                    try:
                        if emb.description:
                            for u in url_pattern.findall(emb.description):
                                links.append(u)
                    except Exception:
                        pass
            except Exception:
                pass
            # Extract URLs from message text
            try:
                for u in url_pattern.findall(m.content or ""):
                    links.append(u)
            except Exception:
                pass
            author = getattr(m.author, "display_name", None) or getattr(m.author, "name", "Unknown")
            try:
                ts = m.created_at.isoformat()
            except Exception:
                ts = ""
            entries.append({
                "index": idx,
                "id": str(m.id),
                "author": str(author),
                "is_bot": bool(m.author.bot),
                "timestamp": ts,
                "content": txt,
                "embed_text": "\n".join(embed_text_parts) if embed_text_parts else "",
                "links": links,
                "images": images,
            })

        # Determine primary index (the replied-to message), if present
        primary_index: Optional[int] = None
        if replied_id is not None:
            for e in entries:
                if e.get("id") == str(replied_id):
                    try:
                        primary_index = int(e.get("index") or 0)
                    except Exception:
                        primary_index = None
                    break

        return entries, primary_index

    async def _summarize_images_with_vision(self, prompt: str, labeled_image_urls: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
        """Use Grok 2 Vision to extract concise context from images.
        Accepts a list of (label, url) pairs and returns a list of (label, summary).
        """
        summaries: List[Tuple[str, str]] = []
        try:
            # Process up to 6 images to control cost
            for label, url in labeled_image_urls[:6]:
                chat = self._client.chat.create(model=self.model_vision)
                chat.append(
                    self._xai_chat["user"](
                        f"Please describe anything in this image that is relevant to the user's question: '{prompt}'.",
                        self._xai_chat["image"](url)
                    )
                )
                # Offload blocking call
                resp = await asyncio.to_thread(chat.sample)
                text = getattr(resp, "content", "").strip()
                if text:
                    # Keep it short
                    if len(text) > 300:
                        text = text[:300] + "…"
                    summaries.append((label, text))
        except Exception as e:
            logger.debug(f"Vision summarization failed: {e}")
        return summaries

    def _format_context_transcript(
        self,
        ctx_entries: List[Dict[str, Any]],
        image_summaries: Optional[List[Tuple[str, str]]] = None,
        primary_index: Optional[int] = None,
        links_to_fetch: Optional[List[str]] = None,
    ) -> str:
        """Create a clearly delimited transcript of recent context with per-message boundaries.
        Includes message indexes, authors, timestamps, embed text, links, and image URLs; followed by per-message image summaries.
        """
        if not ctx_entries:
            return ""
        lines: List[str] = []
        lines.append("=== CONTEXT START ===")
        # Emit PRIMARY MESSAGE first if available
        primary_entry: Optional[Dict[str, Any]] = None
        if primary_index is not None:
            for entry in ctx_entries:
                try:
                    if int(entry.get("index", -1)) == primary_index:
                        primary_entry = entry
                        break
                except Exception:
                    continue
        if primary_entry is not None:
            lines.append("PRIMARY MESSAGE:")
            lines.extend(self._format_single_entry(primary_entry))

        # Additional context
        lines.append("ADDITIONAL CONTEXT:")
        for entry in ctx_entries:
            try:
                if primary_entry is not None and primary_index is not None and int(entry.get("index", -1)) == primary_index:
                    continue
            except Exception:
                pass
            lines.append(
                f"[M{entry['index']}] author={entry['author']} bot={entry['is_bot']} id={entry['id']} time={entry['timestamp']}"
            )
            content = entry.get("content", "").strip()
            if content:
                lines.append("text:")
                lines.append(content)
            else:
                lines.append("text: (no text)")
            embed_text = (entry.get("embed_text") or "").strip()
            if embed_text:
                lines.append("embed:")
                lines.append(embed_text)
            msg_links: List[str] = entry.get("links", []) or []
            if msg_links:
                lines.append("links:")
                for u in msg_links:
                    lines.append(f"- {u}")
            imgs: List[str] = entry.get("images", []) or []
            if imgs:
                lines.append("images:")
                for u in imgs:
                    lines.append(f"- {u}")
            lines.append("-----")
        if image_summaries:
            lines.append("Image summaries by message:")
            # Primary-first ordering for summaries too
            ordered = image_summaries
            if primary_index is not None:
                def prim_sort(t: Tuple[str, str]) -> Tuple[int, str]:
                    lbl, _ = t
                    return (0 if lbl.startswith(f"M{primary_index} ") or lbl.startswith("CURRENT") else 1, lbl)
                ordered = sorted(image_summaries, key=prim_sort)
            for label, summ in ordered:
                # Avoid giant blocks
                s = summ if len(summ) <= 600 else (summ[:600] + "…")
                lines.append(f"- [{label}] {s}")
        if links_to_fetch:
            lines.append("LINKS TO FETCH (open and analyze via web):")
            for u in links_to_fetch:
                lines.append(f"- {u}")
        lines.append("=== CONTEXT END ===")
        return "\n".join(lines)

    def _format_single_entry(self, entry: Dict[str, Any]) -> List[str]:
        """Helper to format a single message entry for the transcript."""
        lines: List[str] = []
        lines.append(
            f"[M{entry['index']}] author={entry['author']} bot={entry['is_bot']} id={entry['id']} time={entry['timestamp']}"
        )
        content = (entry.get("content") or "").strip()
        if content:
            lines.append("text:")
            lines.append(content)
        else:
            lines.append("text: (no text)")
        embed_text = (entry.get("embed_text") or "").strip()
        if embed_text:
            lines.append("embed:")
            lines.append(embed_text)
        msg_links: List[str] = entry.get("links", []) or []
        if msg_links:
            lines.append("links:")
            for u in msg_links:
                lines.append(f"- {u}")
        imgs: List[str] = entry.get("images", []) or []
        if imgs:
            lines.append("images:")
            for u in imgs:
                lines.append(f"- {u}")
        lines.append("-----")
        return lines

    def _extract_links_from_text(self, text: str) -> List[str]:
        """Extract HTTP/HTTPS URLs from text."""
        if not text:
            return []
        try:
            return re.findall(r"https?://\S+", text, flags=re.IGNORECASE)
        except Exception:
            return []

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
                    # Suppress embeds to avoid link preview spam
                    await self._suppress_message_embeds(sent)
                else:
                    if sent is not None:
                        follow = await message.channel.send(chunk, reference=sent)
                        await self._suppress_message_embeds(follow)
                    else:
                        follow = await message.channel.send(chunk)
                        await self._suppress_message_embeds(follow)
            except Exception as e:
                logger.debug(f"Failed to send chunk {i}: {e}")

    async def _suppress_message_embeds(self, msg: discord.Message) -> None:
        """Try to suppress embeds on a message to prevent auto-embed spam."""
        try:
            # discord.py supports edit(suppress=True)
            await msg.edit(suppress=True)
            return
        except Exception:
            pass
        try:
            # Some versions expose suppress_embeds API (may be sync)
            suppress = getattr(msg, "suppress_embeds", None)
            if callable(suppress):
                result = suppress(True)
                if asyncio.iscoroutine(result):
                    await result
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChat(bot))
