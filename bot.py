import discord
from discord.ext import commands, tasks
from discord import ui
import os, re, asyncio, time, io
from dotenv import load_dotenv
from pymongo import MongoClient
from flask import Flask
import threading

load_dotenv()

# ================== CONFIG ==================
TOKEN = os.getenv("TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
LTC_ADDRESS = os.getenv("LTC_ADDRESS")
VOUCH_CHANNEL_ID = 1470906406205915385
PROOF_CHANNEL_ID = int(os.getenv("PROOF_CHANNEL_ID"))
DEALER_ROLE_ID = int(os.getenv("DEALER_ROLE_ID"))
BUYER_ROLE_ID = int(os.getenv("BUYER_ROLE_ID"))
HEAD_DEALER_ROLE_ID = int(os.getenv("HEAD_DEALER_ROLE_ID"))
PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID"))
TRANSCRIPT_CHANNEL_ID = int(os.getenv("TRANSCRIPT_CHANNEL_ID"))
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID"))
TICKET_MANAGER_ROLE_ID = int(os.getenv("TICKET_MANAGER_ROLE_ID"))  # NEW — add this to your .env

# ================== MONGO ==================
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["ticket_bot"]
deals_col = db["active_deals"]
panels_col = db["panels"]
tickets_col = db["tickets"]
ltc_col = db["dealer_ltc"]
upi_col = db["dealer_upi"]
proof_col = db["awaiting_proof"]  # persists proof-wait state across restarts
proposals_col = db["deal_proposals"]  # persists pending deal proposals across restarts

# ================== BOT ==================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=".", intents=intents)
bot.remove_command("help")  # Remove built-in help so we can define our own

active_deals = {}
awaiting_proof = {}   # channel_id -> deal_doc (set after buyer confirms delivery)

# ================== ROLE HELPERS ==================
def is_dealer(member):
    return any(r.id == DEALER_ROLE_ID for r in member.roles)

def is_head_dealer(member):
    return any(r.id == HEAD_DEALER_ROLE_ID for r in member.roles)

def is_ticket_manager(member):
    return any(r.id == TICKET_MANAGER_ROLE_ID for r in member.roles)

def can_manage_ticket(member):
    """Dealer, Head Dealer, or Ticket Manager."""
    return is_dealer(member) or is_head_dealer(member) or is_ticket_manager(member)

# ================== EMBED HELPER ==================
def make_embed(title=None, description=None, color=discord.Color.blurple(),
               fields=None, footer=None, thumbnail=None, image=None):
    embed = discord.Embed(title=title, description=description, color=color)
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    if footer:
        embed.set_footer(text=footer)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if image:
        embed.set_image(url=image)
    return embed

# ================== LTC COMMAND ==================
@bot.command()
async def ltc(ctx, address: str):
    if not is_dealer(ctx.author):
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "Only dealers can set an LTC address.", discord.Color.red()))

    ltc_col.update_one(
        {"dealer_id": ctx.author.id},
        {"$set": {"address": address}},
        upsert=True
    )
    await ctx.send(embed=make_embed(
        "✅ LTC Address Saved",
        "Your LTC address has been updated.",
        discord.Color.green(),
        fields=[("Address", f"{address}", False)],
        footer="This address will be shown to buyers during deals."
    ))

def get_dealer_ltc(dealer_id):
    data = ltc_col.find_one({"dealer_id": dealer_id})
    return data["address"] if data else LTC_ADDRESS

# ================== UPI COMMAND ==================
@bot.command()
async def upi(ctx, upi_id: str, image_url: str):
    if not is_dealer(ctx.author):
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "Only dealers can set a UPI ID.", discord.Color.red()))

    upi_col.update_one(
        {"dealer_id": ctx.author.id},
        {"$set": {"upi_id": upi_id, "image_url": image_url}},
        upsert=True
    )
    await ctx.send(embed=make_embed(
        "✅ UPI Details Saved",
        "Your UPI payment details have been updated.",
        discord.Color.green(),
        fields=[
            ("UPI ID", f"`{upi_id}`", False),
            ("QR Image URL", image_url, False),
        ],
        footer="Buyers will see UPI as a payment option in your deals."
    ))

def get_dealer_upi(dealer_id):
    data = upi_col.find_one({"dealer_id": dealer_id})
    return data if data else None

# ================== PAYMENT CHOICE VIEW ==================
class PaymentChoiceView(ui.View):
    """Persistent — lets buyer pick UPI or LTC. Fetches deal/payment info from DB on each click."""

    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="💳 Pay via UPI", style=discord.ButtonStyle.primary, custom_id="persistent_pay_upi")
    async def pay_upi(self, interaction: discord.Interaction, button: ui.Button):
        deal = deals_col.find_one({"channel_id": interaction.channel.id})
        if not deal:
            return await interaction.response.send_message(
                embed=make_embed("❌ Error", "Deal data not found.", discord.Color.red()), ephemeral=True)
        if interaction.user.id != deal["buyer"]:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the buyer can select a payment method.", discord.Color.red()),
                ephemeral=True)
        upi_data = get_dealer_upi(deal["dealer"])
        if not upi_data:
            return await interaction.response.send_message(
                embed=make_embed("❌ UPI Not Set", "The dealer has not set a UPI address.", discord.Color.red()),
                ephemeral=True)
        embed = make_embed(
            title="💳 UPI Payment Details",
            description="Please pay using the UPI details below.",
            color=discord.Color.blue(),
            fields=[
                ("📦 Product", deal["product"], True),
                ("💰 Amount", deal["amount"], True),
                ("🪪 UPI ID", f"`{upi_data['upi_id']}`", False),
            ],
            footer="Scan the QR code or copy the UPI ID to pay.",
            image=upi_data.get("image_url")
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ui.button(label="🪙 Pay via LTC", style=discord.ButtonStyle.secondary, custom_id="persistent_pay_ltc")
    async def pay_ltc(self, interaction: discord.Interaction, button: ui.Button):
        deal = deals_col.find_one({"channel_id": interaction.channel.id})
        if not deal:
            return await interaction.response.send_message(
                embed=make_embed("❌ Error", "Deal data not found.", discord.Color.red()), ephemeral=True)
        if interaction.user.id != deal["buyer"]:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the buyer can select a payment method.", discord.Color.red()),
                ephemeral=True)
        dealer_ltc = get_dealer_ltc(deal["dealer"])
        embed = make_embed(
            title="🪙 LTC Payment Details",
            description="Please send LTC to the address below.",
            color=discord.Color.gold(),
            fields=[
                ("📦 Product", deal["product"], True),
                ("💰 Amount", deal["amount"], True),
                ("🪙 LTC Address", f"{dealer_ltc}", False),
            ],
            footer="Send exact amount and send screenshot once payment is sent."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ================== POST-DEAL CONFIRM VIEW ==================
class PostDealConfirmView(ui.View):
    """Persistent — buyer confirms or unconfirms delivery. Fetches deal from DB on each click."""

    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="✅ Confirm Delivery", style=discord.ButtonStyle.success, custom_id="confirm_delivery")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        deal = deals_col.find_one({"channel_id": interaction.channel.id})
        if not deal:
            return await interaction.response.send_message(
                embed=make_embed("❌ Error", "Deal data not found.", discord.Color.red()), ephemeral=True)
        if interaction.user.id != deal["buyer"]:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the buyer can confirm delivery.", discord.Color.red()),
                ephemeral=True)

        # Assign buyer role
        buyer_member = interaction.guild.get_member(deal["buyer"])
        buyer_role = interaction.guild.get_role(BUYER_ROLE_ID)
        if buyer_member and buyer_role and buyer_role not in buyer_member.roles:
            await buyer_member.add_roles(buyer_role)

        # Remove deal from DB and memory — deal is complete, ticket can now be closed normally
        deals_col.delete_one({"channel_id": interaction.channel.id})
        active_deals.pop(interaction.channel.id, None)

        # Vouch embed
        vouch_embed = make_embed(
            title="🎉 Thank You For Your Purchase!",
            description=(
                f"<@{deal['buyer']}> thanks for buying **{deal['product']}** from us!\n"
                "Please vouch us — no vouch = no warranty."
            ),
            color=discord.Color.green(),
            fields=[
                ("📝 Vouch Text",
                 f"+rep <@{deal['dealer']}> Legit Got {deal['product']} For {deal['amount']}",
                 False),
                ("📢 Vouch Channel", f"<#{VOUCH_CHANNEL_ID}>", False),
            ],
            footer="We appreciate your trust!"
        )
        await interaction.response.edit_message(
            embed=make_embed("✅ Delivery Confirmed", "You've confirmed receiving the product.", discord.Color.green()),
            view=None
        )
        await interaction.channel.send(embed=vouch_embed)

        # Send transcript
        await send_html_transcript(interaction.channel, deal["buyer"])

        # Set awaiting proof
        awaiting_proof[interaction.channel.id] = deal
        proof_col.update_one(
            {"channel_id": interaction.channel.id},
            {"$set": {"channel_id": interaction.channel.id, "deal": deal}},
            upsert=True
        )
        await interaction.channel.send(embed=make_embed(
            "📸 Proof Required",
            f"<@{deal['dealer']}>, please send a screenshot/image as proof of delivery.\nIt will be automatically posted to the proof channel.",
            discord.Color.orange(),
            footer="Just send the image in this channel."
        ))

    @ui.button(label="❌ Unconfirm Delivery", style=discord.ButtonStyle.danger, custom_id="unconfirm_delivery")
    async def unconfirm(self, interaction: discord.Interaction, button: ui.Button):
        deal = deals_col.find_one({"channel_id": interaction.channel.id})
        if not deal:
            return await interaction.response.send_message(
                embed=make_embed("❌ Error", "Deal data not found.", discord.Color.red()), ephemeral=True)
        if interaction.user.id != deal["buyer"]:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the buyer can unconfirm delivery.", discord.Color.red()),
                ephemeral=True)

        # Revert confirmed flag in DB and restore to active deals
        deals_col.update_one({"channel_id": interaction.channel.id}, {"$set": {**deal, "confirmed": False}}, upsert=True)
        active_deals[interaction.channel.id] = deal

        # Remove from awaiting proof since delivery is unconfirmed
        awaiting_proof.pop(interaction.channel.id, None)
        proof_col.delete_one({"channel_id": interaction.channel.id})

        # Remove buyer role
        buyer_member = interaction.guild.get_member(deal["buyer"])
        buyer_role = interaction.guild.get_role(BUYER_ROLE_ID)
        if buyer_member and buyer_role and buyer_role in buyer_member.roles:
            await buyer_member.remove_roles(buyer_role)

        await interaction.response.send_message(
            embed=make_embed(
                "⚠️ Delivery Unconfirmed",
                "You have unconfirmed the delivery. The deal has been marked as incomplete. Please contact the dealer if there's an issue.",
                discord.Color.orange()
            ),
            ephemeral=True
        )

# ================== DEAL CREATION CONFIRM VIEW ==================
class DealConfirmView(ui.View):
    """Persistent — buyer confirms deal. All state fetched from DB on each click."""

    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="✅ Confirm Deal", style=discord.ButtonStyle.success, custom_id="persistent_confirm_deal")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        proposal = proposals_col.find_one({"channel_id": interaction.channel.id})
        if not proposal:
            return await interaction.response.send_message(
                embed=make_embed("❌ Error", "Proposal data not found.", discord.Color.red()), ephemeral=True)
        if interaction.user.id != proposal["buyer_id"]:
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the buyer can confirm the deal.", discord.Color.red()),
                ephemeral=True)

        deal_doc = {
            "channel_id": interaction.channel.id,
            "buyer": proposal["buyer_id"],
            "dealer": proposal["dealer_id"],
            "product": proposal["product"],
            "amount": proposal["amount"],
            "confirmed": False
        }
        deals_col.update_one({"channel_id": interaction.channel.id}, {"$set": deal_doc}, upsert=True)
        active_deals[interaction.channel.id] = deal_doc
        proposals_col.delete_one({"channel_id": interaction.channel.id})

        deal_embed = make_embed(
            title="🤝 Deal Confirmed",
            description="The buyer has confirmed the deal details. Please send payment.",
            color=discord.Color.green(),
            fields=[
                ("👤 Buyer", f"<@{proposal['buyer_id']}>", True),
                ("🧑‍💼 Dealer", f"<@{proposal['dealer_id']}>", True),
                ("📦 Product", proposal["product"], False),
                ("💰 Amount", proposal["amount"], False),
            ],
            footer="Dealer will deliver after payment is received."
        )
        await interaction.response.edit_message(
            embed=make_embed("✅ Deal Accepted", "You've confirmed the deal. Please select a payment method below.", discord.Color.green()),
            view=None
        )
        await interaction.channel.send(embed=deal_embed)

        # Show payment options immediately
        dealer_upi = get_dealer_upi(proposal["dealer_id"])
        dealer_ltc = get_dealer_ltc(proposal["dealer_id"])

        if dealer_upi:
            payment_embed = make_embed(
                title="💰 Select Payment Method",
                description="Please choose how you'd like to pay:",
                color=discord.Color.blurple(),
                fields=[
                    ("📦 Product", proposal["product"], True),
                    ("💰 Amount", proposal["amount"], True),
                ]
            )
            await interaction.channel.send(
                content=f"<@{proposal['buyer_id']}>",
                embed=payment_embed,
                view=PaymentChoiceView()
            )
        else:
            ltc_embed = make_embed(
                title="🪙 LTC Payment Details",
                description="Please send payment to the LTC address below.",
                color=discord.Color.gold(),
                fields=[
                    ("📦 Product", proposal["product"], True),
                    ("💰 Amount", proposal["amount"], True),
                    ("🪙 LTC Address", f"{dealer_ltc}", False),
                ],
                footer="Send exact amount and DM the dealer once payment is sent."
            )
            await interaction.channel.send(content=f"<@{proposal['buyer_id']}>", embed=ltc_embed)

    @ui.button(label="❌ Cancel Deal", style=discord.ButtonStyle.danger, custom_id="persistent_cancel_deal")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        proposal = proposals_col.find_one({"channel_id": interaction.channel.id})
        if not proposal:
            return await interaction.response.send_message(
                embed=make_embed("❌ Error", "Proposal data not found.", discord.Color.red()), ephemeral=True)
        if interaction.user.id not in (proposal["buyer_id"], proposal["dealer_id"]):
            return await interaction.response.send_message(
                embed=make_embed("❌ Not Allowed", "Only the buyer or dealer can cancel.", discord.Color.red()),
                ephemeral=True)
        proposals_col.delete_one({"channel_id": interaction.channel.id})
        await interaction.response.edit_message(
            embed=make_embed("↩️ Deal Cancelled", "The deal was cancelled.", discord.Color.red()),
            view=None
        )

# ================== CLOSE CONFIRM VIEW ==================
class CloseConfirmView(ui.View):
    def __init__(self, invoker_id: int = 0):
        super().__init__(timeout=None)
        self.invoker_id = invoker_id

    @ui.button(label="✅ Yes, Close Ticket", style=discord.ButtonStyle.danger, custom_id="persistent_close_confirm")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.invoker_id:
            return await interaction.response.send_message("Only the command invoker can confirm.", ephemeral=True)

        dealer_role = interaction.guild.get_role(DEALER_ROLE_ID)
        tm_role = interaction.guild.get_role(TICKET_MANAGER_ROLE_ID)
        ticket = tickets_col.find_one({"channel_id": interaction.channel.id})
        owner = interaction.guild.get_member(ticket["owner_id"]) if ticket else None

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            dealer_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if tm_role:
            overwrites[tm_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        if owner:
            overwrites[owner] = discord.PermissionOverwrite(view_channel=False)

        await interaction.channel.edit(overwrites=overwrites)
        await interaction.response.edit_message(
            embed=make_embed("🔒 Ticket Closed", "This ticket has been closed. The buyer no longer has access.", discord.Color.red()),
            view=None
        )
        self.stop()

    @ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary, custom_id="persistent_close_cancel")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.invoker_id:
            return await interaction.response.send_message("Only the command invoker can cancel.", ephemeral=True)
        await interaction.response.edit_message(
            embed=make_embed("↩️ Cancelled", "Ticket close was cancelled.", discord.Color.blurple()),
            view=None
        )
        self.stop()

# ================== PERSISTENT PANEL ==================
class PersistentTicketView(ui.View):
    def __init__(self, timeout=None):
        super().__init__(timeout=timeout)

    @ui.button(label="🎫 Open Ticket", style=discord.ButtonStyle.primary, custom_id="persistent_open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: ui.Button):
        dealer_role = interaction.guild.get_role(DEALER_ROLE_ID)
        tm_role = interaction.guild.get_role(TICKET_MANAGER_ROLE_ID)
        user_id = interaction.user.id

        # Duplicate ticket guard
        existing = tickets_col.find_one({"owner_id": user_id, "guild_id": interaction.guild.id})
        if existing:
            existing_ch = interaction.guild.get_channel(existing["channel_id"])
            if existing_ch:
                return await interaction.response.send_message(
                    embed=make_embed(
                        "❌ Ticket Already Open",
                        f"You already have an open ticket: {existing_ch.mention}\nPlease use that one or ask staff to close it first.",
                        discord.Color.red()
                    ),
                    ephemeral=True
                )
            else:
                # Channel was deleted without DB cleanup — remove stale record
                tickets_col.delete_one({"owner_id": user_id, "guild_id": interaction.guild.id})

        channel_name = f"ticket-{interaction.user.name}-{int(time.time())}"

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            dealer_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if tm_role:
            overwrites[tm_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
        channel = await interaction.guild.create_text_channel(
            name=channel_name, overwrites=overwrites, category=category
        )

        tickets_col.update_one({"channel_id": channel.id}, {"$set": {"owner_id": user_id, "guild_id": interaction.guild.id}}, upsert=True)
        panels_col.update_one({"channel_id": channel.id}, {"$set": {"user_id": user_id}}, upsert=True)

        welcome_embed = make_embed(
            title="🎫 Ticket Opened",
            description=(
                f"Welcome {interaction.user.mention}!\n"
                "A dealer will assist you shortly.\n"
                "Please describe what you're looking for."
            ),
            color=discord.Color.blurple(),
            fields=[("📋 Support Team", f"{dealer_role.mention} will be with you soon.", False)],
            footer="Use .close to close this ticket when you're done."
        )
        await channel.send(content=f"{interaction.user.mention} {dealer_role.mention}", embed=welcome_embed)
        await interaction.response.send_message(
            embed=make_embed("✅ Ticket Created", f"Your ticket: {channel.mention}", discord.Color.green()),
            ephemeral=True
        )

# ================== PANEL CMD ==================
@bot.command()
async def panel(ctx):
    embed = make_embed(
        title="🛒 Purchase/Help",
        description=(
            "Only Create This Ticket For Buying Or Inquiry\n"
"Check [**Prices**](<https://discord.com/channels/1476326508938137776/1476326509433323591>)\n"
"Check [**Vouches**](<https://discord.com/channels/1476326508938137776/1476326509433323595>)"
        ),
        color=discord.Color.blurple(),
        footer="Only you and our team can see your ticket."
    )
    msg = await ctx.send(embed=embed, view=PersistentTicketView())
    panels_col.update_one(
        {"message_id": msg.id},
        {"$set": {"channel_id": ctx.channel.id, "message_id": msg.id}},
        upsert=True
    )

# ================== ON READY ==================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

    for deal in deals_col.find():
        active_deals[deal["channel_id"]] = deal

    # Restore awaiting_proof state from DB after restart
    for entry in proof_col.find():
        awaiting_proof[entry["channel_id"]] = entry["deal"]

    # Register ALL persistent views so buttons work after restart
    bot.add_view(PersistentTicketView())
    bot.add_view(DealConfirmView())
    bot.add_view(PostDealConfirmView())
    bot.add_view(PaymentChoiceView())
    bot.add_view(CloseConfirmView())

    for panel in panels_col.find({"message_id": {"$exists": True}}):
        try:
            ch = bot.get_channel(panel["channel_id"])
            if not ch:
                continue
            msg = await ch.fetch_message(panel["message_id"])
            await msg.edit(view=PersistentTicketView())
        except Exception:
            continue

    refresh_panels.start()

# ================== ON MESSAGE (proof forwarding + command processing) ==================
@bot.event
async def on_message(message):
    # Always ignore bots
    if message.author.bot:
        return

    # Proof auto-forward: check if this ticket is awaiting proof from dealer
    deal = awaiting_proof.get(message.channel.id)
    if deal and message.author.id == deal["dealer"] and message.attachments:
        images = [
            a for a in message.attachments
            if a.content_type and a.content_type.startswith("image/")
        ]
        if images:
            proof_ch = bot.get_channel(PROOF_CHANNEL_ID)
            if proof_ch:
                # First image with full deal info
                embed = make_embed(
                    title=f"# Sold {deal['product']}",
                    description=(
                        f"**Dealer:** <@{deal['dealer']}>\n"
                        f"**Buyer:** <@{deal['buyer']}>\n"
                        f"**Amount:** {deal['amount']}"
                    ),
                    color=discord.Color.green(),
                    image=images[0].url,
                    footer="Proof of delivery"
                )
                await proof_ch.send(embed=embed)

                # Any extra images sent as follow-ups
                for att in images[1:]:
                    await proof_ch.send(embed=make_embed(
                        title=f"📸 Additional Proof — {deal['product']}",
                        color=discord.Color.green(),
                        image=att.url
                    ))

            await message.channel.send(embed=make_embed(
                "✅ Proof Forwarded",
                f"Your proof has been posted to <#{PROOF_CHANNEL_ID}>.",
                discord.Color.green()
            ))

            # Clear from memory and DB
            del awaiting_proof[message.channel.id]
            proof_col.delete_one({"channel_id": message.channel.id})

    # MUST call this so all commands still work
    await bot.process_commands(message)

@tasks.loop(minutes=3)
async def refresh_panels():
    for panel in panels_col.find({"message_id": {"$exists": True}}):
        try:
            ch = bot.get_channel(panel["channel_id"])
            if not ch:
                continue
            msg = await ch.fetch_message(panel["message_id"])
            await msg.edit(view=PersistentTicketView())
        except Exception:
            continue

# ================== HTML TRANSCRIPT ==================
async def create_html_transcript(channel):
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
    <title>Transcript — #{channel.name}</title>
    <style>
    *{{box-sizing:border-box;margin:0;padding:0;}}
    body{{font-family:"Segoe UI",sans-serif;background:#36393F;color:#dcddde;padding:30px;}}
    h2{{color:#fff;border-bottom:2px solid #5865f2;padding-bottom:12px;margin-bottom:20px;font-size:1.4em;}}
    .message{{display:flex;gap:14px;margin-bottom:18px;align-items:flex-start;}}
    .avatar{{width:42px;height:42px;border-radius:50%;background:#5865f2;display:flex;align-items:center;
             justify-content:center;font-weight:bold;font-size:1.1em;flex-shrink:0;color:#fff;}}
    .body{{flex:1;}}
    .meta{{display:flex;align-items:baseline;gap:8px;margin-bottom:4px;}}
    .username{{font-weight:bold;color:#fff;font-size:0.95em;}}
    .timestamp{{color:#72767d;font-size:0.78em;}}
    .content{{color:#dcddde;line-height:1.6;font-size:0.92em;}}
    .attachment{{background:#2f3136;border-radius:4px;padding:5px 10px;display:inline-block;
                 margin-top:6px;font-size:0.82em;border-left:3px solid #5865f2;}}
    a{{color:#00b0f4;text-decoration:none;}}
    </style></head><body>
    <h2>📄 Transcript — #{channel.name}</h2>"""

    async for msg in channel.history(oldest_first=True, limit=None):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        content = (msg.content
                   .replace("&", "&amp;")
                   .replace("<", "&lt;")
                   .replace(">", "&gt;")
                   .replace("\n", "<br>"))
        initial = str(msg.author.display_name)[0].upper()
        attachments = "".join(
            f'<div class="attachment"><a href="{a.url}">📎 {a.filename}</a></div>'
            for a in msg.attachments
        )
        html += f"""
        <div class="message">
            <div class="avatar">{initial}</div>
            <div class="body">
                <div class="meta">
                    <span class="username">{msg.author.display_name}</span>
                    <span class="timestamp">{ts}</span>
                </div>
                <div class="content">{content}</div>
                {attachments}
            </div>
        </div>"""

    html += "</body></html>"
    return html.encode("utf-8")


async def send_html_transcript(channel, buyer_id):
    html_bytes = await create_html_transcript(channel)
    filename = f"{channel.name}_transcript.html"

    # DM buyer — fresh BytesIO
    try:
        buyer = await bot.fetch_user(buyer_id)
        await buyer.send(
            embed=make_embed("📄 Ticket Transcript", f"Here's your transcript for `#{channel.name}`.", discord.Color.blurple()),
            file=discord.File(io.BytesIO(html_bytes), filename=filename)
        )
    except Exception:
        pass

    # Post in transcript channel — fresh BytesIO (fixes exhausted buffer bug)
    transcript_ch = bot.get_channel(TRANSCRIPT_CHANNEL_ID)
    if transcript_ch:
        await transcript_ch.send(
            embed=make_embed("📄 Transcript Saved", f"Transcript for ticket `#{channel.name}`.", discord.Color.blurple()),
            file=discord.File(io.BytesIO(html_bytes), filename=filename)
        )

# ================== DEAL CMD ==================
@bot.command()
async def deal(ctx, *, args=""):
    if not is_dealer(ctx.author):
        return await ctx.send(embed=make_embed(
            "❌ Access Denied", "Only dealers can use `.deal`.", discord.Color.red()))

    # ---- .deal done ----
    if args.strip().lower() == "done":
        deal_doc = active_deals.get(ctx.channel.id) or deals_col.find_one({"channel_id": ctx.channel.id})
        if not deal_doc:
            return await ctx.send(embed=make_embed(
                "❌ No Active Deal",
                "There is no active deal in this channel.",
                discord.Color.red()
            ))

        dealer_ltc = get_dealer_ltc(deal_doc["dealer"])
        dealer_upi = get_dealer_upi(deal_doc["dealer"])

        confirm_embed = make_embed(
            title="📦 Mark Deal as Done?",
            description=(
                f"Are you sure you want to mark this deal as complete?\n"
                f"<@{deal_doc['buyer']}> will be asked to confirm delivery."
            ),
            color=discord.Color.orange(),
            fields=[
                ("📦 Product", deal_doc["product"], True),
                ("💰 Amount", deal_doc["amount"], True),
            ]
        )

        class DealDoneConfirmView(ui.View):
            def __init__(self_inner):
                super().__init__(timeout=30)

            @ui.button(label="✅ Yes, Mark Done", style=discord.ButtonStyle.success)
            async def yes(self_inner, interaction: discord.Interaction, button: ui.Button):
                if interaction.user.id != ctx.author.id:
                    return await interaction.response.send_message("Only the dealer can confirm.", ephemeral=True)

                delivery_embed = make_embed(
                    title="📦 Delivery Confirmation Required",
                    description=f"<@{deal_doc['buyer']}>, please confirm you received your item.",
                    color=discord.Color.blurple(),
                    fields=[
                        ("📦 Product", deal_doc["product"], True),
                        ("💰 Amount", deal_doc["amount"], True),
                    ],
                    footer="Click 'Confirm Delivery' once you've received your product."
                )
                await interaction.response.edit_message(
                    embed=make_embed("✅ Sent", "Delivery confirmation sent to the buyer.", discord.Color.green()),
                    view=None
                )
                await ctx.send(
                    content=f"<@{deal_doc['buyer']}>",
                    embed=delivery_embed,
                    view=PostDealConfirmView()
                )
                self_inner.stop()

            @ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
            async def no(self_inner, interaction: discord.Interaction, button: ui.Button):
                if interaction.user.id != ctx.author.id:
                    return await interaction.response.send_message("Only the dealer can cancel.", ephemeral=True)
                await interaction.response.edit_message(
                    embed=make_embed("↩️ Cancelled", "Deal done was cancelled.", discord.Color.blurple()),
                    view=None
                )
                self_inner.stop()

        await ctx.send(embed=confirm_embed, view=DealDoneConfirmView())
        return

    # ---- Create deal ----
    matches = re.findall(r"\[(.*?)\]", args)
    if len(matches) < 2:
        return await ctx.send(embed=make_embed(
            "❌ Invalid Usage",
            "**Usage:** `.deal [Product] [Amount]`\n**Example:** `.deal [Netflix 1M] [0.005 LTC]`",
            discord.Color.red()
        ))

    product, amount = matches[0], matches[1]

    ticket = tickets_col.find_one({"channel_id": ctx.channel.id})
    if not ticket:
        return await ctx.send(embed=make_embed(
            "❌ No Ticket Found",
            "Could not detect ticket owner. Ensure this is a ticket channel.",
            discord.Color.red()
        ))

    buyer = ctx.guild.get_member(ticket["owner_id"])
    if not buyer:
        return await ctx.send(embed=make_embed(
            "❌ Buyer Not Found",
            "The ticket owner could not be found in this server.",
            discord.Color.red()
        ))

    # Save proposal to DB so DealConfirmView can fetch it after a restart
    existing_proposal = proposals_col.find_one({"channel_id": ctx.channel.id})
    if existing_proposal:
        return await ctx.send(embed=make_embed(
            "⚠️ Proposal Already Pending",
            f"There is already a pending deal proposal in this channel for **{existing_proposal['product']}** at **{existing_proposal['amount']}**.\n"
            "The buyer must confirm or cancel it before you can create a new one.",
            discord.Color.orange()
        ))

    proposals_col.update_one(
        {"channel_id": ctx.channel.id},
        {"$set": {
            "channel_id": ctx.channel.id,
            "buyer_id": buyer.id,
            "dealer_id": ctx.author.id,
            "product": product,
            "amount": amount,
        }},
        upsert=True
    )

    preview_embed = make_embed(
        title="🤝 Deal Proposal",
        description=f"<@{buyer.id}>, please review the deal details below and confirm if everything is correct.",
        color=discord.Color.orange(),
        fields=[
            ("👤 Buyer", f"<@{buyer.id}>", True),
            ("🧑‍💼 Dealer", f"<@{ctx.author.id}>", True),
            ("📦 Product", product, False),
            ("💰 Amount", amount, False),
        ],
        footer="Only the buyer can confirm or cancel this deal."
    )
    await ctx.send(
        content=f"<@{buyer.id}>",
        embed=preview_embed,
        view=DealConfirmView()
    )

# ================== TICKET MANAGEMENT COMMANDS ==================
@bot.command()
async def close(ctx):
    if not ctx.channel.name.startswith("ticket-"):
        return await ctx.send(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()))
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))

    deal_doc = deals_col.find_one({"channel_id": ctx.channel.id})
    if deal_doc and not is_head_dealer(ctx.author):
        return await ctx.send(embed=make_embed(
            "⚠️ Active Deal",
            "Cannot close — there is an active deal in this ticket.\nOnly a Head Dealer can force close.",
            discord.Color.orange()
        ))

    await ctx.send(
        embed=make_embed("🔒 Close Ticket?", "Are you sure you want to close this ticket? The buyer will lose access.", discord.Color.orange()),
        view=CloseConfirmView(ctx.author.id)
    )


@bot.command()
async def reopen(ctx):
    if not ctx.channel.name.startswith("ticket-"):
        return
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))

    ticket = tickets_col.find_one({"channel_id": ctx.channel.id})
    owner = ctx.guild.get_member(ticket["owner_id"]) if ticket else None
    dealer_role = ctx.guild.get_role(DEALER_ROLE_ID)
    tm_role = ctx.guild.get_role(TICKET_MANAGER_ROLE_ID)

    overwrites = {
        ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        dealer_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if tm_role:
        overwrites[tm_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    if owner:
        overwrites[owner] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    await ctx.channel.edit(overwrites=overwrites)
    await ctx.send(embed=make_embed("🔓 Ticket Reopened", "Buyer access has been restored.", discord.Color.green()))


@bot.command()
async def rename(ctx, *, new_name):
    if not ctx.channel.name.startswith("ticket-"):
        return
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))

    clean = new_name.strip().replace("[", "").replace("]", "").replace(" ", "-").lower()
    new_channel_name = f"ticket-{clean}"
    await ctx.channel.edit(name=new_channel_name)
    await ctx.send(embed=make_embed("✏️ Ticket Renamed", f"Channel renamed to `{new_channel_name}`.", discord.Color.blurple()))


@bot.command()
async def add(ctx, member: discord.Member):
    if not ctx.channel.name.startswith("ticket-"):
        return
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))

    await ctx.channel.set_permissions(member, view_channel=True, send_messages=True)
    await ctx.send(embed=make_embed("➕ Member Added", f"{member.mention} has been added to this ticket.", discord.Color.green()))


@bot.command()
async def remove(ctx, member: discord.Member):
    if not ctx.channel.name.startswith("ticket-"):
        return
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))

    await ctx.channel.set_permissions(member, view_channel=False, send_messages=False)
    await ctx.send(embed=make_embed("➖ Member Removed", f"{member.mention} has been removed from this ticket.", discord.Color.orange()))


@bot.command()
async def delete(ctx):
    if not ctx.channel.name.startswith("ticket-"):
        return
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))

    deal_doc = deals_col.find_one({"channel_id": ctx.channel.id})
    if deal_doc and not is_head_dealer(ctx.author):
        return await ctx.send(embed=make_embed(
            "⚠️ Active Deal",
            "Cannot delete ticket — there is an active deal.\nOnly a Head Dealer can force delete.",
            discord.Color.orange()
        ))

    await ctx.send(embed=make_embed("🗑️ Deleting Ticket", "This ticket will be **permanently deleted** in 3 seconds...", discord.Color.red()))
    await asyncio.sleep(3)

    tickets_col.delete_one({"channel_id": ctx.channel.id})
    deals_col.delete_one({"channel_id": ctx.channel.id})
    panels_col.delete_one({"channel_id": ctx.channel.id})
    active_deals.pop(ctx.channel.id, None)

    await ctx.channel.delete()

# ================== DISPUTE COMMAND ==================
@bot.command()
async def dispute(ctx, *, reason: str = "No reason provided."):
    if not ctx.channel.name.startswith("ticket-"):
        return await ctx.send(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()))

    ticket = tickets_col.find_one({"channel_id": ctx.channel.id})
    deal_doc = deals_col.find_one({"channel_id": ctx.channel.id})

    is_buyer = ticket and ctx.author.id == ticket.get("owner_id")
    if not is_buyer and not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "Only the buyer or a dealer can raise a dispute.", discord.Color.red()))

    head_dealer_role = ctx.guild.get_role(HEAD_DEALER_ROLE_ID)

    dispute_embed = make_embed(
        title="⚠️ Dispute Raised",
        description=f"A dispute has been raised in this ticket by {ctx.author.mention}.",
        color=discord.Color.red(),
        fields=[
            ("👤 Raised By", ctx.author.mention, True),
            ("📦 Product", deal_doc["product"] if deal_doc else "N/A", True),
            ("💰 Amount", deal_doc["amount"] if deal_doc else "N/A", True),
            ("📝 Reason", reason, False),
            ("📌 Channel", ctx.channel.mention, False),
        ],
        footer="A Head Dealer will review this shortly."
    )

    await ctx.send(
        content=head_dealer_role.mention if head_dealer_role else "",
        embed=dispute_embed
    )

    log_ch = bot.get_channel(TRANSCRIPT_CHANNEL_ID)
    if log_ch:
        await log_ch.send(embed=dispute_embed)


# ================== TRANSFER COMMAND ==================
@bot.command()
async def transfer(ctx, new_dealer: discord.Member):
    if not ctx.channel.name.startswith("ticket-"):
        return await ctx.send(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()))
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))
    if not can_manage_ticket(new_dealer):
        return await ctx.send(embed=make_embed("❌ Invalid Target", f"{new_dealer.mention} is not a dealer or ticket manager.", discord.Color.red()))
    if new_dealer.id == ctx.author.id:
        return await ctx.send(embed=make_embed("❌ Invalid", "You can't transfer a ticket to yourself.", discord.Color.red()))

    await ctx.channel.set_permissions(new_dealer, view_channel=True, send_messages=True)

    await ctx.send(embed=make_embed(
        "🔁 Ticket Transferred",
        f"This ticket has been transferred from {ctx.author.mention} to {new_dealer.mention}.",
        discord.Color.blurple(),
        fields=[("📌 New Handler", new_dealer.mention, False)],
        footer="The new dealer has been notified."
    ))

    try:
        await new_dealer.send(embed=make_embed(
            "📨 Ticket Transferred To You",
            f"{ctx.author.mention} has transferred a ticket to you.",
            discord.Color.blurple(),
            fields=[
                ("📌 Channel", ctx.channel.mention, False),
                ("🏠 Server", ctx.guild.name, False),
            ],
            footer="Head over to the ticket channel to assist."
        ))
    except discord.Forbidden:
        pass


# ================== REMIND COMMAND ==================
@bot.command()
async def remind(ctx, minutes: int, *, message: str = "This is your reminder!"):
    if not ctx.channel.name.startswith("ticket-"):
        return await ctx.send(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()))
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))
    if minutes < 1 or minutes > 1440:
        return await ctx.send(embed=make_embed("❌ Invalid Time", "Please provide a time between 1 and 1440 minutes (24 hours).", discord.Color.red()))

    await ctx.send(embed=make_embed(
        "⏱️ Reminder Set",
        f"I'll remind you in **{minutes} minute(s)**.",
        discord.Color.blurple(),
        fields=[("📝 Message", message, False)],
        footer=f"Set by {ctx.author.display_name}"
    ))

    channel_id = ctx.channel.id
    author_id = ctx.author.id
    author_name = ctx.author.display_name

    async def send_reminder():
        await asyncio.sleep(minutes * 60)
        ch = bot.get_channel(channel_id)
        if ch:
            await ch.send(
                content=f"<@{author_id}>",
                embed=make_embed(
                    "⏰ Reminder",
                    message,
                    discord.Color.gold(),
                    footer=f"Reminder set {minutes} minute(s) ago by {author_name}"
                )
            )

    asyncio.create_task(send_reminder())


# ================== CALL COMMAND ==================
@bot.command()
async def call(ctx, member: discord.Member = None):
    if not ctx.channel.name.startswith("ticket-"):
        return await ctx.send(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()))
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))

    if member is None:
        ticket = tickets_col.find_one({"channel_id": ctx.channel.id})
        if not ticket:
            return await ctx.send(embed=make_embed("❌ No Ticket Data", "Could not find the ticket owner.", discord.Color.red()))
        member = ctx.guild.get_member(ticket["owner_id"])
        if not member:
            return await ctx.send(embed=make_embed("❌ User Not Found", "The ticket owner is no longer in the server.", discord.Color.red()))

    try:
        await member.send(embed=make_embed(
            "📣 You're Being Called!",
            f"**{ctx.author.display_name}** is calling you in your ticket.",
            discord.Color.orange(),
            fields=[
                ("📌 Channel", ctx.channel.mention, False),
                ("🏠 Server", ctx.guild.name, False),
            ],
            footer="Please head back to your ticket channel."
        ))
        await ctx.send(embed=make_embed(
            "📣 Called!",
            f"{member.mention} has been DM'd to return to this ticket.",
            discord.Color.green()
        ))
    except discord.Forbidden:
        await ctx.send(embed=make_embed(
            "⚠️ DM Failed",
            f"Could not DM {member.mention} — their DMs are closed. Pinging them here instead.",
            discord.Color.orange()
        ))
        await ctx.send(content=f"Hey {member.mention}, you're needed in this ticket!")


# ================== TRANSCRIPT COMMAND ==================
@bot.command()
async def transcript(ctx):
    if not ctx.channel.name.startswith("ticket-"):
        return await ctx.send(embed=make_embed("❌ Not a Ticket", "This command only works in ticket channels.", discord.Color.red()))
    if not can_manage_ticket(ctx.author):
        return await ctx.send(embed=make_embed("❌ Access Denied", "You need Dealer or Ticket Manager role.", discord.Color.red()))

    await ctx.send(embed=make_embed("📄 Generating Transcript...", "Please wait a moment.", discord.Color.blurple()))

    ticket = tickets_col.find_one({"channel_id": ctx.channel.id})
    buyer_id = ticket["owner_id"] if ticket else None

    await send_html_transcript(ctx.channel, buyer_id)

    await ctx.send(embed=make_embed(
        "✅ Transcript Sent",
        "The transcript has been posted to the log channel" + (f" and DMed to <@{buyer_id}>." if buyer_id else "."),
        discord.Color.green()
    ))


# ================== HELP COMMAND ==================
@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(
        title="🤖 Bot Command Reference",
        description="Here's everything this bot can do:",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="💰 Payment Setup (Dealer only)",
        value=(
            "`.ltc <address>` — Set your LTC wallet address\n"
            "`.upi <id> <image_url>` — Set your UPI ID and QR code image"
        ),
        inline=False
    )

    embed.add_field(
        name="🤝 Deal Management (Dealer only)",
        value=(
            "`.deal [Product] [Amount]` — Create a deal proposal for the buyer to confirm\n"
            "`.deal done` — Mark delivery as complete, ask buyer to confirm receipt"
        ),
        inline=False
    )

    embed.add_field(
        name="🎫 Ticket Management (Dealer & Ticket Manager)",
        value=(
            "`.close` — Close the ticket (hides it from buyer)\n"
            "`.reopen` — Reopen a closed ticket\n"
            "`.delete` — Permanently delete the ticket channel\n"
            "`.rename <name>` — Rename the ticket channel\n"
            "`.add @user` — Add a member to the ticket\n"
            "`.remove @user` — Remove a member from the ticket\n"
            "`.transfer @dealer` — Transfer ticket to another dealer\n"
            "`.transcript` — Manually send transcript to log + buyer DM"
        ),
        inline=False
    )

    embed.add_field(
        name="🛡️ Dispute & Communication",
        value=(
            "`.dispute <reason>` — Raise a dispute, pings Head Dealer\n"
            "`.call [@user]` — DM ping the buyer (or a specific user) to return\n"
            "`.remind <minutes> <message>` — Set a reminder ping in this ticket"
        ),
        inline=False
    )

    embed.add_field(
        name="🛒 Panel (Admin)",
        value="`.panel` — Post the ticket open panel in the current channel",
        inline=False
    )

    embed.add_field(
        name="📋 How a Deal Works",
        value=(
            "1️⃣ Dealer runs `.deal [Product] [Amount]`\n"
            "2️⃣ **Buyer** clicks ✅ Confirm Deal → payment options shown\n"
            "3️⃣ Buyer sends payment\n"
            "4️⃣ Dealer delivers, runs `.deal done`\n"
            "5️⃣ **Buyer** clicks ✅ Confirm Delivery → vouch embed sent\n"
            "6️⃣ **Dealer** sends proof image → auto-posted to proof channel"
        ),
        inline=False
    )

    embed.set_footer(text="Prefix: .  |  Only the buyer can confirm deals & delivery.")
    await ctx.send(embed=embed)

# ================== FLASK (keep-alive) ==================
app = Flask("")

@app.route("/")
def home():
    return "✅ Bot is running 24/7"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

threading.Thread(target=run_flask, daemon=True).start()
bot.run(TOKEN)
