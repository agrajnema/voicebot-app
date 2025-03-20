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

import smtplib
from email.message import EmailMessage
from twilio.rest import Client
import re

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

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
EMAIL_HOST = os.getenv("EMAIL_HOST")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM")


twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


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

# At the top of your file, add:
from fastapi.middleware.cors import CORSMiddleware

# After creating your FastAPI app:
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Application is running!"}


@app.get("/websocket-test")
async def websocket_test():
    return {"websocket_supported": True, "message": "WebSocket endpoint is operational"}


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    response = VoiceResponse()
    response.say("Please wait while we connect your call.")
    response.pause(length=1)
    
    # Add a beep sound to indicate when the user should start speaking
    response.play("https://api.twilio.com/cowbell.mp3")  # Use Twilio's standard tone
    response.say("Please speak after the beep.")
    
    # Get the public-facing hostname
    host = request.headers.get("X-Forwarded-Host", request.url.hostname)
    
    # Set up the WebSocket connection
    connect = Connect()
    ws_url = f"wss://{host}/media-stream"
    logger.info(f"Setting up Twilio stream connection to: {ws_url}")
    connect.stream(url=ws_url)
    response.append(connect)
    
    return HTMLResponse(content=str(response), media_type="application/xml")

# Add this to your global variables
conversation_states = {}

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    logger.info("WebSocket connection attempt from Twilio.")
    try:
        await websocket.accept()
        logger.info("WebSocket connection successfully accepted.")

        stream_sid = None
        call_active = True  # Flag to track if call should remain active
        
        # Initialize conversation state
        conversation_state = {
            "last_response": "",
            "waiting_for": None,
            "contact_info": None,
            "delivery_method": None
        }

        # Create proper headers for the websocket connection
        headers = {"api-key": AZURE_OPENAI_API_KEY}
        
        # Connect to OpenAI API with proper headers
        async with websockets.connect(
            AZURE_OPENAI_API_ENDPOINT,
            extra_headers=headers
        ) as openai_ws:
            await initialize_session(openai_ws)

            async def receive_from_twilio():
                nonlocal stream_sid, call_active
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        logger.info(f"Received Twilio event: {data['event']}")
                        
                        if data["event"] == "media":
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"],
                            }
                            await openai_ws.send(json.dumps(audio_append))
                        elif data["event"] == "start":
                            stream_sid = data["start"]["streamSid"]
                            logger.info(f"Stream started with SID: {stream_sid}")
                            if stream_sid:
                                conversation_states[stream_sid] = conversation_state
                except WebSocketDisconnect:
                    logger.warning("WebSocket disconnected by client.")
                except Exception as e:
                    logger.error(f"Error in receive_from_twilio: {str(e)}")
                finally:
                    if stream_sid and stream_sid in conversation_states:
                        del conversation_states[stream_sid]

            async def send_to_twilio():
                nonlocal call_active
                try:
                    async for openai_message in openai_ws:
                        response = json.loads(openai_message)
                        
                        # Handle input from user
                        if response.get("type") == "input_audio_buffer.committed":
                            user_input = response.get("text", "").strip().lower()
                            logger.info(f"User input committed: {user_input}")
                            
                            # Check for call end keywords
                            if "goodbye" in user_input or "bye" in user_input or "end call" in user_input or "hang up" in user_input:
                                logger.info("End call keyword detected")
                                call_active = False
                                await inject_assistant_message(openai_ws, "Thank you for calling Alinta Energy. Goodbye!")
                                
                                # End the call after a short delay to let the goodbye message play
                                asyncio.create_task(end_call_after_delay(stream_sid, 5))
                            
                            # Process based on conversation state
                            if stream_sid and stream_sid in conversation_states:
                                state = conversation_states[stream_sid]
                                
                                # Check for SMS or Email selection
                                if state["waiting_for"] == "sms_or_email":
                                    if "sms" in user_input:
                                        state["delivery_method"] = "sms"
                                        state["waiting_for"] = "mobile"
                                    elif "email" in user_input:
                                        state["delivery_method"] = "email"
                                        state["waiting_for"] = "email"
                                
                                # Check for mobile number or email input
                                elif state["waiting_for"] == "mobile" and any(char.isdigit() for char in user_input):
                                    state["contact_info"] = user_input.replace(" ", "")
                                    success = await send_sms(state["contact_info"], state["last_response"])
                                    state["waiting_for"] = None
                                    
                                    if success:
                                        await inject_assistant_message(openai_ws, "I've sent the information to your mobile number. Is there anything else you need help with?")
                                    else:
                                        await inject_assistant_message(openai_ws, "I'm sorry, there was an issue sending the SMS. Would you like to try again with a different number?")
                                    
                                elif state["waiting_for"] == "email" and "@" in user_input:
                                    # Extract email more carefully from speech recognition
                                    email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', user_input)
                                    email = email_match.group(0) if email_match else user_input.replace(" ", "")
                                    
                                    state["contact_info"] = email
                                    success = await send_email(state["contact_info"], "Information from Alinta Energy", state["last_response"])
                                    state["waiting_for"] = None
                                    
                                    if success:
                                        await inject_assistant_message(openai_ws, "I've sent the information to your email address. Is there anything else you need help with?")
                                    else:
                                        await inject_assistant_message(openai_ws, "I'm sorry, there was an issue sending the email. Would you like to try again with a different email address?")

                        # Handle AI response for detecting SMS/Email prompt
                        if response.get("type") == "response.text.done":
                            full_response = response.get("text", "")
                            logger.info(f"AI full response: {full_response}")
                            if stream_sid and stream_sid in conversation_states:
                                conversation_states[stream_sid]["last_response"] = full_response
                                
                                # If the AI asked about SMS or Email, update state
                                if "sms or email" in full_response.lower():
                                    conversation_states[stream_sid]["waiting_for"] = "sms_or_email"
                                    logger.info("Detected SMS/Email prompt - waiting for user selection")

                        # Handle audio response
                        if response.get("type") == "response.audio.delta" and "delta" in response:
                            audio_payload = base64.b64encode(
                                base64.b64decode(response["delta"])
                            ).decode("utf-8")
                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": audio_payload},
                            }
                            # Convert to JSON string before sending
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
                    logger.error(f"Error in send_to_twilio: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

            # Run both coroutines concurrently
            await asyncio.gather(receive_from_twilio(), send_to_twilio())

    except Exception as e:
        logger.error(f"Critical WebSocket error: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())

# Add function to end call after a delay
async def end_call_after_delay(stream_sid, delay_seconds):
    """End the call after a specified delay to allow goodbye message to play"""
    try:
        logger.info(f"Will end call with SID {stream_sid} after {delay_seconds} seconds")
        await asyncio.sleep(delay_seconds)
        
        # Get the call SID associated with this stream
        # Note: You may need to store a mapping between stream_sid and call_sid
        call_sid = stream_sid.split('.')[0] if stream_sid and '.' in stream_sid else None
        
        if call_sid:
            # End the call using Twilio's API
            twilio_client.calls(call_sid).update(status="completed")
            logger.info(f"Call with SID {call_sid} has been ended")
        else:
            logger.error(f"Could not extract call SID from stream SID: {stream_sid}")
    except Exception as e:
        logger.error(f"Error ending call: {e}")

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



# Add these helper functions
async def inject_assistant_message(openai_ws, message):
    """Inject a message into the conversation as if it came from the assistant."""
    inject_message = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": message}]
        }
    }
    await openai_ws.send(json.dumps(inject_message))
    
    # Prompt OpenAI to continue processing
    await openai_ws.send(json.dumps({"type": "response.create"}))

async def send_sms(phone_number, message):
    """Send SMS using Twilio."""
    try:
        # Remove any non-digit characters and ensure it starts with +
        clean_number = ''.join(filter(str.isdigit, phone_number))
        if not clean_number.startswith('+'):
            clean_number = '+' + clean_number
            
        # Truncate message if too long for SMS
        if len(message) > 1600:
            message = message[:1597] + "..."
            
        twilio_client.messages.create(
            body=message,
            from_=TWILIO_PHONE_NUMBER,
            to=clean_number
        )
        logger.info(f"SMS sent to {clean_number}")
        return True
    except Exception as e:
        logger.error(f"Error sending SMS: {e}")
        return False

# Enhance the email sending function with better error handling
async def send_email(email_address, subject, message):
    """Send email using SMTP with improved error handling."""
    try:
        logger.info(f"Attempting to send email to {email_address}")
        
        # Basic email validation
        if not re.match(r'[\w\.-]+@[\w\.-]+\.\w+', email_address):
            logger.error(f"Invalid email format: {email_address}")
            return False
            
        msg = EmailMessage()
        msg.set_content(message)
        msg['Subject'] = subject
        msg['From'] = EMAIL_FROM
        msg['To'] = email_address
        
        # Connect with timeout
        logger.debug(f"Connecting to SMTP server: {EMAIL_HOST}:{EMAIL_PORT}")
        server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=10)
        
        # Detailed logging for troubleshooting
        server.set_debuglevel(1)
        
        # Start TLS
        server.starttls()
        logger.debug("STARTTLS initiated")
        
        # Login
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        logger.debug("SMTP login successful")
        
        # Send message
        server.send_message(msg)
        logger.debug("Message sent")
        
        # Quit properly
        server.quit()
        
        logger.info(f"Email successfully sent to {email_address}")
        return True
    except smtplib.SMTPException as e:
        logger.error(f"SMTP error sending email: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending email: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)