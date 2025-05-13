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
        verify_punishment_roles.start()  # Start the verification task

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
    # Assuming you want to convert to your local timezone
    local_tz = ZoneInfo('America/Los_Angeles')  # Replace with your timezone
    # Make sure the input datetime is timezone-aware
    if utc_datetime.tzinfo is None:
        utc_datetime = utc_datetime.replace(tzinfo=ZoneInfo('UTC'))
    local_datetime = utc_datetime.astimezone(local_tz)
    return local_datetime.timestamp()

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


@bot.tree.command(name="vcmute", description="Ban user from seeing voice, even if rejoin.")
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
    name="vcban", description="Ban user from entire server. Only can see #contact-staff."
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

    expiry_time, error_message = parse_duration(duration)
    if error_message:
        await interaction.response.send_message(
            f"Error: {error_message}", ephemeral=True
        )
        return

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

def parse_duration(duration: str) -> tuple[datetime.datetime, str]:
    if not duration:
        return None, None

    # Valid units mapping
    valid_units = {
        'm': 'minutes',
        'minutes': 'minutes',
        'h': 'hours',
        'hours': 'hours',
        'd': 'days',
        'days': 'days'
    }

    # Remove any leading/trailing whitespace and split by space
    parts = duration.strip().split()
    
    if len(parts) == 1:
        # Handle case with no space (e.g., "30m")
        for i, c in enumerate(parts[0]):
            if c.isalpha():
                value_str = parts[0][:i]
                unit = parts[0][i:].lower()
                break
        else:
            return None, f"Invalid duration format.\nValid units are: m, minutes, h, hours, d, days. Or other error. Error code 0001."
    elif len(parts) == 2:
        # Handle case with space (e.g., "30 minutes")
        value_str = parts[0]
        unit = parts[1].lower()
    else:
        return None, f"Invalid duration format.\nValid units are: m, minutes, h, hours, d, days. Or other error. Error code 0002."

    # Convert value to integer
    try:
        value = int(value_str) if value_str else 0
    except ValueError:
        return None, f"Invalid duration format.\nValid units are: m, minutes, h, hours, d, days. Or other error. Error code 0003."

    # Check if the unit is valid
    if unit not in valid_units:
        return None, f"Invalid time unit: {unit}\nValid units are: m, minutes, h, hours, d, days. Or other error. Error code 0004."

    # Create timedelta based on the unit
    if valid_units[unit] == 'minutes':
        delta = datetime.timedelta(minutes=value)
    elif valid_units[unit] == 'hours':
        delta = datetime.timedelta(hours=value)
    elif valid_units[unit] == 'days':
        delta = datetime.timedelta(days=value)

    # Check minimum duration (1 minute)
    if delta < datetime.timedelta(minutes=1):
        return None, "Duration must be at least 1 minute"

    # Check maximum duration
    if delta > MAX_DURATION:
        delta = MAX_DURATION

    return datetime.datetime.utcnow() + delta, None

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

@tasks.loop(seconds=15)  # New task to verify punishment roles every 15 seconds
async def verify_punishment_roles():
    try:
        now = datetime.datetime.utcnow()
        # Get all active punishments (not expired)
        async with bot.db.execute(
            "SELECT * FROM punishments WHERE expiry_time > ?", (now,)
        ) as cursor:
            active_punishments = await cursor.fetchall()
        
        for punishment in active_punishments:
            user_id, guild_id, role_id, expiry_time, reason, issuer_id = punishment
            guild = bot.get_guild(guild_id)
            if guild:
                member = guild.get_member(user_id)
                if member and not is_protected(member):
                    role = guild.get_role(role_id)
                    if role and role not in member.roles:
                        # Role is missing - reapply it
                        await member.add_roles(role, reason=f"Reapplying punishment role: {reason}")
                        log_channel = bot.get_channel(LOG_CHANNEL_ID)
                        action = "muted" if role_id == MUTE_ROLE_ID else "banned"
                        
                        # Get issuer name if possible
                        issuer = guild.get_member(issuer_id)
                        issuer_name = f"{issuer.mention}" if issuer else "System"
                        
                        await log_channel.send(
                            f"⚠️ Reapplied {action} role to {member.mention}. Role was missing but punishment is still active. "
                            f"Original reason: {reason}. Original issuer: {issuer_name}"
                        )
                        logger.info(f"Reapplied {action} role to {member.display_name} (ID: {member.id})")
    except Exception as e:
        logger.error(f"Error in verify_punishment_roles task: {str(e)}")

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.errors.CheckFailure):
        if "Usage limit exceeded" in str(error):
            await interaction.response.send_message(
                f"Usage limit exceeded. The bot can only be used {MAX_USES_PER_HOUR:d} times per hour. Please try again later. Error code 0005.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "You don't have permission to use this command. Error code 0006.", ephemeral=True
            )
    else:
        await interaction.response.send_message(
            f"An error occurred: {str(error)}", ephemeral=True
        )
        logger.info(f"An error occurred: {str(error)}")


bot.run(BOT_TOKEN)
