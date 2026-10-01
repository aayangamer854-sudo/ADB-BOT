import os
import sys
import json
import time
import re
import asyncio
import logging
from datetime import datetime
import string
import secrets
import discord
from discord.ext import commands, tasks
from discord import app_commands
import docker

# ---------------------------------------------------------
# LOGGING & CONFIG
# ---------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

CONFIG_FILE = "config.json"
DB_FILE = "vps_db.json"
BOT_VERSION = "1.0"
BOT_AUTHOR = "MD AAYAN RAZA (AayanGamerz)"
PREFIX = "!"

if not os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "w") as f:
        json.dump({
            "TOKEN": "YOUR_DISCORD_BOT_TOKEN_HERE", 
            "ADMIN_IDS": [], 
            "DEFAULT_DATA_DIR": "./vps_data",
            "BACKUP_CHANNEL_ID": 0
        }, f, indent=4)

with open(CONFIG_FILE, "r") as f:
    config = json.load(f)

def load_db():
    if not os.path.exists(DB_FILE): return {"vps": {}, "admins": config.get("ADMIN_IDS", []), "settings": {"slots": 10, "deploy_enabled": True}}
    try:
        with open(DB_FILE, "r") as f:
            data = json.load(f)
            if "admins" not in data: data["admins"] = config.get("ADMIN_IDS", [])
            if "settings" not in data: data["settings"] = {"slots": 10, "deploy_enabled": True}
            return data
    except: return {"vps": {}, "admins": config.get("ADMIN_IDS", []), "settings": {"slots": 10, "deploy_enabled": True}}

def save_db(data):
    with open(DB_FILE, "w") as f: json.dump(data, f, indent=4)

try: docker_client = docker.from_env()
except: docker_client = None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# ---------------------------------------------------------
# CLOUD BACKUP ENGINE
# ---------------------------------------------------------
@tasks.loop(hours=12)
async def auto_backup_loop():
    channel_id = config.get("BACKUP_CHANNEL_ID", 0)
    if channel_id and channel_id != 0:
        channel = bot.get_channel(int(channel_id))
        if channel and os.path.exists(DB_FILE):
            try:
                await channel.send(
                    content=f"🔄 **Auto Cloud Backup** • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    file=discord.File(DB_FILE)
                )
            except Exception as e:
                logging.error(f"Failed to send auto-backup: {e}")

# ---------------------------------------------------------
# UTILITIES
# ---------------------------------------------------------
def generate_password(length=12):
    return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))

def create_embed(title, description, color=discord.Color.blue()):
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=f"Made by {BOT_AUTHOR} • v{BOT_VERSION}")
    embed.timestamp = datetime.utcnow()
    return embed

def parse_size_to_bytes(size_str: str) -> int:
    size_str = size_str.lower().strip()
    match = re.match(r"^(\d+)([mg])$", size_str)
    if not match: return 4 * 1024 * 1024 * 1024
    num, unit = int(match.groups()[0]), match.groups()[1]
    return num * 1024 * 1024 if unit == "m" else num * 1024 * 1024 * 1024

def is_admin_slash():
    async def predicate(interaction: discord.Interaction) -> bool:
        db = load_db()
        return interaction.user.id in db.get("admins", []) or interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)

# ---------------------------------------------------------
# TUNNEL PIPELINES
# ---------------------------------------------------------
def _setup_ssh_env(container_id: str, password: str):
    container = docker_client.containers.get(container_id)
    init_cmd = (
        "bash -c '"
        "apt-get update -qq && apt-get install -y -qq openssh-server curl sudo >/dev/null 2>&1 && "
        "mkdir -p /run/sshd && "
        f"echo \"root:{password}\" | chpasswd && "
        "sed -i s/#PermitRootLogin.*/PermitRootLogin\ yes/ /etc/ssh/sshd_config && "
        "sed -i s/#PasswordAuthentication.*/PasswordAuthentication\ yes/ /etc/ssh/sshd_config && "
        "service ssh restart || /usr/sbin/sshd"
        "'"
    )
    container.exec_run(init_cmd, detach=False)

def _start_pinggy(container_id: str) -> dict:
    container = docker_client.containers.get(container_id)
    container.exec_run("pkill -f pinggy", detach=False)
    
    cmd = "bash -c 'nohup ssh -p 443 -R0:localhost:22 -o StrictHostKeyChecking=no tcp@a.pinggy.io > /tmp/pinggy.log 2>&1 &'"
    container.exec_run(cmd, detach=True)
    
    pinggy_data = {"host": "N/A", "port": "N/A"}
    for _ in range(15):
        time.sleep(2)
        res = container.exec_run("cat /tmp/pinggy.log")
        out = res.output.decode("utf-8", errors="ignore")
        match = re.search(r"tcp://([a-zA-Z0-9.-]+):(\d+)", out)
        if match:
            pinggy_data["host"] = match.group(1)
            pinggy_data["port"] = match.group(2)
            break
    return pinggy_data

def _start_sshx(container_id: str) -> str:
    container = docker_client.containers.get(container_id)
    container.exec_run("bash -c 'curl -sSf https://sshx.io/get | sh && nohup sshx > /tmp/sshx.log 2>&1 &'", detach=True)
    for _ in range(10):
        time.sleep(2)
        res = container.exec_run("cat /tmp/sshx.log")
        match = re.search(r"(https://sshx\.io/s/[a-zA-Z0-9_-]+)", res.output.decode("utf-8", errors="ignore"))
        if match: return match.group(1)
    return "N/A"

# ---------------------------------------------------------
# PROVISIONING (DAYTONA FIX)
# ---------------------------------------------------------
def _build_container(os_tag: str, container_name: str, ram_bytes: int = 4*1024*1024*1024):
    data_dir = os.path.abspath(config.get("DEFAULT_DATA_DIR", "./vps_data"))
    os.makedirs(data_dir, exist_ok=True)
    
    try: docker_client.images.get(os_tag)
    except: docker_client.images.pull(os_tag)

    try: docker_client.containers.get(container_name).remove(force=True)
    except: pass

    container = docker_client.containers.run(
        image=os_tag, name=container_name, command="tail -f /dev/null", detach=True, tty=True,
        mem_limit=ram_bytes, privileged=False, cap_add=[],
        volumes={f"{data_dir}/{container_name}": {"bind": "/data", "mode": "rw"}}
    )
    return container

# ---------------------------------------------------------
# UI & VIEWS
# ---------------------------------------------------------
class OSSelectDeploy(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Ubuntu 22.04", value="ubuntu:22.04", description="Stable Ubuntu release"),
            discord.SelectOption(label="Ubuntu 24.04", value="ubuntu:24.04", description="Latest Ubuntu release"),
            discord.SelectOption(label="Debian 11", value="debian:11", description="Debian Bullseye"),
            discord.SelectOption(label="Debian 12", value="debian:12", description="Debian Bookworm")
        ]
        super().__init__(placeholder="Choose an Operating System", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        os_tag = self.values[0]
        user_id = str(interaction.user.id)
        
        container_name = f"vps_{user_id}_{int(time.time())}"
        await interaction.edit_original_response(embed=create_embed("⏳ Deploying", f"Building your `{os_tag}` VPS..."), view=None)
        
        try:
            container = await asyncio.to_thread(_build_container, os_tag, container_name)
            password = generate_password()
            await asyncio.to_thread(_setup_ssh_env, container.id, password)
            pinggy = await asyncio.to_thread(_start_pinggy, container.id)
            
            db = load_db()
            vps_id = container.id[:10]
            db["vps"][vps_id] = {
                "container_id": container.id, "container_name": container_name, "owner_id": interaction.user.id,
                "ram": "4g", "cpu": 2, "disk": "30g", "os": os_tag, "status": "RUNNING", "password": password
            }
            save_db(db)

            dm_embed = create_embed("🚀 VPS Deployed", f"Your server `{vps_id}` is ready!", discord.Color.green())
            dm_embed.add_field(name="Specs", value="**RAM:** 4GB\n**CPU:** 2 Core\n**Disk:** 30GB", inline=False)
            
            if pinggy["host"] != "N/A":
                dm_embed.add_field(name="🔑 Termius Credentials", value=f"**Host:** `{pinggy['host']}`\n**Port:** `{pinggy['port']}`\n**User:** `root`\n**Pass:** `{password}`", inline=False)
            else:
                dm_embed.add_field(name="⚠️ Tunnel Notice", value="Pinggy tunnel blocked on boot. Use the `Pinggy` buttons in `!manage` to force reconnect.", inline=False)

            try:
                await interaction.user.send(embed=dm_embed, view=VPSControls(vps_id, interaction.user.id, container.id))
                await interaction.edit_original_response(embed=create_embed("✅ Deployed", "VPS created! Check your DMs for credentials & panel.", discord.Color.green()))
            except:
                await interaction.edit_original_response(embed=create_embed("✅ Deployed", "VPS created! Please enable DMs and use `!manage`.", discord.Color.orange()))
        except Exception as e:
            await interaction.edit_original_response(embed=create_embed("❌ Error", str(e), discord.Color.red()))

class DeployView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(OSSelectDeploy())

class OSSelectReinstall(discord.ui.Select):
    def __init__(self, vps_id, container_id):
        self.vps_id = vps_id
        self.container_id = container_id
        options = [
            discord.SelectOption(label="Ubuntu 22.04", value="ubuntu:22.04"),
            discord.SelectOption(label="Ubuntu 24.04", value="ubuntu:24.04"),
            discord.SelectOption(label="Debian 11", value="debian:11"),
            discord.SelectOption(label="Debian 12", value="debian:12")
        ]
        super().__init__(placeholder="Choose OS to Reinstall", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        os_tag = self.values[0]
        db = load_db()
        vps = db["vps"][self.vps_id]
        c_name = vps["container_name"]
        
        await interaction.edit_original_response(embed=create_embed("⏳ Reinstalling", f"Wiping data and installing `{os_tag}`..."), view=None)
        
        try:
            container = await asyncio.to_thread(_build_container, os_tag, c_name)
            password = generate_password()
            await asyncio.to_thread(_setup_ssh_env, container.id, password)
            pinggy = await asyncio.to_thread(_start_pinggy, container.id)
            
            db["vps"][self.vps_id]["os"] = os_tag
            db["vps"][self.vps_id]["password"] = password
            db["vps"][self.vps_id]["container_id"] = container.id
            save_db(db)
            
            res_embed = create_embed("✅ Reinstalled", "Your VPS has been freshly installed.", discord.Color.green())
            if pinggy["host"] != "N/A":
                res_embed.add_field(name="🔑 New Credentials", value=f"**Host:** `{pinggy['host']}`\n**Port:** `{pinggy['port']}`\n**User:** `root`\n**Pass:** `{password}`", inline=False)
            else:
                res_embed.add_field(name="⚠️ Tunnel", value="Pinggy blocked. Click the SSH buttons below to retry.", inline=False)
            
            await interaction.edit_original_response(embed=res_embed, view=VPSControls(self.vps_id, interaction.user.id, container.id))
        except Exception as e:
            await interaction.edit_original_response(embed=create_embed("❌ Error", str(e), discord.Color.red()))

class ReinstallView(discord.ui.View):
    def __init__(self, vps_id, container_id):
        super().__init__(timeout=60)
        self.add_item(OSSelectReinstall(vps_id, container_id))

class VPSControls(discord.ui.View):
    def __init__(self, vps_id: str, owner_id: int, container_id: str):
        super().__init__(timeout=None)
        self.vps_id, self.owner_id, self.container_id = vps_id, owner_id, container_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You do not own this VPS.", ephemeral=True)
            return False
        return True

    async def ensure_password(self):
        db = load_db()
        vps = db["vps"].get(self.vps_id)
        if not vps: return None
        pwd = vps.get("password")
        if not pwd:
            pwd = generate_password()
            await asyncio.to_thread(_setup_ssh_env, self.container_id, pwd)
            db["vps"][self.vps_id]["password"] = pwd
            save_db(db)
        return pwd

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, emoji="▶️", row=0)
    async def btn_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            docker_client.containers.get(self.container_id).start()
            db = load_db()
            if self.vps_id in db["vps"]: db["vps"][self.vps_id]["status"] = "RUNNING"; save_db(db)
            await interaction.followup.send("🟢 VPS Started.", ephemeral=True)
        except Exception as e: await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏸️", row=0)
    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            docker_client.containers.get(self.container_id).stop()
            db = load_db()
            if self.vps_id in db["vps"]: db["vps"][self.vps_id]["status"] = "STOPPED"; save_db(db)
            await interaction.followup.send("🔴 VPS Stopped.", ephemeral=True)
        except Exception as e: await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
        
    @discord.ui.button(label="Restart", style=discord.ButtonStyle.primary, emoji="🔄", row=0)
    async def btn_restart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            docker_client.containers.get(self.container_id).restart()
            db = load_db()
            if self.vps_id in db["vps"]: db["vps"][self.vps_id]["status"] = "RUNNING"; save_db(db)
            await interaction.followup.send("🔄 VPS Restarted.", ephemeral=True)
        except Exception as e: await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Reinstall", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def btn_reinstall(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=create_embed("🔄 Reinstall VPS", "Select an OS below to wipe and reinstall your VPS.", discord.Color.orange()), view=ReinstallView(self.vps_id, self.container_id), ephemeral=True)

    @discord.ui.button(label="Pinggy SSH", style=discord.ButtonStyle.secondary, emoji="🔑", row=1)
    async def btn_pinggy_ssh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        pwd = await self.ensure_password()
        if not pwd: return await interaction.followup.send("❌ VPS not found.", ephemeral=True)
        
        await interaction.followup.send("📡 Establishing Pinggy Tunnel... (Please wait ~10s)", ephemeral=True)
        try:
            pinggy = await asyncio.to_thread(_start_pinggy, self.container_id)
            if pinggy["host"] != "N/A":
                msg = f"**🔑 SSH Command:**\n`ssh -p {pinggy['port']} root@{pinggy['host']}`\n**Password:** `{pwd}`"
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.followup.send("❌ Pinggy tunnel blocked.", ephemeral=True)
        except Exception as e: await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Pinggy Termius", style=discord.ButtonStyle.secondary, emoji="📱", row=1)
    async def btn_pinggy_termius(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        pwd = await self.ensure_password()
        if not pwd: return await interaction.followup.send("❌ VPS not found.", ephemeral=True)
        
        await interaction.followup.send("📡 Establishing Pinggy Tunnel... (Please wait ~10s)", ephemeral=True)
        try:
            pinggy = await asyncio.to_thread(_start_pinggy, self.container_id)
            if pinggy["host"] != "N/A":
                msg = f"**🌐 Termius App Details**\n**Host:** `{pinggy['host']}`\n**Port:** `{pinggy['port']}`\n**Username:** `root`\n**Password:** `{pwd}`"
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.followup.send("❌ Pinggy tunnel blocked.", ephemeral=True)
        except Exception as e: await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Web SSHX", style=discord.ButtonStyle.primary, emoji="💻", row=1)
    async def btn_sshx(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("💻 Generating Web SSHX link...", ephemeral=True)
        try:
            link = await asyncio.to_thread(_start_sshx, self.container_id)
            if link != "N/A": await interaction.followup.send(f"✅ **Web Terminal Ready!**\n👉 {link}", ephemeral=True)
            else: await interaction.followup.send("❌ Failed to start SSHX.", ephemeral=True)
        except Exception as e: await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

# ---------------------------------------------------------
# USER TEXT COMMANDS
# ---------------------------------------------------------
@bot.command(name="help")
async def cmd_help(ctx):
    embed = create_embed("📚 Bot Commands", "Manage your VPS directly from Discord.")
    embed.add_field(name="👤 User Commands", value="`!deploy` - Create a free VPS\n`!myvps` - View your VPS\n`!manage` - Control your VPS", inline=False)
    embed.add_field(name="👑 Admin (Slash Commands)", value="`/create` - Custom VPS\n`/setslots` - Set max VPS slots\n`/toggledeploy` - Enable/Disable deployments\n`/list` - View all VPS\n`/deletevps` - Remove a VPS\n`/backup` - Get database backup", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="deploy")
async def cmd_deploy(ctx):
    db = load_db()
    if not db["settings"].get("deploy_enabled", True): return await ctx.send(embed=create_embed("⛔ Deployment Disabled", "Deployments are currently disabled.", discord.Color.red()))
    if len(db.get("vps", {})) >= db["settings"].get("slots", 10): return await ctx.send(embed=create_embed("❌ Server Full", "No slots available.", discord.Color.red()))
    if any(v.get("owner_id") == ctx.author.id for k, v in db.get("vps", {}).items()): return await ctx.send(embed=create_embed("❌ Limit Reached", "You can only deploy 1 VPS.", discord.Color.red()))

    await ctx.send(embed=create_embed("🚀 Deploy Free VPS", "Select your Operating System below.\n*(Specs: 4GB RAM, 2 vCPU, 30GB Disk)*"), view=DeployView())

@bot.command(name="manage")
async def cmd_manage(ctx):
    db = load_db()
    user_vps = [(vps_id, info) for vps_id, info in db.get("vps", {}).items() if info.get("owner_id") == ctx.author.id]
    if not user_vps: return await ctx.send(embed=create_embed("❌ Error", "No active VPS found.", discord.Color.red()))

    for vps_id, info in user_vps:
        embed = create_embed(f"⚙️ Manage VPS - {vps_id}", f"**Status:** {info.get('status', 'RUNNING')}\n**Specs:** {info['ram']} RAM | {info['cpu']} CPU | {info['disk']} Disk")
        try: await ctx.author.send(embed=embed, view=VPSControls(vps_id, ctx.author.id, info['container_id']))
        except discord.Forbidden: return await ctx.send(embed=create_embed("❌ Error", "Please enable DMs.", discord.Color.red()))
    await ctx.send(embed=create_embed("✅ Sent", "Check your DMs for the management panel.", discord.Color.green()))

@bot.command(name="myvps")
async def cmd_myvps(ctx):
    db = load_db()
    user_vps = [(vps_id, info) for vps_id, info in db.get("vps", {}).items() if info.get("owner_id") == ctx.author.id]
    if not user_vps: return await ctx.send(embed=create_embed("❌ Error", "No active VPS found.", discord.Color.red()))
    embed = create_embed("🖥️ Your VPS Instances", "List of your active servers.")
    for vps_id, info in user_vps: embed.add_field(name=f"ID: {vps_id}", value=f"**OS:** {info['os']} | **Status:** {info.get('status', 'RUNNING')}", inline=False)
    await ctx.send(embed=embed)

# ---------------------------------------------------------
# ADMIN SLASH COMMANDS
# ---------------------------------------------------------
@bot.tree.command(name="backup", description="Admin: Get an instant backup of the database in your DMs")
@is_admin_slash()
async def slash_backup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if os.path.exists(DB_FILE):
        try:
            await interaction.user.send("📦 **Here is your manual database backup:**", file=discord.File(DB_FILE))
            await interaction.followup.send("✅ Backup sent to your DMs successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to send DM: {e}", ephemeral=True)
    else:
        await interaction.followup.send("❌ Database file not found.", ephemeral=True)

@bot.tree.command(name="create", description="Admin: Custom Create VPS")
@app_commands.describe(os_tag="Use tags like ubuntu:22.04 or debian:12")
@is_admin_slash()
async def slash_create(interaction: discord.Interaction, user: discord.Member, ram: str, cpu: int, disk: str, os_tag: str):
    await interaction.response.defer(ephemeral=False)
    container_name = f"vps_{user.id}_{int(time.time())}"
    ram_bytes = parse_size_to_bytes(ram)
    
    try:
        container = await asyncio.to_thread(_build_container, os_tag, container_name, ram_bytes)
        password = generate_password()
        await asyncio.to_thread(_setup_ssh_env, container.id, password)
        pinggy = await asyncio.to_thread(_start_pinggy, container.id)
        
        db = load_db()
        vps_id = container.id[:10]
        db["vps"][vps_id] = {
            "container_id": container.id, "container_name": container_name, "owner_id": user.id,
            "ram": ram, "cpu": cpu, "disk": disk, "os": os_tag, "status": "RUNNING", "password": password
        }
        save_db(db)

        dm_embed = create_embed("🚀 Custom VPS Deployed", f"Admin has deployed `{vps_id}` for you!", discord.Color.green())
        dm_embed.add_field(name="Specs", value=f"**RAM:** {ram}\n**CPU:** {cpu} Core\n**Disk:** {disk}", inline=False)
        if pinggy["host"] != "N/A":
            dm_embed.add_field(name="🔑 Termius Credentials", value=f"**Host:** `{pinggy['host']}`\n**Port:** `{pinggy['port']}`\n**User:** `root`\n**Pass:** `{password}`", inline=False)
        else:
            dm_embed.add_field(name="⚠️ Tunnel", value="Pinggy blocked. Use `!manage`.", inline=False)

        try:
            await user.send(embed=dm_embed, view=VPSControls(vps_id, user.id, container.id))
            await interaction.followup.send(embed=create_embed("✅ Created", f"Custom VPS assigned to {user.mention}.", discord.Color.green()))
        except:
            await interaction.followup.send(embed=create_embed("✅ Created", f"VPS assigned to {user.mention} (DMs closed).", discord.Color.orange()))
    except Exception as e:
        await interaction.followup.send(embed=create_embed("❌ Error", str(e), discord.Color.red()))

@bot.tree.command(name="setslots", description="Admin: Set maximum total deployment slots")
@is_admin_slash()
async def slash_setslots(interaction: discord.Interaction, amount: int):
    db = load_db()
    db["settings"]["slots"] = amount
    save_db(db)
    await interaction.response.send_message(embed=create_embed("⚙️ Slots Updated", f"Maximum VPS deployment slots set to **{amount}**.", discord.Color.green()))

@bot.tree.command(name="toggledeploy", description="Admin: Enable or disable the !deploy command")
@is_admin_slash()
async def slash_toggledeploy(interaction: discord.Interaction):
    db = load_db()
    db["settings"]["deploy_enabled"] = not db["settings"].get("deploy_enabled", True)
    save_db(db)
    status = "ENABLED" if db["settings"]["deploy_enabled"] else "DISABLED"
    await interaction.response.send_message(embed=create_embed("⚙️ Deployment Toggled", f"Deploy command is now **{status}**.", discord.Color.green() if status == "ENABLED" else discord.Color.red()))

@bot.tree.command(name="list", description="Admin: View all deployed VPS instances")
@is_admin_slash()
async def slash_list(interaction: discord.Interaction):
    db = load_db()
    if not db.get("vps", {}): return await interaction.response.send_message(embed=create_embed("📄 List", "No active VPS instances."))
    embed = create_embed("📋 Master VPS List", "All active deployments.")
    for vps_id, data in db["vps"].items(): embed.add_field(name=f"ID: {vps_id} | Owner: <@{data.get('owner_id')}>", value=f"**OS:** {data['os']}", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="deletevps", description="Admin: Delete a specific VPS by ID")
@is_admin_slash()
async def slash_deletevps(interaction: discord.Interaction, vps_id: str):
    db = load_db()
    if vps_id not in db.get("vps", {}): return await interaction.response.send_message(embed=create_embed("❌ Error", f"VPS ID `{vps_id}` not found.", discord.Color.red()))
    try: docker_client.containers.get(db["vps"][vps_id]["container_id"]).remove(force=True)
    except: pass
    del db["vps"][vps_id]
    save_db(db)
    await interaction.response.send_message(embed=create_embed("🗑️ Deleted", f"VPS `{vps_id}` has been destroyed.", discord.Color.green()))

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ **Access Denied:** Admin only.", ephemeral=True)
    else:
        logging.error(f"AppCommand error: {error}")

@bot.event
async def on_ready():
    logging.info(f"Bot logged in as {bot.user.name}")
    await bot.tree.sync()
    logging.info("Slash commands synced globally.")
    if not auto_backup_loop.is_running():
        auto_backup_loop.start()

if __name__ == "__main__":
    bot.run(config.get("TOKEN"))
