from motor.motor_asyncio import AsyncIOMotorClient
import os

client: AsyncIOMotorClient = None
db = None

# Read at import time — load_dotenv() in main.py runs before this module is imported
COLLECTION: str = os.getenv("COLLECTION_NAME", "questions")

async def connect_db():
    global client, db
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    db_name   = os.getenv("DB_NAME",   "proshno-potro")
    client = AsyncIOMotorClient(mongo_uri)
    db = client[db_name]
    print(f"✅ Connected to MongoDB: {db_name}.{COLLECTION}")

async def close_db():
    global client
    if client:
        client.close()
        print("🔌 MongoDB connection closed")

def get_db():
    return db