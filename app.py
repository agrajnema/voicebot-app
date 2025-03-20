import os
import json
import base64
import asyncio
import websockets
import logging
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from dotenv import load_dotenv

load_dotenv()

# Configuration
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_API_ENDPOINT = os.getenv("AZURE_OPENAI_API_ENDPOINT")
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
AZURE_SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY")
AZURE_SEARCH_INDEX = os.getenv("AZURE_SEARCH_INDEX")
AZURE_SEARCH_SEMANTIC_CONFIGURATION = os.getenv("AZURE_SEARCH_SEMANTIC_CONFIGURATION")
PORT = int(os.getenv("PORT", 5050))
VOICE = "alloy"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Azure Search Client Setup
credential = AzureKeyCredential(AZURE_SEARCH_KEY)
search_client = SearchClient(
    endpoint=AZURE_SEARCH_ENDPOINT, index_name=AZURE_SEARCH_INDEX, credential=credential
)

# FastAPI App
app = FastAPI()


@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Application is running!"}


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    response = VoiceResponse()
    response.say("Please wait while we connect your call.")
    response.pause(length=1)
    response.say("You can start talking now!")
    host = request.url.hostname
    #host = request.url.hostname
    # if request.url.port:
    #      host = f"{host}:{request.url.port}"
    connect = Connect()
    print(f"wss://{host}/media-stream")
    connect.stream(url=f"wss://{host}/media-stream")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    logger.info("WebSocket connection opened.")
    await websocket.accept()

    stream_sid = None

    try:
        # Open connection to Azure OpenAI
        async with websockets.connect(
            AZURE_OPENAI_API_ENDPOINT,
            additional_headers={"api-key": AZURE_OPENAI_API_KEY},
        ) as openai_ws:
            await initialize_session(openai_ws)

            # Handle incoming messages from Twilio
            async def receive_from_twilio():
                nonlocal stream_sid
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        if data["event"] == "media":
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"],
                            }
                            await openai_ws.send(json.dumps(audio_append))
                        elif data["event"] == "start":
                            stream_sid = data["start"]["streamSid"]
                            logger.info(f"Stream started with SID: {stream_sid}")
                        # Handle other Twilio events
                        elif data["event"] == "mark":
                            logger.info(f"Mark event received: {data}")
                        elif data["event"] == "stop":
                            logger.info(f"Stop event received, closing connection")
                            return
                except WebSocketDisconnect:
                    logger.warning("WebSocket disconnected by client.")
                except json.JSONDecodeError:
                    logger.error("Received malformed JSON from Twilio")
                except Exception as e:
                    logger.error(f"Error in receive_from_twilio: {str(e)}")
                finally:
                    if openai_ws.open:
                        await openai_ws.close()

            # Handle outgoing messages to Twilio
            async def send_to_twilio():
                try:
                    current_query = ""
                    async for openai_message in openai_ws:
                        response = json.loads(openai_message)

                        if response.get("type") == "response.audio.delta" and "delta" in response:
                            current_query = response.get("text", "").strip()
                            if current_query:
                                logger.info(f"User query: {current_query}")
                            audio_payload = base64.b64encode(
                                base64.b64decode(response["delta"])
                            ).decode("utf-8")
                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": audio_payload},
                            }
                            # Send as JSON - using the proper WebSocket method
                            await websocket.send_text(json.dumps(audio_delta))

                        # Handle function calls for RAG
                        if response.get("type") == "response.function_call_arguments.done":
                            function_name = response["name"]
                            if function_name == "get_additional_context":
                                query = json.loads(response["arguments"]).get("query", "")
                                search_results = azure_search_rag(query)
                                logger.info(f"RAG Results: {search_results}")
                                await send_function_output(openai_ws, response["call_id"], search_results)
                except Exception as e:
                    logger.error(f"Error in send_to_twilio: {str(e)}")

            # Run both tasks concurrently
            await asyncio.gather(receive_from_twilio(), send_to_twilio())
    except Exception as e:
        logger.error(f"WebSocket handler error: {str(e)}")
    finally:
        # Ensure we clean up properly
        if not websocket.client_state == WebSocket.DISCONNECTED:
            await websocket.close()
        logger.info("WebSocket connection closed.")


async def initialize_session(openai_ws):
    """Initialize the OpenAI session with instructions and tools."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": (
                "You are an AI assistant providing factual answers ONLY from the search. "
                "If USER says hello Always respond with with Hello, I am Jason from Alinta Energy. How can I help you today? "
                "Use the `get_additional_context` function to retrieve relevant information. "
                "Keep all your responses very concise and straight to point and not more than 30 words. "
                "After providing any information, always ask: 'Would you like to receive this information via SMS or Email?' "
                "If they choose SMS, ask for their mobile number. "
                "If they choose Email, ask for their email address. "
                "Once they provide these details, confirm you are sending the information and continue the conversation. "
                "If USER says Thank You, Always respond with with You are welcome, Is there anything else I can help you with?"
            ),
            "tools": [
                {
                    "type": "function",
                    "name": "get_additional_context",
                    "description": "Fetch context from Azure Search based on a user query.",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ],
        },
    }
    await openai_ws.send(json.dumps(session_update))


async def trigger_rag_search(openai_ws, query):
    """Trigger RAG search for a specific query."""
    search_function_call = {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call",
            "name": "get_additional_context",
            "arguments": {"query": query},
        },
    }
    await openai_ws.send(json.dumps(search_function_call))


async def send_function_output(openai_ws, call_id, output):
    """Send RAG results back to OpenAI."""
    response = {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        },
    }
    await openai_ws.send(json.dumps(response))

    # Prompt OpenAI to continue processing
    await openai_ws.send(json.dumps({"type": "response.create"}))


def azure_search_rag(query):
    """Perform Azure Cognitive Search and return results."""
    try:
        logger.info(f"Querying Azure Search with: {query}")
        results = search_client.search(
            search_text=query,
            top=2,
            query_type="semantic",
            semantic_configuration_name=AZURE_SEARCH_SEMANTIC_CONFIGURATION,
        )
        summarized_results = [doc.get("chunk", "No content available") for doc in results]
        if not summarized_results:
            return "No relevant information found in Azure Search."
        return "\n".join(summarized_results)
    except Exception as e:
        logger.error(f"Error in Azure Search: {e}")
        return "Error retrieving data from Azure Search."


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)