import discord
from discord import app_commands
from discord.ext import commands, tasks
import datetime
import time
import logging
from zoneinfo import ZoneInfo
import aiosqlite
import os
from collections import deque
from dotenv import load_dotenv

intents = discord.Intents.all()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ModBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.db = None

    async def setup_hook(self):
        # This will sync the application commands with Discord
        await self.tree.sync()
        self.db = await aiosqlite.connect(DB_NAME)
        await self.db.execute(
            """CREATE TABLE IF NOT EXISTS punishments
                      (user_id INTEGER, guild_id INTEGER, role_id INTEGER,
                       expiry_time TIMESTAMP, reason TEXT, issuer_id INTEGER)"""
        )
        await self.db.commit()

    async def on_ready(self):
        logger.info(f"{self.user} has connected to Discord!")
        check_expired_punishments.start()

    async def close(self):
        await self.db.close()
        await super().close()


bot = ModBot()

load_dotenv()
# Configuration
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")  # FILL ME
LOG_CHANNEL_ID = 1222257923921674300  # #mod-lobby
MUTE_ROLE_ID = 1142731909705773126  # "No Voice"
TEMP_BAN_ROLE_ID = 1248595167431233549  # "Temporary Quarantine"
MAX_DURATION = datetime.timedelta(days=30)  # Maximum duration for mute/ban
DB_NAME = "punishments.db"

# Usage tracking
MAX_USES_PER_HOUR = 50
usage_times = deque()

# List of role IDs that are allowed to use the bot commands
ALLOWED_ROLE_IDS = [
    1019142978108919808,  # Admin
    1019211216041824288,  # Senior Moderator
    1026262454101094511,  # Moderator
    1221823875801682071,  # VC Mod
]

# List of role IDs that are protected from the bot's actions
PROTECTED_ROLE_IDS = ALLOWED_ROLE_IDS + [
    1026186011157463183,  # Wick
    1023008166818095164,  # Bots
    1023008026535395381,  # Starboard
    1122896344659529859,  # Recess
    1064063260925624352,  # Thread Manager
    1028848986913787926,  # Engauge
    1024808267156836353,  # Server Stats
    1221214202857783492,  # DISBOARD
    1050213584854065185,  # MoBot
    1163990382946812028,  # PromptInspector
    1182867544932089939,  # Showcase
    1260276484220649614,  # FD VC Bot
]


def utc_to_local_timestamp(utc_datetime):
    local_tz = ZoneInfo(time.tzname[0])
    local_datetime = utc_datetime.replace(tzinfo=ZoneInfo("UTC")).astimezone(local_tz)
    local_timestamp = local_datetime.timestamp()
    return local_timestamp


def has_permission():
    def predicate(interaction: discord.Interaction) -> bool:
        return any(role.id in ALLOWED_ROLE_IDS for role in interaction.user.roles)

    return app_commands.check(predicate)


def is_protected(member: discord.Member) -> bool:
    return any(role.id in PROTECTED_ROLE_IDS for role in member.roles)


def check_usage_limit():
    current_time = time.time()
    usage_times.append(current_time)

    # Remove usage times older than 1 hour
    while usage_times and current_time - usage_times[0] > 3600:
        usage_times.popleft()

    return len(usage_times) <= MAX_USES_PER_HOUR


def usage_limit_check():
    def predicate(interaction: discord.Interaction) -> bool:
        if not check_usage_limit():
            raise app_commands.errors.CheckFailure(
                "Usage limit exceeded. Please try again later."
            )
        return True

    return app_commands.check(predicate)


@bot.tree.command(name="vcmute", description="Mute a user in voice channels")
@app_commands.describe(
    user="The user to mute",
    duration="Duration (e.g. 15m, 3h, or 5d)",
    reason="Reason for the mute",
)
@app_commands.guild_only()
@has_permission()
@usage_limit_check()
async def vcmute(
    interaction: discord.Interaction,
    user: discord.Member,
    duration: str = None,
    reason: str = None,
):
    if is_protected(user):
        await interaction.response.send_message(
            f"Cannot mute {user.mention} as they have a protected role.", ephemeral=True
        )
        return
    await handle_punishment(interaction, user, duration, reason, MUTE_ROLE_ID, "muted")


@bot.tree.command(
    name="vcban", description="Temporarily ban a user from voice channels"
)
@app_commands.describe(
    user="The user to ban",
    duration="Duration (e.g. 15m, 3h, or 5d)",
    reason="Reason for the ban",
)
@app_commands.guild_only()
@has_permission()
@usage_limit_check()
async def vcban(
    interaction: discord.Interaction,
    user: discord.Member,
    duration: str = None,
    reason: str = None,
):
    if is_protected(user):
        await interaction.response.send_message(
            f"Cannot ban {user.mention} as they have a protected role.", ephemeral=True
        )
        return
    await handle_punishment(
        interaction, user, duration, reason, TEMP_BAN_ROLE_ID, "temp banned"
    )


async def handle_punishment(
    interaction: discord.Interaction,
    user: discord.Member,
    duration: str,
    reason: str,
    role_id: int,
    action: str,
):
    role = interaction.guild.get_role(role_id)
    if not role:
        await interaction.response.send_message(
            f"Error: Role not found.", ephemeral=True
        )
        return

    expiry_time = parse_duration(duration)
    if expiry_time is None:
        expiry_time = datetime.datetime.max

    await user.add_roles(role, reason=reason)

    # Store punishment in database
    await bot.db.execute(
        "INSERT INTO punishments VALUES (?, ?, ?, ?, ?, ?)",
        (
            user.id,
            interaction.guild.id,
            role_id,
            expiry_time,
            reason,
            interaction.user.id,
        ),
    )
    await bot.db.commit()

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    await log_action(log_channel, user, action, expiry_time, reason, interaction.user)

    if expiry_time != datetime.datetime.max:
        until_str = f"<t:{utc_to_local_timestamp(expiry_time):.0f}:F>"
    else:
        until_str = "indefinitely"

    await interaction.response.send_message(
        f"{user.mention} has been {action} until {until_str}. Reason: {reason}",
        ephemeral=True,
    )


def parse_duration(duration: str) -> datetime.datetime:
    if not duration:
        return None

    unit = duration[-1].lower()
    try:
        value = int(duration[:-1])
    except ValueError:
        return None

    if unit == "m":
        delta = datetime.timedelta(minutes=value)
    elif unit == "h":
        delta = datetime.timedelta(hours=value)
    elif unit == "d":
        delta = datetime.timedelta(days=value)
    else:
        return None

    if delta > MAX_DURATION:
        delta = MAX_DURATION

    return datetime.datetime.utcnow() + delta


async def log_action(channel, user, action, expiry_time, reason, issuer=None):
    if expiry_time and expiry_time != datetime.datetime.max:
        duration = f"until <t:{utc_to_local_timestamp(expiry_time):.0f}:F>"
    else:
        duration = "indefinitely"

    if issuer:
        logger.info(f"{user.mention} has been {action} {duration} by {issuer.mention}. Reason: {reason}")

        await channel.send(
            f"{user.mention} has been {action} {duration} by {issuer.mention}. Reason: {reason}"
        )
    else:
        logger.info(f"{user.mention} has been un{action} {duration}. Reason: {reason}")

        await channel.send(
            f"{user.mention} has been un{action} {duration}. Reason: {reason}"
        )


@tasks.loop(minutes=1)
async def check_expired_punishments():
    now = datetime.datetime.utcnow()
    async with bot.db.execute(
        "SELECT * FROM punishments WHERE expiry_time <= ?", (now,)
    ) as cursor:
        expired_punishments = await cursor.fetchall()

    for punishment in expired_punishments:
        user_id, guild_id, role_id, _, _, _ = punishment
        guild = bot.get_guild(guild_id)
        if guild:
            member = guild.get_member(user_id)
            if member:
                if is_protected(member):
                    continue  # Skip removing roles from protected users
                role = guild.get_role(role_id)
                if role and role in member.roles:
                    await member.remove_roles(role)
                    log_channel = bot.get_channel(LOG_CHANNEL_ID)
                    action = "muted" if role_id == MUTE_ROLE_ID else "banned"
                    await log_action(
                        log_channel, member, action, None, "Punishment duration expired"
                    )

    # Remove expired punishments from the database
    await bot.db.execute("DELETE FROM punishments WHERE expiry_time <= ?", (now,))
    await bot.db.commit()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.errors.CheckFailure):
        if "Usage limit exceeded" in str(error):
            await interaction.response.send_message(
                f"Usage limit exceeded. The bot can only be used {MAX_USES_PER_HOUR:d} times per hour. Please try again later.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
    else:
        await interaction.response.send_message(
            f"An error occurred: {str(error)}", ephemeral=True
        )


bot.run(BOT_TOKEN)
