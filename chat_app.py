import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from twitchio.ext import commands
from lib.redis import get_redis
from lib.mongodb import get_db
from datetime import datetime, timedelta
from services.generative_service import generate_response
from services.custom_model_service import custom_model
from utils.logging import logger 
import random

# Load environment variables from .env file
load_dotenv()

# Initialize Redis and MongoDB connections
redis_client = get_redis()
db = get_db()
messages_collection = db["messages"]
state_collection = db["state"]

executor = ThreadPoolExecutor()

class Bot(commands.Bot):

    def __init__(self):
        super().__init__(token=os.getenv('ACCESS_TOKEN', 'default_access_token'), prefix='!', initial_channels=[os.getenv('CHANNEL', '')])
        logger.info('Bot initialized.')

    async def event_ready(self):
        logger.info(f'Logged in as | {self.nick}')
        logger.info(f'User id is | {self.user_id}')

    async def event_message(self, message):
        if message.author.name.lower() == self.nick.lower():
            return

        logger.info(f'Received message from {message.author.name}: {message.content}')
        response = await process_message(message.content)
        # await message.channel.send(response)

async def process_message(message):
    loop = asyncio.get_event_loop()

    # Fire and forget model update
    loop.run_in_executor(executor, update_model, message)
    
    api_url = os.getenv("LLM_CHAT_API_URL", "http://127.0.0.1:1800/msg")  # yinmei-chatAPI
    payload = {
        "msg": message.content,  # mes
        "uid": message.author.id,  # id
        "username": message.author.name  # name
    }
    
    try:
        # Send the POST request to the LLM chat API
        response = requests.post(api_url, json=payload)
        response.raise_for_status()  # 检查请求是否成功
        response_data = response.json()  # 解析返回的 JSON 数据
        # Extract the response from the API
        if response_data and "response" in response_data:
            response_text = response_data["response"]
            logger.info(f"Received response from LLM chat API: {response_text}")
            return response_text
        else:
            logger.error("No valid response received from LLM chat API")
            return "Error: No response from LLM chat API"
    except requests.RequestException as e:
        logger.error(f"Error sending request to LLM chat API: {e}")
        return "Error: Failed to send message to LLM chat API"

    
    # Get cached response
    cached_response = redis_client.get("latest_response")
    if cached_response:
        logger.debug(f'Using cached response for message: {message}')
        return cached_response.decode("utf-8")
    else:
        # If no cached response, generate a custom response
        logger.info(f'No cached response, generating new response for message: {message}')
        generative_future = loop.run_in_executor(executor, generate_response, message)
        generative_response = await generative_future

        # Cache the new response
        redis_client.set("latest_response", generative_response)
        logger.debug(f'Cached new response for message: {message}')
        return generative_response

def update_model(message):
    try:
        # Save message to MongoDB for auditing
        response = generate_response(message)
        messages_collection.insert_one({
            "message": message,
            "response": response,
            "timestamp": datetime.utcnow(),
            "feedback_score": random.randint(0, 100)  # Simulated feedback score
        })

        # Update the model
        custom_model.update_model([message], [response])

        # Save the state to MongoDB
        state_collection.replace_one({}, {"state": custom_model}, upsert=True)

        # Update the highest feedback response if needed
        feedback_scores = [doc["feedback_score"] for doc in messages_collection.find()]
        highest_feedback_score = max(feedback_scores, default=0)
        if feedback_scores[-1] == highest_feedback_score:
            redis_client.set("latest_response", response)
        
        logger.info('Model updated successfully.')
    except Exception as e:
        logger.error(f'Error updating model: {str(e)}')

# Set the interval for updating the model (in minutes)
UPDATE_INTERVAL = 10

async def scheduled_updates():
    while True:
        await asyncio.sleep(UPDATE_INTERVAL * 60)
        # Update the highest feedback score response
        update_highest_feedback_response()

def update_highest_feedback_response():
    try:
        ten_minutes_ago = datetime.utcnow() - timedelta(minutes=10)
        recent_messages = messages_collection.find({"timestamp": {"$gte": ten_minutes_ago}})
        best_response = max(recent_messages, key=lambda msg: msg.get("feedback_score", 0), default=None)
        if best_response:
            redis_client.set("latest_response", best_response["response"])
        
        logger.info('Highest feedback response updated.')
    except Exception as e:
        logger.error(f'Error updating highest feedback response: {str(e)}')

if __name__ == "__main__":
    bot = Bot()
    loop = asyncio.get_event_loop()
    loop.create_task(scheduled_updates())
    loop.run_until_complete(bot.start())
