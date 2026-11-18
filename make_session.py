"""Запустите ОДИН раз на своём компьютере, чтобы получить TG_SESSION
(нужен только для закрытого канала). pip install telethon"""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("api_id: "))
api_hash = input("api_hash: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as client:  # спросит телефон и код
    print("\nВАШ TG_SESSION (никому не показывайте):\n")
    print(client.session.save())
