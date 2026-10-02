import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Directory settings
WORK_DIR = os.getenv("WORK_DIR", "workspace")
os.makedirs(WORK_DIR, exist_ok=True)
