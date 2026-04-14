#!/usr/bin/env python3
"""Minimal starter bot for Bybit.

This is a lightweight example showing how to load API credentials
from a .env file and fetch account balance using ccxt (async).
"""
import os
import asyncio
from dotenv import load_dotenv
import ccxt.async_support as ccxt

load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")

async def main():
    if not API_KEY or not API_SECRET:
        print("Warning: BYBIT_API_KEY or BYBIT_API_SECRET not set in .env or secrets. Exiting.")
        return

    exchange = ccxt.bybit({
        'apiKey': API_KEY,
        'secret': API_SECRET,
    })
    try:
        balance = await exchange.fetch_balance()
        print("Balance:")
        print(balance)
    except Exception as e:
        print("Error fetching balance:", e)
    finally:
        await exchange.close()

if __name__ == '__main__':
    asyncio.run(main())