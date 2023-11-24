import asyncio
import io
import textwrap
from typing import Tuple
from urllib.parse import urlparse

import discord
import httpx
import orm
from discord.ext import commands

from utils.db import StarBoardMessage


class StarBoardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()

    @staticmethod
    async def archive_image(starboard_message: discord.Message):
        async with httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.69; Win64; x64) "
                "LCC-Bot-Scraper/0 (https://github.com/nexy7574/LCC-bot)"
            }
        ) as session:
            image = starboard_message.embeds[0].image
            if image and image.url:
                parsed = urlparse(image.url)
                filename = parsed.path.split("/")[-1]
                try:
                    r = await session.get(image.url)
                except httpx.HTTPError:
                    if image.proxy_url:
                        r = await session.get(image.proxy_url)
                    else:
                        return

                FS_LIMIT = starboard_message.guild.filesize_limit
                # if FS_LIMIT is 8mb, its actually 25MB
                if FS_LIMIT == 8 * 1024 * 1024:
                    FS_LIMIT = 25 * 1024 * 1024
                if r.status_code == 200 and len(r.content) < FS_LIMIT:
                    file = io.BytesIO(r.content)
                    file.seek(0)
                    embed = starboard_message.embeds[0].copy()
                    embed.set_image(url="attachment://" + filename)
                    embeds = [embed, *starboard_message.embeds[1:]]
                    await starboard_message.edit(embeds=embeds, file=discord.File(file, filename=filename))

    async def generate_starboard_embed(self, message: discord.Message) -> discord.Embed:
        star_count = [x for x in message.reactions if str(x.emoji) == "\N{white medium star}"]
        if not star_count:
            star_count = 0
        else:
            star_count = star_count[0].count
        # noinspection PyUnresolvedReferences
        cap = (message.channel if "thread" in message.channel.type.name else message.guild).member_count * 0.1
        embed = discord.Embed(colour=discord.Colour.gold(), timestamp=message.created_at, description=message.content)
        embed.set_author(
            name=message.author.display_name, url=message.jump_url, icon_url=message.author.display_avatar.url
        )

        if star_count > 5:
            stars = "\N{white medium star}x{:,}".format(star_count)
        else:
            stars = "\N{white medium star}" * star_count
            stars = stars or "\N{no entry sign}"

        embed.add_field(
            name="Info",
            value=f"Star count: {stars}\n"
            f"Channel: {message.channel.mention}\n"
            f"Author: {message.author.mention}\n"
            f"URL: [jump]({message.jump_url})\n"
            f"Sent: {discord.utils.format_dt(message.created_at, 'R')}",
            inline=False,
        )
        if message.edited_at:
            embed.fields[0].value += "\nLast edited: " + discord.utils.format_dt(message.edited_at, "R")

        if message.reference is not None:
            try:
                ref: discord.Message = await self.bot.get_channel(message.reference.channel_id).fetch_message(
                    message.reference.message_id
                )
            except discord.HTTPException:
                pass
            else:
                embed.add_field(
                    name="In reply to",
                    value=f"[Message by {ref.author.display_name}]({ref.jump_url}):\n>>> ",
                    inline=False,
                )
                field = embed.fields[1]
                if not ref.content:
                    embed.fields[1].value = field.value.replace(":\n>>> ", "")
                else:
                    embed.fields[1].value += textwrap.shorten(ref.content, 1024 - len(field.value), placeholder="...")

        if message.attachments:
            for file in message.attachments:
                name = f"Attachment #{message.attachments.index(file)}"
                spoiler = file.is_spoiler()
                if spoiler:
                    embed.add_field(name=name, value=f"||[{file.filename}]({file.url})||", inline=False)
                else:
                    if file.content_type.startswith("image") and embed.image is discord.Embed.Empty:
                        embed.set_image(url=file.url)
                    embed.add_field(name=name, value=f"[{file.filename}]({file.url})", inline=False)

        # embed.set_footer(text="Starboard threshold for this message was {:.2f}.".format(cap))
        return embed

    @commands.Cog.listener("on_raw_reaction_add")
    @commands.Cog.listener("on_raw_reaction_remove")
    async def on_star_add(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id:
            return
        async with self.lock:
            if str(payload.emoji) != "\N{white medium star}":
                return
            message: discord.Message = await self.bot.get_channel(payload.channel_id).fetch_message(payload.message_id)
            if message.author.id == payload.user_id and payload.event_type == "REACTION_ADD":
                if message.channel.permissions_for(message.guild.me).manage_messages:
                    await message.remove_reaction(payload.emoji, message.author)
                return await message.reply(
                    f"You can't star your own messages you pretentious dick, {message.author.mention}."
                )
            star_count = [x for x in message.reactions if str(x.emoji) == "\N{white medium star}"]
            if not star_count:
                star_count = 0
            else:
                star_count = star_count[0].count

            if star_count == 0:
                try:
                    database: StarBoardMessage = await StarBoardMessage.objects.get(id=payload.message_id)
                except orm.NoMatch:
                    return
                else:
                    channel = discord.utils.get(message.guild.text_channels, name="starboard")
                    if channel:
                        try:
                            message = await channel.fetch_message(database.id)
                            await message.delete(delay=0.1, reason="Starboard message lost all stars.")
                        except discord.HTTPException:
                            pass
                        finally:
                            await database.delete()
                    else:
                        await database.delete()

            database: Tuple[StarBoardMessage, bool] = await StarBoardMessage.objects.get_or_create(
                {"channel": payload.channel_id}, id=payload.message_id
            )
            entry, created = database
            if created:
                # noinspection PyUnresolvedReferences
                cap = message.channel
                if self.bot.intents.members and hasattr(cap, "members"):
                    cap = len([x for x in cap.members if not x.bot]) * 0.1
                else:
                    cap = cap.member_count * 0.1
                if star_count >= cap:
                    channel = discord.utils.get(message.guild.text_channels, name="starboard")
                    if channel and channel.can_send():
                        embed = await self.generate_starboard_embed(message)
                        embeds = [embed, *tuple(filter(lambda x: x.type == "rich", message.embeds))][:10]
                        msg = await channel.send(embeds=embeds)
                        await entry.update(starboard_message=msg.id)
                        self.bot.loop.create_task(self.archive_image(msg))
                else:
                    await entry.delete()
                    return
            else:
                channel = discord.utils.get(message.guild.text_channels, name="starboard")
                embed = await self.generate_starboard_embed(message)
                embeds = [embed, *tuple(filter(lambda x: x.type == "rich", message.embeds))][:10]
                if channel and channel.can_send() and entry.starboard_message:
                    try:
                        msg = await channel.fetch_message(entry.starboard_message)
                    except discord.NotFound:
                        msg = await channel.send(embeds=embeds)
                        await entry.update(starboard_message=msg.id)
                        self.bot.loop.create_task(self.archive_image(msg))
                    except discord.HTTPException:
                        pass
                    else:
                        await msg.edit(embeds=embeds)
                        self.bot.loop.create_task(self.archive_image(msg))

    @commands.message_command(name="Starboard Info")
    @discord.guild_only()
    async def get_starboard_info(self, ctx: discord.ApplicationContext, message: discord.Message):
        return await ctx.respond(embed=await self.generate_starboard_embed(message))


def setup(bot):
    bot.add_cog(StarBoardCog(bot))
