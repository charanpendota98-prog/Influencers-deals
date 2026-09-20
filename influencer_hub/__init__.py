"""influencer_hub — multi-tenant influencer layer on top of the bestgaa loot bot.

One shared deal pool. Per influencer:
  * we create a Telegram channel under our bot account
  * we pair the influencer's WhatsApp number (QR) for a group feed + an
    official WhatsApp Channel
  * Amazon links carry THEIR amazon associate tag
  * every other merchant link carries OUR EarnKaro publisher id

See README.md for the full walkthrough.
"""
__version__ = "0.1.0"
