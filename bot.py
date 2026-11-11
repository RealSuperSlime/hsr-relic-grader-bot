"""HSR Relic Grader - Discord bot.

    !grade            pick a character from a list, then upload the relic screenshot
    !grade Acheron    skip the picker (name is fuzzy: 'acheron', 'dan heng', ...)
    !characters       how many characters are loaded, plus a few examples
    /grade            same thing as a slash command

OCR and scoring both run locally in this process; nothing is uploaded to a
third-party service.

Setup: copy .env.example to .env, paste your token after DISCORD_BOT_TOKEN=,
then run start_bot.bat. The .env file is git-ignored, so it never gets
uploaded anywhere. A DISCORD_BOT_TOKEN environment variable takes precedence.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import sys
import time
import traceback
import uuid

import discord
from discord import app_commands
from discord.ext import commands

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from relic_grades.characters import all_character_names, find_character  # noqa: E402
from relic_grades.envfile import load_env  # noqa: E402
from relic_grades.grade import grade_image  # noqa: E402
from relic_grades.paths import data_dir  # noqa: E402
from relic_grades.showcase import ShowcaseError, showcase  # noqa: E402

ENV_FILE = os.path.join(HERE, '.env')              # your secrets live here (git-ignored)
ENV_EXAMPLE_FILE = os.path.join(HERE, '.env.example')
GUILD_FILE = os.path.join(HERE, 'guild.txt')       # optional: paste a server ID here
GUILD_EXAMPLE_FILE = os.path.join(HERE, 'guild.txt.example')
LEGACY_TOKEN_FILE = os.path.join(HERE, 'token.txt')  # pre-.env location, still read if present
PREFIX = '!'
UID_RE = re.compile(r'\d{6,12}')      # UIDs are digits; never trust one into a URL

# The engine may be imported from a pip install (requirements-bot.txt) rather than
# from the checkout next to this file - and then it looks for Tesseract beside
# site-packages, where the bundled copy is not. Point it at the bundle we ship.
_VENDOR_TESSERACT = os.path.join(ROOT, 'vendor', 'tesseract', 'tesseract.exe')
if not os.environ.get('RELIC_TESSERACT') and os.path.isfile(_VENDOR_TESSERACT):
    os.environ['RELIC_TESSERACT'] = _VENDOR_TESSERACT
PAGE_SIZE = 25
IMAGE_TIMEOUT = 180                       # seconds to wait for the screenshot
GRADE_COLORS = {
    'S+': 0xD8A12B, 'S': 0xE11D2E, 'A': 0xFF7A45, 'B': 0x9A7BFF,
    'C': 0x4AA8FF, 'D': 0x7D93A3, 'F': 0x6B7479,
}

# Only the intents this bot actually consumes: guild + DM messages and their
# content. Members/Presence (the other privileged ones) stay off.
intents = discord.Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.dm_messages = True
intents.message_content = True            # required for !prefix commands
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None,
                   # never let user text turn into @everyone/@role pings
                   allowed_mentions=discord.AllowedMentions.none())


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _looks_like_token(value: str) -> bool:
    """A real bot token is three dot-separated parts; this keeps a stray comment
    line in the legacy file from being sent to Discord as the token."""
    return value.count('.') == 2 and len(value) >= 50 and not value.startswith('PASTE')


def load_token() -> str:
    """Token lookup order: DISCORD_BOT_TOKEN / DISCORD_TOKEN env var, then .env
    (under either name), then the legacy token.txt."""
    load_env(ENV_FILE)
    token = (os.environ.get('DISCORD_BOT_TOKEN')
             or os.environ.get('DISCORD_TOKEN') or '').strip()
    if not token and os.path.isfile(LEGACY_TOKEN_FILE):
        with open(LEGACY_TOKEN_FILE, encoding='utf-8') as f:
            legacy = f.read().strip()
        if _looks_like_token(legacy):
            print('  note: reading the token from token.txt - it now belongs in .env, '
                  'you can delete token.txt.')
            token = legacy
    if not token or token.startswith('PASTE'):
        raise SystemExit(
            'No bot token found.\n'
            f'  1. copy  {ENV_EXAMPLE_FILE}\n'
            f'     to    {ENV_FILE}\n'
            '  2. paste your bot token after DISCORD_BOT_TOKEN=\n'
            '  3. run start_bot.bat again\n'
            '  (.env is git-ignored - it is never uploaded anywhere)')
    return token


def autocomplete_matches(current: str, limit: int = 25) -> list[str]:
    """Character names for the autocomplete hint.

    Prefix matches first, then substring matches, then - if the player is still
    typing with a typo - the closest names by similarity, so the list is never
    empty while something plausible exists."""
    from difflib import SequenceMatcher

    query = (current or '').strip().lower()
    names = all_character_names()
    if not query:
        return names[:limit]
    starts = [n for n in names if n.lower().startswith(query)]
    if len(starts) >= limit:
        return starts[:limit]
    rest = [n for n in names if query in n.lower() and not n.lower().startswith(query)]
    out = (starts + rest)[:limit]
    if out:
        return out
    scored = []
    for n in names:
        key = n.lower().replace('the ', '')
        scored.append((max(SequenceMatcher(None, query, key).ratio(),
                           SequenceMatcher(None, query, n.lower()).ratio()), n))
    scored.sort(key=lambda kv: -kv[0])
    return [n for score, n in scored if score >= 0.5][:limit]


def load_guild_id() -> int | None:
    """Optional server ID. When set, slash commands sync to that server
    instantly instead of waiting on Discord's global propagation. Read from the
    DISCORD_GUILD_ID env var, .env, or guild.txt - in that order."""
    load_env(ENV_FILE)
    raw = os.environ.get('DISCORD_GUILD_ID', '').strip()
    if not raw and os.path.isfile(GUILD_FILE):
        with open(GUILD_FILE, encoding='utf-8') as f:
            raw = f.read().strip()
    raw = ''.join(ch for ch in raw if ch.isdigit())
    return int(raw) if raw else None


def grade_color(letter: str) -> int:
    key = (letter or 'F').strip()
    return GRADE_COLORS.get(key, GRADE_COLORS.get(key[:1], 0x6B7479))


MAX_IMAGE_BYTES = 8 * 1024 * 1024      # a relic screenshot is a few hundred KB


def find_tesseract_quiet() -> str:
    """For logging only: which tesseract the OCR would use, or why it can't."""
    from relic_grades.ocr import find_tesseract
    try:
        return find_tesseract()
    except Exception as e:
        return f'NOT FOUND ({type(e).__name__})'

# --------------------------------------------------------------------------- #
# keeping this machine out of Discord
#
# A raw exception is enough to hand a stranger the OS, the username and the
# folder layout of whatever box the bot runs on, so nothing user-visible is
# ever built from exception text. The detail goes to the console instead.
# --------------------------------------------------------------------------- #
_DRIVE_RE = re.compile(r'[A-Za-z]:[\\/][^\s`"\')]*')
_UNC_RE = re.compile(r'\\\\[^\s`"\')]+')
_MACHINE_WORDS = re.compile(r'(?<![\w/])(?:site-packages|AppData|Program Files|Users)(?![\w-])',
                            re.IGNORECASE)
_HOME = os.path.expanduser('~')
_USERNAME = os.environ.get('USERNAME') or os.environ.get('USER') or ''


def scrub(text: str) -> str:
    """Remove anything that describes this machine from user-visible text."""
    if not text:
        return text
    text = _DRIVE_RE.sub('<path>', text)
    text = _UNC_RE.sub('<path>', text)
    if _HOME:
        text = text.replace(_HOME, '<home>')
    if len(_USERNAME) > 2:
        text = re.sub(re.escape(_USERNAME), '<user>', text, flags=re.IGNORECASE)
    return _MACHINE_WORDS.sub('<path>', text)


LOG_DIR = os.path.join(HERE, 'logs')
LOG_FILE = os.path.join(LOG_DIR, 'errors.log')


def error_ref(exc: BaseException, **context) -> str:
    """Log the real error for whoever runs the bot; give the user a code.

    Written to stderr *and* to logs/errors.log with a full traceback, so a
    failure can be diagnosed after the console window is long gone. The file is
    git-ignored: it contains host paths.
    """
    ref = uuid.uuid4().hex[:8]
    extra = ' '.join(f'{k}={v}' for k, v in context.items())
    detail = f'[error {ref}] {type(exc).__name__}: {exc}' + (f'  ({extra})' if extra else '')
    print(detail, file=sys.stderr, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'\n=== {time.strftime("%Y-%m-%d %H:%M:%S")} {detail}\n')
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except Exception:
        pass
    return ref


def failure_hint(exc: BaseException) -> str:
    """What went wrong, in words, without exposing this machine.

    A bare error code was useless to whoever was holding the bot: they cannot act
    on "05405fbf". These categories say enough to self-diagnose - "the host has no
    OCR engine" is actionable, "C:\\Users\\...\\tesseract.exe is missing" is not.
    """
    text = f'{type(exc).__name__}: {exc}'.lower()
    if 'tesseract' in text:
        return "the bot's machine has no OCR engine set up"
    if isinstance(exc, FileNotFoundError) or 'no such file' in text:
        return "a file the bot needs is missing on its machine"
    if isinstance(exc, PermissionError) or 'access is denied' in text:
        return "the bot's machine blocked access to a file"
    if 'unidentifiedimage' in text or 'cannot identify image' in text:
        return 'that file could not be read as an image'
    if any(w in text for w in ('urlopen', 'connection', 'timed out', 'getaddrinfo')):
        return 'a network request failed'
    return 'the bot hit an internal error'


def with_code(message: str, exc: BaseException, **context) -> str:
    """A user-facing failure: plain wording, no host detail, plus a log code."""
    ref = error_ref(exc, **context)
    return scrub(f'{message}\nReason: {failure_hint(exc)}. '
                 f'Error code `{ref}` (the details are in the log, not here).')


def is_image_attachment(att: discord.Attachment) -> bool:
    if att.size and att.size > MAX_IMAGE_BYTES:
        return False
    if (att.content_type or '').lower().startswith('image/'):
        return True
    return att.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp'))


def _num(v, fmt: str = 'g') -> str:
    return format(v, fmt) if isinstance(v, (int, float)) else '?'


def build_embed(payload: dict) -> discord.Embed:
    relic, score = payload['relic'], payload['score']
    character = payload['character']
    grade = score['grade']
    embed = discord.Embed(
        title=relic.get('piece') or 'Relic',
        description=(f"{relic.get('set') or '?'} · {relic.get('part') or '?'} "
                     f"· {relic.get('grade', 5)}★ +{relic.get('level', 15)}"),
        color=grade_color(grade))
    embed.add_field(name='Score', value=f"## {score['value']:.1f}\n**{grade}**", inline=True)
    embed.add_field(name='Main stat',
                    value=f"**{relic.get('mainStat')} {_num(relic.get('mainRead') or relic.get('mainValue'))}**",
                    inline=True)
    embed.add_field(name='Scored for', value=f"**{character['name']}**", inline=True)

    lines = []
    for s in payload['substats']:
        shown = _num(s.get('shown'))
        badge = f"×{s['badge']}" if s.get('badge') else '—'
        if s['weight'] > 0:
            lines.append(f"`{s['stat']:<16} {shown:>6}`  {badge:>3}  w **{s['weight']:.2f}** → **{s['weighted']:.2f}**")
        else:
            lines.append(f"`{s['stat']:<16} {shown:>6}`  {badge:>3}  w —     → 0.00")
    embed.add_field(name='Substats  ·  card · rolls · weight · points',
                    value='\n'.join(lines) or 'none read', inline=False)

    breakdown = (f"substat points **{score['raw']:.2f}** / ideal **{score['ideal']:.2f}**")
    if not score['correctMain']:
        breakdown += '\n⚠ main stat is off-meta for this character'
    embed.add_field(name='Breakdown', value=breakdown, inline=False)

    warnings = relic.get('warnings') or []
    if warnings:
        embed.add_field(name='⚠ OCR warnings',
                        value=scrub('\n'.join(f'• {w}' for w in warnings))[:1000],
                        inline=False)
    embed.set_footer(text='Fribbels-identical scoring · OCR and grading ran locally')
    return embed


def build_character_embed(character: dict) -> discord.Embed:
    """One showcased character: total grade on top, every piece under it."""
    name = character['name']
    total, rating = character.get('total'), character.get('rating')
    title = f"{name} — {total:.1f} {rating}" if total is not None else f'{name} — not graded'
    bits = []
    if character.get('level'):
        bits.append(f"Lv {character['level']}")
    if character.get('eidolon'):
        bits.append(f"E{character['eidolon']}")
    if not character.get('known'):
        bits.append('no weights for this character yet')
    graded = [r for r in character['relics'] if r['score'] is not None]
    if total is not None:
        bits.append(f'average of {len(graded)} pieces')
    embed = discord.Embed(title=title[:250],
                          description=' · '.join(bits) or None,
                          color=grade_color(rating or 'F'))

    for relic in character['relics']:
        if relic['score'] is None:
            value = f"{relic.get('piece') or relic['slot']}\nmain **{relic['main']}**"
        else:
            lines = [f"**{relic['score']:.1f} {relic['rating']}**",
                     f"{(relic.get('piece') or relic['slot'])[:80]}",
                     f"main {relic['main']}"]
            for s in relic['subs']:
                badge = f"×{s['badge']}" if s.get('badge') else ''
                if s.get('weight'):
                    lines.append(f"`{s['stat'][:16]:<16} {s['shown']:>6} {badge:>3}` → {s['weighted']:.1f}")
                else:
                    lines.append(f"`{s['stat'][:16]:<16} {s['shown']:>6} {badge:>3}`")
            value = '\n'.join(lines)
        embed.add_field(name=relic['slot'][:250], value=value[:1020], inline=True)
    return embed


# --------------------------------------------------------------------------- #
# character picker (paged, because Discord allows 25 options per select)
# --------------------------------------------------------------------------- #
class CharacterSelect(discord.ui.Select):
    def __init__(self, names: list[str], on_pick):
        super().__init__(placeholder='Choose a character…', min_values=1, max_values=1,
                         options=[discord.SelectOption(label=n[:100], value=n) for n in names])
        self._on_pick = on_pick

    async def callback(self, interaction: discord.Interaction):
        await self._on_pick(interaction, self.values[0])


class PickerView(discord.ui.View):
    def __init__(self, names: list[str], on_pick):
        super().__init__(timeout=IMAGE_TIMEOUT)
        self.names = names
        self.on_pick = on_pick
        self.page = 0
        self.pages = max(1, (len(names) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._render()

    def _render(self):
        self.clear_items()
        start = self.page * PAGE_SIZE
        self.add_item(CharacterSelect(self.names[start:start + PAGE_SIZE], self.on_pick))
        if self.pages > 1:
            prev = discord.ui.Button(label='◀ Prev', style=discord.ButtonStyle.secondary,
                                     disabled=self.page == 0, row=1)
            nxt = discord.ui.Button(label='Next ▶', style=discord.ButtonStyle.secondary,
                                    disabled=self.page >= self.pages - 1, row=1)
            prev.callback = self._prev
            nxt.callback = self._next
            self.add_item(prev)
            self.add_item(nxt)

    def _label(self) -> str:
        return f'**Step 1 · pick your character**  ·  page {self.page + 1}/{self.pages}'

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._render()
        await interaction.response.edit_message(content=self._label(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self.pages - 1, self.page + 1)
        self._render()
        await interaction.response.edit_message(content=self._label(), view=self)


# --------------------------------------------------------------------------- #
# the flow (shared by !grade and /grade)
# --------------------------------------------------------------------------- #
async def wait_for_image(author, channel, send):
    def check(m: discord.Message):
        return (m.author == author and m.channel == channel
                and any(is_image_attachment(a) for a in m.attachments))
    try:
        msg = await bot.wait_for('message', check=check, timeout=IMAGE_TIMEOUT)
    except asyncio.TimeoutError:
        await send(content=f'Timed out waiting for the screenshot '
                           f'({IMAGE_TIMEOUT // 60} min). Run the command again.')
        return None
    return next(a for a in msg.attachments if is_image_attachment(a))


async def grade_and_reply(send, character_name: str, attachment: discord.Attachment):
    notice = await send(content=f'Reading the screenshot for **{character_name}**…')
    try:
        data = await attachment.read()
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(None, grade_image, io.BytesIO(data), character_name)
    except Exception as e:
        await notice.edit(content=with_code(
            'Could not read that screenshot — try a tighter crop of just the relic card.',
            e, bytes=len(data), content_type=attachment.content_type or '?',
            data_dir=data_dir(), tesseract=find_tesseract_quiet()))
        return
    await notice.edit(content=None, embed=build_embed(payload))


async def run_flow(author, channel, send, character_query=None):
    if character_query:
        try:
            found = find_character(character_query)
        except ValueError:
            await send(content=f'No character matching `{character_query}`. '
                               f'Try `{PREFIX}grade` to pick from the list.')
            return
        await send(content=f'**Step 2 · upload the relic screenshot** for **{found["name"]}**\n'
                           f'Attach the image to your next message ({IMAGE_TIMEOUT // 60} min to wait).')
        attachment = await wait_for_image(author, channel, send)
        if attachment:
            await grade_and_reply(send, found['name'], attachment)
        return

    names = all_character_names()
    picked: dict = {}

    async def on_pick(interaction: discord.Interaction, value: str):
        if interaction.user != author:
            await interaction.response.send_message(
                f'This picker belongs to someone else — run `{PREFIX}grade` yourself.',
                ephemeral=True)
            return
        picked['name'] = value
        await interaction.response.edit_message(
            content=f'**Step 2 · upload the relic screenshot** for **{value}**\n'
                    f'Attach the image to your next message here.', view=None)

    view = PickerView(names, on_pick)
    await send(content=f'**Step 1 · pick your character**  ·  page 1/{view.pages}'
                       f'  ({len(names)} characters)', view=view)

    for _ in range(IMAGE_TIMEOUT * 2):
        if picked.get('name'):
            break
        await asyncio.sleep(0.5)
    if not picked.get('name'):
        return
    attachment = await wait_for_image(author, channel, send)
    if attachment:
        await grade_and_reply(send, picked['name'], attachment)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
@bot.command(name='grade')
async def grade_cmd(ctx, *, character: str = ''):
    """Grade a relic screenshot for a character."""
    await run_flow(ctx.author, ctx.channel, ctx.send, character.strip() or None)


async def run_uid_lookup(send, uid: str, character: str = ''):
    uid = (uid or '').strip()
    if not uid:
        await send(f'Usage: `{PREFIX}uid <uid>` — e.g. `{PREFIX}uid 101000000`\n'
                   f'Add a name to filter: `{PREFIX}uid 101000000 herta`')
        return
    if not UID_RE.fullmatch(uid):
        await send('That does not look like a UID — they are digits only, '
                   'e.g. `101000000`.')
        return
    notice = await send(f'Fetching showcase **{uid}**…')
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, showcase, uid, character or None)
    except ShowcaseError as e:
        await notice.edit(content=scrub(f'⚠ {e}'))
        return
    except Exception as e:
        await notice.edit(content=with_code('Could not read that profile.', e))
        return

    characters = data['characters']
    if not characters:
        await notice.edit(content=(f"No showcased character matching `{character}` "
                                   f"for **{data['nickname']}**."))
        return

    graded = [c for c in characters if c['total'] is not None]
    header = (f"**{data['nickname']}** · UID `{data['uid']}` · {data['total_characters']} "
              f"character(s) on showcase")
    if graded:
        best = max(graded, key=lambda c: c['total'])
        header += f"\nbest piece set: **{best['name']} {best['total']:.1f} {best['rating']}**"
    try:
        await notice.edit(content=header, embeds=[build_character_embed(c) for c in characters][:10])
    except (discord.HTTPException, AttributeError):
        await notice.edit(content=header)
        for c in characters[:10]:
            await send(embed=build_character_embed(c))


@bot.command(name='uid')
async def uid_cmd(ctx, uid: str = '', *, character: str = ''):
    """Look up a player's UID and grade every character on their profile."""
    await run_uid_lookup(ctx.send, uid, character)


@bot.command(name='characters')
async def characters_cmd(ctx):
    """Show how many characters are available."""
    names = all_character_names()
    await ctx.send(f'**{len(names)} characters** loaded (Fribbels weights).\n'
                   f'e.g. {", ".join(names[:12])} …')


@bot.command(name='graderhelp')
async def graderhelp_cmd(ctx):
    await ctx.send(embed=discord.Embed(
        title='HSR Relic Grader',
        description=(f'`{PREFIX}grade` — pick a character, then upload the screenshot\n'
                     f'`{PREFIX}grade Acheron` — skip the picker\n'
                     f'`{PREFIX}uid <uid>` — grade every character on that player\'s showcase\n'
                     f'`{PREFIX}uid <uid> herta` — just the characters matching a name\n'
                     f'`{PREFIX}characters` — what is loaded\n'
                     f'`/grade`, `/uid` — slash versions\n\n'
                     'Scoring is identical to Fribbels HSR Optimizer; OCR runs locally.'),
        color=0xE11D2E))


@bot.tree.command(name='grade', description='Grade a HSR relic screenshot for a character')
@app_commands.describe(character='Character name - start typing, e.g. "the"')
async def grade_slash(interaction: discord.Interaction, character: str = ''):
    await interaction.response.defer()
    await run_flow(interaction.user, interaction.channel, interaction.followup.send,
                   character.strip() or None)


@grade_slash.autocomplete('character')
async def grade_slash_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n) for n in autocomplete_matches(current, 25)]


@bot.tree.command(name='uid', description="Grade every character on a player's showcase")
@app_commands.describe(uid='Player UID (9 digits, e.g. 101000000)',
                       character='Optional character name filter - start typing')
async def uid_slash(interaction: discord.Interaction, uid: str, character: str = ''):
    await interaction.response.defer()
    await run_uid_lookup(interaction.followup.send, uid, character)


@uid_slash.autocomplete('character')
async def uid_slash_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n) for n in autocomplete_matches(current, 25)]


async def sync_commands(verbose: bool = True) -> list:
    """Register the slash commands. With guild.txt set this is instant; without
    it Discord takes up to an hour to publish global commands."""
    guild_id = load_guild_id()
    if guild_id:
        guild = discord.Object(id=guild_id)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        target = f'guild {guild_id}'
    else:
        synced = await bot.tree.sync()
        target = 'globally (can take up to an hour to appear)'
    if verbose:
        names = ', '.join(sorted(c.name for c in synced))
        print(f'Synced {len(synced)} slash command(s) {target}: {names}')
    return synced


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} ({bot.user.id})')
    print(f'{len(all_character_names())} characters loaded. Type {PREFIX}grade in Discord.')
    try:
        await sync_commands()
        if not load_guild_id():
            print('  tip: paste your server ID into guild.txt for instant command updates.')
    except discord.Forbidden:
        print('Slash command sync failed: the bot is missing the applications.commands scope.\n'
              '  Re-invite it with scopes "bot" + "applications.commands".')
    except Exception as e:
        print(f'Slash command sync failed: {type(e).__name__}: {e}')
    await bot.change_presence(activity=discord.Game(name=f'{PREFIX}grade'))


@bot.command(name='sync')
@commands.is_owner()
async def sync_cmd(ctx):
    """(owner) force a slash-command re-sync."""
    try:
        synced = await sync_commands()
        await ctx.send(f'Re-synced {len(synced)} commands: '
                       f'{", ".join(sorted(c.name for c in synced))}\n'
                       f'If they still do not show, press Ctrl+R in Discord.')
    except Exception as e:
        await ctx.send(with_code('Sync failed.', e))


@sync_cmd.error
async def sync_cmd_error(ctx, error):
    if isinstance(error, commands.NotOwner):
        await ctx.send('Only the bot owner can run that.')


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, (commands.NotOwner, commands.MissingPermissions)):
        await ctx.send('Only the bot owner can run that.')
        return
    await ctx.send(with_code('Something went wrong running that command.', error))


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction,
                               error: app_commands.AppCommandError):
    """Slash commands had no handler at all: an exception there reached the user
    as Discord's own traceback text. Keep the detail local, answer in private."""
    message = with_code('Something went wrong running that command.', error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


def main() -> None:
    token = load_token()
    try:
        bot.run(token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        print("""
[!] The bot logged in but Discord refused it: the Message Content intent is OFF.

    Fix it in 3 clicks:
      1. open  https://discord.com/developers/applications/
      2. pick your application -> Bot  (left sidebar)
      3. scroll to "Privileged Gateway Intents" -> turn ON "Message Content Intent"
         -> Save Changes
    Then run start_bot.bat again. (Keep "Server Members" and "Presence" OFF.)
""")
        raise SystemExit(1)
    except discord.LoginFailure:
        print("""
[!] Discord rejected the token.

    Open .env and make sure DISCORD_BOT_TOKEN holds ONLY the token - no "Bot " prefix,
    no quotes, no extra lines. If you reset the token in the portal, paste the
    new one. A token looks like: MTIzNDU2Nzg5.GhIjKl.<26 more characters>
""")
        raise SystemExit(1)


if __name__ == '__main__':
    main()
