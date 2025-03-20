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
    # Create TwiML response
    response = VoiceResponse()
    
    # Get form data - this captures the call SID for later use
    form_data = await request.form()
    call_sid = form_data.get('CallSid')
    logger.info(f"Incoming call with SID: {call_sid}")
    
    # Store the call SID in a global mapping
    if call_sid:
        # Create a global mapping if it doesn't exist
        if not hasattr(app, 'call_mappings'):
            app.call_mappings = {}
        app.call_mappings[call_sid] = {'active': True}
    
    # Welcome message
    response.say("Welcome to Alinta Energy.")
    
    # Add an explicit beep sound using Twilio's built-in capability
    # The 'w' makes it pause for half a second
    response.play(digits="1w")
    
    # Get the host for the WebSocket connection
    host = request.headers.get("X-Forwarded-Host", request.url.hostname)
    if request.url.port and request.url.port != 443:
        host = f"{host}:{request.url.port}"
    
    # Set up the WebSocket connection
    connect = Connect()
    ws_url = f"wss://{host}/media-stream"
    logger.info(f"Setting up Twilio stream connection to: {ws_url}")
    connect.stream(url=ws_url)
    response.append(connect)
    
    # Add instruction after the beep
    response.say("You can start talking now.")
    
    logger.info(f"Generated TwiML: {str(response)}")
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
        call_sid = None
        
        # Initialize conversation state
        conversation_state = {
            "last_response": "",
            "waiting_for": None,
            "contact_info": None,
            "delivery_method": None,
            "email_chars": [],  # For storing email character by character
            "mobile_digits": [],  # For storing mobile digits
            "confirmed_email": None,  # For storing confirmed email
            "disconnect_attempts": 0  # Track goodbye attempts
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
                nonlocal stream_sid, call_sid
                try:
                    async for message in websocket.iter_text():
                        try:
                            # Log the raw message for debugging
                            logger.debug(f"Raw message from Twilio: {message[:100]}...")
                            
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
                                # Extract call_sid from stream_sid (Twilio format: CAXXXX.MXXXX)
                                if "." in stream_sid:
                                    call_sid = stream_sid.split('.')[0]
                                    logger.info(f"Extracted call SID: {call_sid}")
                                
                                logger.info(f"Stream started with SID: {stream_sid}")
                                
                                # Initialize conversation state
                                if stream_sid:
                                    conversation_states[stream_sid] = {
                                        "last_response": "",
                                        "waiting_for": None,
                                        "contact_info": None,
                                        "delivery_method": None,
                                        "email_chars": [],
                                        "mobile_digits": [],
                                        "confirmed_email": None,
                                        "confirmed_mobile": None,
                                        "disconnect_attempts": 0
                                    }
                                    
                                    # Store in the global mapping for later use
                                    if call_sid and hasattr(app, 'call_mappings'):
                                        app.call_mappings[call_sid] = {'active': True, 'stream_sid': stream_sid}
                            elif data["event"] == "stop":
                                logger.info("Received stop event from Twilio")
                                # Cleanup on stop event
                                if stream_sid and stream_sid in conversation_states:
                                    del conversation_states[stream_sid]
                                return
                        except json.JSONDecodeError as e:
                            logger.error(f"JSON decode error from Twilio message: {e}")
                        except KeyError as e:
                            logger.error(f"Missing key in Twilio message: {e}")
                except WebSocketDisconnect:
                    logger.warning("WebSocket disconnected by client.")
                except Exception as e:
                    logger.error(f"Error in receive_from_twilio: {str(e)}")
                    import traceback
                    logger.error(traceback.format_exc())
                finally:
                    # Clean up
                    if stream_sid and stream_sid in conversation_states:
                        del conversation_states[stream_sid]
                    logger.info("Exiting receive_from_twilio function")

            async def send_to_twilio():
                try:
                    async for openai_message in openai_ws:
                        response = json.loads(openai_message)
                        
                        # Handle input from user
                        if response.get("type") == "input_audio_buffer.committed":
                            user_input = response.get("text", "").strip().lower()
                            logger.info(f"User input committed: {user_input}")
                            
                            if not stream_sid or stream_sid not in conversation_states:
                                continue
                                
                            state = conversation_states[stream_sid]
                            
                            # Check for call end keywords with increased sensitivity
                            if any(word in user_input for word in ["bye", "goodbye", "end", "hang up", "thank you bye", "no thank you", "that's all"]):
                                state["disconnect_attempts"] += 1
                                logger.info(f"Possible end call keyword detected: '{user_input}', attempt {state['disconnect_attempts']}")
                                
                                # If we've detected it multiple times, or it's a strong match
                                if state["disconnect_attempts"] >= 2 or any(phrase in user_input for phrase in ["goodbye", "hang up", "end call"]):
                                    await inject_assistant_message(openai_ws, "Thank you for calling Alinta Energy. Goodbye!")
                                    # End the call after the goodbye message
                                    asyncio.create_task(terminate_call(call_sid))
                                    return
                            
                            # Process based on conversation state
                            if state["waiting_for"] == "sms_or_email":
                                if "sms" in user_input or "text" in user_input or "message" in user_input:
                                    state["delivery_method"] = "sms"
                                    state["waiting_for"] = "mobile"
                                    state["mobile_digits"] = []
                                    await inject_assistant_message(openai_ws, 
                                        "I'll need your mobile number. Please say each digit one at a time, and I'll confirm each digit.")
                                elif "email" in user_input or "mail" in user_input or "e-mail" in user_input:
                                    state["delivery_method"] = "email"
                                    state["waiting_for"] = "email_chars"
                                    state["email_chars"] = []
                                    await inject_assistant_message(openai_ws, 
                                        "I'll need your email address. Please spell out your email one character at a time. "
                                        "Say 'at' for @, 'dot' for period, and I'll confirm each character.")
                                elif "neither" in user_input or "none" in user_input or "no" in user_input:
                                    state["waiting_for"] = None
                                    await inject_assistant_message(openai_ws, "No problem. Is there anything else I can help you with?")
                            
                            # Handle mobile number input
                            elif state["waiting_for"] == "mobile":
                                # Extract digits
                                digits = re.findall(r'\d', user_input)
                                
                                if digits:
                                    # Process each digit individually with confirmation
                                    for digit in digits:
                                        # Add the digit to our collection
                                        state["mobile_digits"].append(digit)
                                        
                                        # Confirm each digit as it's received
                                        await inject_assistant_message(openai_ws, f"I heard the digit {digit}. "
                                                                    f"So far I have {' '.join(state['mobile_digits'])}. "
                                                                    f"Please continue with the next digit or say 'done' if complete.")
                                    
                                    # Check if we have enough digits for a phone number
                                    if len(state["mobile_digits"]) >= 10:
                                        # Format into a phone number
                                        mobile = ''.join(state["mobile_digits"])
                                        
                                        # Add leading + if international format needed
                                        if not mobile.startswith('+'):
                                            mobile = '+' + mobile
                                            
                                        state["confirmed_mobile"] = mobile
                                        state["waiting_for"] = "confirm_mobile"
                                        
                                        # Spell out the digits for confirmation
                                        spelled_number = ' '.join(state["mobile_digits"])
                                        await inject_assistant_message(openai_ws, 
                                            f"I have your complete number as {spelled_number}. Is that correct? Please say yes or no.")
                                else:
                                    # If "done" is said, check if we have enough digits
                                    if "done" in user_input.lower() and len(state["mobile_digits"]) >= 10:
                                        mobile = ''.join(state["mobile_digits"])
                                        if not mobile.startswith('+'):
                                            mobile = '+' + mobile
                                        state["confirmed_mobile"] = mobile
                                        state["waiting_for"] = "confirm_mobile"
                                        spelled_number = ' '.join(state["mobile_digits"])
                                        await inject_assistant_message(openai_ws, 
                                            f"I have your number as {spelled_number}. Is that correct? Please say yes or no.")
                                    else:
                                        await inject_assistant_message(openai_ws, 
                                            "I didn't catch any digits. Please say the next digit of your mobile number.")
                            
                            # Handle email character input
                            elif state["waiting_for"] == "email_chars":
                                # Process input for characters
                                user_input = user_input.lower().strip()
                                
                                # Handle special characters
                                if "at" in user_input or "@" in user_input:
                                    state["email_chars"].append("@")
                                    char_added = "@"
                                elif "dot" in user_input or "period" in user_input:
                                    state["email_chars"].append(".")
                                    char_added = "."
                                elif "underscore" in user_input:
                                    state["email_chars"].append("_")
                                    char_added = "_"
                                elif "dash" in user_input or "hyphen" in user_input:
                                    state["email_chars"].append("-")
                                    char_added = "-"
                                # Handle single character input
                                elif len(user_input) == 1 and user_input.isalnum():
                                    state["email_chars"].append(user_input)
                                    char_added = user_input
                                # Handle spelled out characters
                                elif user_input in ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", 
                                                "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
                                                "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]:
                                    state["email_chars"].append(user_input)
                                    char_added = user_input
                                # Handle common letter clarifications
                                elif "as in" in user_input:
                                    # Extract the letter from phrases like "b as in boy"
                                    parts = user_input.split()
                                    if parts and len(parts[0]) == 1 and parts[0].isalpha():
                                        state["email_chars"].append(parts[0])
                                        char_added = parts[0]
                                    else:
                                        await inject_assistant_message(openai_ws, 
                                            "I didn't catch that character. Please say a single letter or number.")
                                        continue
                                # Check for completion
                                elif "done" in user_input or "complete" in user_input or "finished" in user_input:
                                    # Check if we have a valid email format with @ symbol
                                    email_so_far = ''.join(state["email_chars"])
                                    if "@" in email_so_far and "." in email_so_far.split("@")[1]:
                                        state["confirmed_email"] = email_so_far
                                        state["waiting_for"] = "confirm_email"
                                        
                                        # Spell out the email for confirmation
                                        spelled_email = spell_out_email(email_so_far)
                                        await inject_assistant_message(openai_ws, 
                                            f"I have your complete email as {spelled_email}. Is that correct? Please say yes or no.")
                                        continue
                                    else:
                                        await inject_assistant_message(openai_ws, 
                                            "The email doesn't seem complete. It should contain an @ symbol and a domain with a dot. Please continue.")
                                        continue
                                else:
                                    await inject_assistant_message(openai_ws, 
                                        "I didn't understand that. Please say a single character, 'at' for @, 'dot' for period, or 'done' when complete.")
                                    continue
                                
                                # Current email construction with feedback
                                email_so_far = ''.join(state["email_chars"])
                                
                                # Confirm the character that was just added
                                await inject_assistant_message(openai_ws, 
                                    f"Added '{char_added}'. Your email so far is {spell_out_email(email_so_far)}. "
                                    f"Please say the next character or 'done' if complete.")
                            
                            # Handle mobile confirmation
                            elif state["waiting_for"] == "confirm_mobile":
                                if "yes" in user_input or "correct" in user_input or "right" in user_input:
                                    # Send the SMS
                                    mobile = state["confirmed_mobile"]
                                    success = await send_sms(mobile, state["last_response"])
                                    state["waiting_for"] = None
                                    
                                    if success:
                                        await inject_assistant_message(openai_ws, 
                                            f"Great! I've sent the information to your mobile. Is there anything else you need help with?")
                                    else:
                                        await inject_assistant_message(openai_ws, 
                                            "I'm sorry, there was an issue sending the SMS. Let's continue with something else. What else can I help you with?")
                                else:
                                    # Start over with mobile
                                    state["mobile_digits"] = []
                                    state["waiting_for"] = "mobile"
                                    await inject_assistant_message(openai_ws, 
                                        "Let's try again. Please say your mobile number one digit at a time, and I'll confirm each digit.")
                            
                            # Handle email confirmation
                            elif state["waiting_for"] == "confirm_email":
                                if "yes" in user_input or "correct" in user_input or "right" in user_input:
                                    # Send the email
                                    email_address = state["confirmed_email"]
                                    success = await send_email(email_address, "Information from Alinta Energy", state["last_response"])
                                    state["waiting_for"] = None
                                    
                                    if success:
                                        await inject_assistant_message(openai_ws, 
                                            f"Great! I've sent the information to {email_address}. Is there anything else you need help with?")
                                    else:
                                        await inject_assistant_message(openai_ws, 
                                            "I'm sorry, there was an issue sending the email. Let's continue with something else. What else can I help you with?")
                                else:
                                    # Start over with email
                                    state["email_chars"] = []
                                    state["waiting_for"] = "email_chars"
                                    await inject_assistant_message(openai_ws, 
                                        "Let's try again. Please spell out your email one character at a time. I'll confirm each character as we go.")

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
                            try:
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
                            except Exception as e:
                                logger.error(f"Error sending audio to Twilio: {e}")

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

# Add this as a standalone function
async def terminate_call(call_sid):
    """Forcefully terminate a Twilio call"""
    try:
        logger.info(f"Attempting to terminate call: {call_sid}")
        
        # Update call status in our tracking
        if hasattr(app, 'call_mappings') and call_sid in app.call_mappings:
            app.call_mappings[call_sid]['active'] = False
        
        # Use Twilio client to end the call
        result = twilio_client.calls(call_sid).update(status="completed")
        logger.info(f"Call termination result: {result.status}")
        
        # Alternative method if the above doesn't work
        # Note: This requires proper Twilio API credentials
        twilio_account_sid = TWILIO_ACCOUNT_SID
        twilio_auth_token = TWILIO_AUTH_TOKEN
        
        # Log that we're using alternative method
        logger.info("Using alternative call termination method")
        
        # Make a direct request to Twilio API
        import requests
        url = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_account_sid}/Calls/{call_sid}.json"
        data = {"Status": "completed"}
        response = requests.post(url, data=data, auth=(twilio_account_sid, twilio_auth_token))
        
        logger.info(f"Alternative termination status code: {response.status_code}")
        logger.info(f"Response: {response.text}")
        
        return True
    except Exception as e:
        logger.error(f"Failed to terminate call: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return False


# Add this to the beginning of your file after imports
# To ensure Twilio client is properly initialized
def verify_twilio_setup():
    """Verify Twilio setup and log information"""
    try:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
            logger.error("Twilio credentials not properly configured")
            return False
            
        logger.info(f"Initializing Twilio client with account: {TWILIO_ACCOUNT_SID[:5]}...{TWILIO_ACCOUNT_SID[-5:]}")
        
        # Test the client by getting account info
        account = twilio_client.api.accounts(TWILIO_ACCOUNT_SID).fetch()
        logger.info(f"Twilio account status: {account.status}")
        
        # Verify phone number
        if not TWILIO_PHONE_NUMBER:
            logger.error("Twilio phone number not configured")
            return False
            
        logger.info(f"Using Twilio phone number: {TWILIO_PHONE_NUMBER}")
        return True
    except Exception as e:
        logger.error(f"Twilio setup verification failed: {str(e)}")
        return False

def spell_out_email(email):
    """Spell out an email address for clearer voice confirmation"""
    result = ""
    for char in email:
        if char == '@':
            result += " at "
        elif char == '.':
            result += " dot "
        else:
            result += f" {char} "
    return result

async def end_call(call_sid):
    """End the Twilio call immediately"""
    try:
        if not call_sid:
            logger.error("No call_sid provided for ending call")
            return
            
        logger.info(f"Ending call with SID: {call_sid}")
        
        # End the call using Twilio's API
        twilio_client.calls(call_sid).update(status="completed")
        logger.info(f"Call with SID {call_sid} has been ended")
        
    except Exception as e:
        logger.error(f"Error ending call: {e}")
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
    """Send SMS using Twilio with improved error handling."""
    try:
        # Log the attempt
        logger.info(f"Attempting to send SMS to {phone_number}")
        
        # Clean the phone number
        clean_number = ''.join(filter(str.isdigit, phone_number))
        
        # Add country code if missing
        if not clean_number.startswith('+'):
            # Default to +1 (US) if unspecified - adjust for your region
            clean_number = '+' + clean_number
            
        logger.info(f"Formatted phone number: {clean_number}")
        
        # Truncate message if too long for SMS
        if len(message) > 1600:
            message = message[:1597] + "..."
            
        # Debug log Twilio credentials (partial)
        logger.info(f"Using Twilio account: {TWILIO_ACCOUNT_SID[:5]}...{TWILIO_ACCOUNT_SID[-5:]}")
        logger.info(f"Using Twilio phone number: {TWILIO_PHONE_NUMBER}")
        
        # Actually send the SMS
        message_resource = twilio_client.messages.create(
            body=message,
            from_=TWILIO_PHONE_NUMBER,
            to=clean_number
        )
        
        # Log success with message SID
        logger.info(f"SMS sent successfully to {clean_number}, Message SID: {message_resource.sid}")
        return True
    except Exception as e:
        logger.error(f"Error sending SMS: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
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


# Mobile number collection with digit-by-digit confirmation
async def handle_mobile_input(user_input, state, openai_ws):
    """Handle mobile number input with digit-by-digit confirmation"""
    # Extract digits
    digits = re.findall(r'\d', user_input)
    
    if digits:
        # Process each digit individually with confirmation
        for digit in digits:
            # Add the digit to our collection
            state["mobile_digits"].append(digit)
            
            # Confirm each digit as it's received
            await inject_assistant_message(openai_ws, f"I heard the digit {digit}. "
                                          f"So far I have {' '.join(state['mobile_digits'])}. "
                                          f"Please continue with the next digit or say 'done' if complete.")
        
        # Check if we have enough digits for a phone number
        if len(state["mobile_digits"]) >= 10:
            # Format into a phone number
            mobile = ''.join(state["mobile_digits"])
            
            # Add leading + if international format needed
            if not mobile.startswith('+'):
                mobile = '+' + mobile
                
            state["confirmed_mobile"] = mobile
            state["waiting_for"] = "confirm_mobile"
            
            # Spell out the digits for confirmation
            spelled_number = ' '.join(state["mobile_digits"])
            await inject_assistant_message(openai_ws, 
                f"I have your complete number as {spelled_number}. Is that correct? Please say yes or no.")
    else:
        # If "done" is said, check if we have enough digits
        if "done" in user_input.lower() and len(state["mobile_digits"]) >= 10:
            mobile = ''.join(state["mobile_digits"])
            if not mobile.startswith('+'):
                mobile = '+' + mobile
            state["confirmed_mobile"] = mobile
            state["waiting_for"] = "confirm_mobile"
            spelled_number = ' '.join(state["mobile_digits"])
            await inject_assistant_message(openai_ws, 
                f"I have your number as {spelled_number}. Is that correct? Please say yes or no.")
        else:
            await inject_assistant_message(openai_ws, 
                "I didn't catch any digits. Please say the next digit of your mobile number.")


# Email collection with character-by-character confirmation
async def handle_email_username_input(user_input, state, openai_ws):
    """Handle email username input with character-by-character confirmation"""
    # Check if this is the first character or continuing
    if not state.get("email_chars", []):
        state["email_chars"] = []
        await inject_assistant_message(openai_ws, 
            "Please spell out your email address one character at a time. Say 'at' for @ and 'dot' for period.")
        return
    
    # Process input for characters
    user_input = user_input.lower().strip()
    
    # Handle special characters
    if "at" in user_input or "@" in user_input:
        state["email_chars"].append("@")
        char_added = "@"
    elif "dot" in user_input or "period" in user_input:
        state["email_chars"].append(".")
        char_added = "."
    elif "underscore" in user_input:
        state["email_chars"].append("_")
        char_added = "_"
    elif "dash" in user_input or "hyphen" in user_input:
        state["email_chars"].append("-")
        char_added = "-"
    # Handle single character input
    elif len(user_input) == 1 and user_input.isalnum():
        state["email_chars"].append(user_input)
        char_added = user_input
    # Handle spelled out characters
    elif user_input in ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", 
                      "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
                      "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]:
        state["email_chars"].append(user_input)
        char_added = user_input
    # Handle common letter clarifications
    elif "as in" in user_input:
        # Extract the letter from phrases like "b as in boy"
        parts = user_input.split()
        if parts and len(parts[0]) == 1 and parts[0].isalpha():
            state["email_chars"].append(parts[0])
            char_added = parts[0]
        else:
            await inject_assistant_message(openai_ws, 
                "I didn't catch that character. Please say a single letter or number.")
            return
    # Check for completion
    elif "done" in user_input or "complete" in user_input or "finished" in user_input:
        # Check if we have a valid email format with @ symbol
        email_so_far = ''.join(state["email_chars"])
        if "@" in email_so_far and "." in email_so_far.split("@")[1]:
            state["confirmed_email"] = email_so_far
            state["waiting_for"] = "confirm_email"
            
            # Spell out the email for confirmation
            spelled_email = spell_out_email(email_so_far)
            await inject_assistant_message(openai_ws, 
                f"I have your complete email as {spelled_email}. Is that correct? Please say yes or no.")
            return
        else:
            await inject_assistant_message(openai_ws, 
                "The email doesn't seem complete. It should contain an @ symbol and a domain with a dot. Please continue.")
            return
    else:
        await inject_assistant_message(openai_ws, 
            "I didn't understand that. Please say a single character, 'at' for @, 'dot' for period, or 'done' when complete.")
        return
    
    # Current email construction with feedback
    email_so_far = ''.join(state["email_chars"])
    
    # Confirm the character that was just added
    await inject_assistant_message(openai_ws, 
        f"Added '{char_added}'. Your email so far is {spell_out_email(email_so_far)}. "
        f"Please say the next character or 'done' if complete.")

if __name__ == "__main__":
    import uvicorn
    verify_twilio_setup()

    uvicorn.run(app, host="0.0.0.0", port=PORT)