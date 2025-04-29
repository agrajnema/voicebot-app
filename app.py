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
import re, datetime, sys, time

from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

load_dotenv()

# Configuration
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_API_ENDPOINT = os.getenv("AZURE_OPENAI_API_ENDPOINT")
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
AZURE_SEARCH_KEY = os.getenv("AZURE_SEARCH_KEY")
AZURE_SEARCH_INDEX = os.getenv("AZURE_SEARCH_INDEX")
AZURE_SEARCH_SEMANTIC_CONFIGURATION = os.getenv("AZURE_SEARCH_SEMANTIC_CONFIGURATION")
AZURE_LOG_CONNECTION_STRING = os.getenv("AZURE_LOG_CONNECTION_STRING")

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

# Configure Azure Monitor if connection string is available
if AZURE_LOG_CONNECTION_STRING:
    try:
        configure_azure_monitor(
            connection_string=AZURE_LOG_CONNECTION_STRING,
        )
        tracer = trace.get_tracer("alinta-voicebot")
        logger.info("Azure Log Analytics configured successfully")
    except Exception as e:
        logger.error(f"Failed to configure Azure Log Analytics: {e}")
        tracer = None
else:
    logger.warning("Azure Log Analytics connection string not provided")
    tracer = None

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

# Create a function for live streaming conversation events
def stream_conversation(direction, text, call_sid=None):
    """Stream conversation events to console and Azure Log Stream in real-time"""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    call_info = f"[Call: {call_sid}] " if call_sid else ""
    
    # ANSI color codes for better visibility
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RESET = "\033[0m"
    
    # Format the message based on direction
    if direction == "user":
        log_message = f"{call_info}USER → SYSTEM: {text}"
        print(f"{BLUE}{timestamp} {log_message}{RESET}", flush=True)
    elif direction == "system":
        log_message = f"{call_info}SYSTEM → USER: {text}"
        print(f"{GREEN}{timestamp} {log_message}{RESET}", flush=True)
    elif direction == "system-partial":
        log_message = f"{call_info}SYSTEM (typing): {text}"
        print(f"{YELLOW}{timestamp} {log_message}{RESET}", flush=True)
    else:
        log_message = f"{call_info}{direction}: {text}"
        print(f"{timestamp} {log_message}", flush=True)
    
    # Log to file for permanent record
    conversation_logger = logging.getLogger("conversation")
    conversation_logger.info(f"{call_info}[{direction}] {text}")
    
    # Send to Azure Log Analytics if configured
    if tracer and call_sid:
        with tracer.start_as_current_span(f"conversation-{direction}") as span:
            # Add relevant attributes
            span.set_attribute("call_sid", call_sid)
            span.set_attribute("direction", direction)
            span.set_attribute("message", text)
            span.set_attribute("timestamp", timestamp)
            
            # Set status based on direction (for potential error tracking)
            span.set_status(Status(StatusCode.OK))
            
            # Add additional context if needed
            if direction == "user":
                span.add_event("user_speech", {"text": text})
            elif direction == "system":
                span.add_event("system_response", {"text": text})

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
    
    # Get form data to capture the call SID
    form_data = await request.form()
    call_sid = form_data.get('CallSid')
    logger.info(f"Incoming call with SID: {call_sid}")
    
    # Get the public-facing hostname
    host = request.headers.get("X-Forwarded-Host", request.url.hostname)
    if request.url.port and request.url.port != 443:
        host = f"{host}:{request.url.port}"
    
    # Create the Connect verb with proper WebSocket URL
    connect = Connect()
    ws_url = f"wss://{host}/media-stream"
    connect.stream(url=ws_url)
    
    # Add the connect element to the response - no greeting here, just connect
    response.append(connect)
    
    # Play a DTMF tone (beep) to signal the user should wait
    response.play(digits="1w")
    
    # Log the full TwiML for debugging
    logger.info(f"Generated TwiML response: {str(response)}")
    
    # Return the TwiML response
    return HTMLResponse(content=str(response), media_type="application/xml")


# Add this to your global variables
conversation_states = {}

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    logger.info("WebSocket connection attempt from Twilio.")
    try:
        # Accept the WebSocket connection
        await websocket.accept()
        logger.info("WebSocket connection successfully accepted.")

        stream_sid = None
        call_sid = None
        greeting_sent = False  # Track if the initial greeting was sent
        
        # Initialize conversation state
        conversation_state = {
            "last_response": "",
            "waiting_for": "initial_greeting",  # Start by waiting to send greeting
            "flow_stage": "greeting",
            "intent_confirmed": False,
            "contact_info": None,
            "delivery_method": None,
            "email_chars": [],
            "mobile_digits": [],
            "confirmed_email": None,
            "confirmed_mobile": None,
            "disconnect_attempts": 0,
            "last_query": "",
            "last_result": "",
            "email_disclaimer_sent": False
        }

        # Connect to OpenAI with proper headers
        headers = {"api-key": AZURE_OPENAI_API_KEY}
        
        try:
            openai_ws = await websockets.connect(AZURE_OPENAI_API_ENDPOINT, extra_headers=headers)
            logger.info("Successfully connected to OpenAI API")
        except Exception as e:
            logger.error(f"Failed to connect to OpenAI API: {e}")
            await websocket.close()
            return
        
        try:
            # Initialize the OpenAI session
            await initialize_session(openai_ws)
            logger.info("OpenAI session initialized successfully")
            
            # Send initial greeting message as soon as connection is established
            async def trigger_initial_greeting():
                try:
                    # Wait a short moment for everything to initialize
                    await asyncio.sleep(0.5)
                    
                    # Trigger the greeting by sending an empty message to get the AI to start
                    logger.info("Triggering initial greeting from OpenAI")
                    await openai_ws.send(json.dumps({"type": "response.create"}))
                except Exception as e:
                    logger.error(f"Error sending initial greeting: {e}")
            
            # Start the greeting trigger task
            greeting_task = asyncio.create_task(trigger_initial_greeting())
            
            # Function to handle Twilio reception
            async def receive_from_twilio():
                nonlocal stream_sid, call_sid
                try:
                    async for message in websocket.iter_text():
                        try:
                            data = json.loads(message)
                            event_type = data.get("event")
                            logger.info(f"Received Twilio event: {event_type}")
                            
                            if event_type == "media":
                                audio_append = {
                                    "type": "input_audio_buffer.append",
                                    "audio": data["media"]["payload"],
                                }
                                await openai_ws.send(json.dumps(audio_append))
                            elif event_type == "start":
                                stream_sid = data["start"]["streamSid"]
                                # Extract call_sid from stream_sid
                                if "." in stream_sid:
                                    call_sid = stream_sid.split('.')[0]
                                    # Log the call_sid prominently to easily find it for transcript searches
                                    logger.critical(f"CALL_SID_FOR_TRANSCRIPT_SEARCH: {call_sid}")
                                    logger.info(f"Extracted call SID: {call_sid}")
                                    # Start conversation tracking in Azure
                                    track_complete_conversation(call_sid)
                                
                                logger.info(f"Stream started with SID: {stream_sid}")
                                
                                # Initialize conversation state
                                if stream_sid:
                                    conversation_states[stream_sid] = conversation_state
                            elif event_type == "stop":
                                logger.info("Received stop event from Twilio")
                                # Save complete transcript before cleanup
                                if call_sid:
                                    save_complete_transcript(call_sid)
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
                    # Save transcript on disconnect
                    if call_sid:
                        save_complete_transcript(call_sid)
                except Exception as e:
                    logger.error(f"Error in receive_from_twilio: {str(e)}")
                    import traceback
                    logger.error(traceback.format_exc())
                finally:
                    # Save transcript before cleaning up
                    if call_sid:
                        save_complete_transcript(call_sid)
                    # Clean up
                    if stream_sid and stream_sid in conversation_states:
                        del conversation_states[stream_sid]
                    logger.info("Exiting receive_from_twilio function")

            # Function to handle sending to Twilio
            async def send_to_twilio():
                # Initialize buffer for partial text updates
                partial_text_buffer = ""
                last_partial_update = time.time()
                
                try:
                    async for openai_message in openai_ws:
                        try:
                            response = json.loads(openai_message)
                            response_type = response.get("type", "")
                            
                            # Handle user input committed by OpenAI
                            if response_type == "input_audio_buffer.committed":
                                user_input = response.get("text", "").strip().lower()
                                logger.info(f"User input committed: '{user_input}'")
                                
                                # Stream user speech in real-time
                                stream_conversation("user", user_input, call_sid)
                                # Add to complete transcript
                                add_to_conversation_transcript(call_sid, "user", user_input)
                                # Also log to Azure with more detail
                                log_conversation_to_azure(call_sid, "user", user_input, {
                                    "flow_stage": state.get("flow_stage", "unknown"),
                                    "waiting_for": state.get("waiting_for", "unknown")
                                })

                                if not stream_sid or stream_sid not in conversation_states:
                                    logger.warning("No active conversation state found")
                                    continue
                                    
                                state = conversation_states[stream_sid]
                                state["last_query"] = user_input
                                
                                # Check for call end keywords in any context
                                end_call_phrases = ["bye", "goodbye", "end", "hang up", "thank you bye", 
                                                "no thank you", "that's all", "that is all"]
                                
                                if any(phrase in user_input for phrase in end_call_phrases):
                                    logger.info(f"End call keyword detected: '{user_input}'")
                                    await inject_assistant_message(openai_ws, "Thanks for calling Alinta Energy.")
                                    # End the call after a short delay
                                    asyncio.create_task(end_call_with_delay(call_sid, 2))
                                    return
                                
                                # Handle "Is that right?" confirmation
                                if "is that right" in state.get("last_response", "").lower() and not state.get("intent_confirmed", False):
                                    logger.info(f"Processing intent confirmation. User said: '{user_input}'")
                                    
                                    # Check for affirmative response
                                    if any(word in user_input.lower() for word in ["yes", "correct", "right", "yeah", "yep", "that's right"]):
                                        state["intent_confirmed"] = True
                                        logger.info("User CONFIRMED intent")
                                        # Let the normal flow continue
                                    
                                    # Check for negative response
                                    elif any(word in user_input.lower() for word in ["no", "incorrect", "wrong", "not right", "nope"]):
                                        logger.info("User REJECTED intent - Interrupting AI and resetting flow")

                                        # Reset relevant state variables
                                        state["intent_confirmed"] = False
                                        state["last_query"] = ""
                                        state["waiting_for"] = "clarification"
                                        
                                        # Use the new interruption function
                                        success = await force_ai_interrupt(openai_ws, 
                                            "I apologize for misunderstanding. Please tell me again what you're looking for, and I'll do my best to help.")
                                        
                                        if not success:
                                            # Fallback to the old method if interruption fails
                                            await inject_assistant_message(openai_ws, 
                                                "I apologize for misunderstanding. Please tell me again what you're looking for, and I'll do my best to help.")
                                        
                                        # Critical: Return early to prevent any additional processing
                                        return
                                
                                # Check if we need to update waiting_for state based on AI's last response
                                if "would you like to receive the links by sms or email" in state.get("last_response", "").lower():
                                    state["waiting_for"] = "sms_or_email"
                                    logger.info("Setting state to waiting for SMS or Email selection")

                                # Process different conversation states
                                if state["waiting_for"] == "sms_or_email":
                                    # Critical logging - log exact input with character-by-character inspection
                                    user_input_raw = user_input
                                    user_input_lower = user_input.lower().strip()
                                    
                                    logger.critical(f"SMS/EMAIL DETECTION - Raw input: '{user_input_raw}'")
                                    logger.critical(f"SMS/EMAIL DETECTION - Lowercase: '{user_input_lower}'")
                                    logger.critical(f"SMS/EMAIL DETECTION - Character codes: {[ord(c) for c in user_input_lower]}")
                                    
                                    # ULTRA-SIMPLE detection - look for the exact strings only, nothing fancy
                                    if "sms" in user_input_lower or "text" in user_input_lower:
                                        logger.critical("SMS KEYWORD FOUND - SELECTING SMS")
                                        state["delivery_method"] = "sms"
                                        state["waiting_for"] = "mobile_collection"
                                        
                                        await inject_assistant_message(openai_ws, 
                                            "Whilst you can do this online, I can text details. Please say your mobile number.")

                                    elif "email" in user_input_lower or "mail" in user_input_lower:
                                        logger.critical("EMAIL KEYWORD FOUND - SELECTING EMAIL")
                                        state["delivery_method"] = "email"
                                        state["waiting_for"] = "email_collection"
                                        
                                        await inject_assistant_message(openai_ws, 
                                            "Whilst you can do this online, I can send you an email with details. "
                                            "Please say your email address now.")
                                                                    
                                # Handle email collection
                                elif state["waiting_for"] == "email_collection":
                                    # Log the exact input received for debugging
                                    logger.critical(f"EMAIL COLLECTION - Raw input: '{user_input}'")
                                    
                                    # Simplify the input - replace spoken versions with symbols
                                    processed_input = user_input.lower()
                                    processed_input = processed_input.replace(" at ", "@").replace(" dot ", ".")
                                    
                                    # Log the processed input
                                    logger.critical(f"EMAIL COLLECTION - Processed input: '{processed_input}'")
                                    
                                    # SIMPLIFIED: Just extract anything that looks like an email
                                    email_pattern = r'[\w\.-]+@[\w\.-]+\.\w+'
                                    email_matches = re.findall(email_pattern, processed_input)
                                    
                                    if email_matches:
                                        # Take the first match that looks like an email
                                        email = email_matches[0]
                                        logger.critical(f"EMAIL COLLECTION - Extracted email: '{email}'")
                                        
                                        # Store the EXACT email as extracted, without any modification
                                        state["confirmed_email"] = email
                                        state["waiting_for"] = "confirm_email"
                                        
                                        # Keep email exactly as captured, don't modify it
                                        confirmation_message = f"Is {email} correct?"
                                        
                                        logger.critical(f"EMAIL COLLECTION - Confirmation message: '{confirmation_message}'")
                                        await inject_assistant_message(openai_ws, confirmation_message)
                                    else:
                                        # No email pattern found
                                        logger.critical("EMAIL COLLECTION - No email pattern found")
                                        await inject_assistant_message(openai_ws, 
                                            "I couldn't detect a valid email address. Please say your complete email address again, "
                                            "saying 'at' for @ and 'dot' for period. For example, 'john at gmail dot com'.")

                                # Handle mobile number collection
                                elif state["waiting_for"] == "mobile_collection":
                                    logger.info(f"Processing mobile input: '{user_input}'")
                                    # Extract all digits from the speech
                                    digits = re.findall(r'\d', user_input)
                                    
                                    if len(digits) >= 10:  # If we have enough digits for a phone number
                                        mobile = ''.join(digits)
                                        
                                        # Format properly if needed
                                        if not mobile.startswith('+'):
                                            # Determine if country code is included or needs to be added
                                            if len(mobile) > 10:
                                                # Assume it includes country code
                                                pass
                                            else:
                                                # Add default country code (adjust for your region)
                                                mobile = '+61' + mobile  # Example: Australia
                                        
                                        state["confirmed_mobile"] = mobile
                                        state["waiting_for"] = "confirm_mobile"
                                        
                                        # Spell out the complete number for confirmation
                                        spelled_number = ' '.join(mobile)
                                        await inject_assistant_message(openai_ws, 
                                            f"I have your mobile number as {spelled_number}. Is that correct? Please say yes or no.")
                                    else:
                                        await inject_assistant_message(openai_ws, 
                                            "I couldn't capture enough digits for a complete mobile number. Please say your full mobile number again, "
                                            "speaking clearly. For example, '0412 345 678'.")
                                
                                # Handle email confirmation
                                elif state["waiting_for"] == "confirm_email":
                                    logger.info(f"Processing email confirmation: '{user_input}'")
                                    if any(word in user_input for word in ["yes", "correct", "right", "yep", "yeah"]):
                                        email_address = state["confirmed_email"]
                                        logger.info(f"Sending email to confirmed address: {email_address}")
                                        
                                        # Send the email
                                        success = await send_email(email_address, "Information from Alinta Energy", state["last_result"])
                                        state["waiting_for"] = None
                                        
                                        if success:
                                            await inject_assistant_message(openai_ws, 
                                                f"Great! I've sent the information to {email_address}. Is there anything else you need help with?")
                                        else:
                                            logger.error(f"Failed to send email to {email_address}")
                                            await inject_assistant_message(openai_ws, 
                                                f"I'm sorry, there was an issue sending the email to {email_address}. Let's continue with something else. What else can I help you with?")
                                    else:
                                        # Start over with email
                                        state["waiting_for"] = "email_collection"
                                        await inject_assistant_message(openai_ws, 
                                            "Let's try again. Please say your complete email address, saying 'at' for @ and 'dot' for period.")
                                # Handle mobile confirmation
                                elif state["waiting_for"] == "confirm_mobile":
                                    logger.info(f"Processing mobile confirmation: '{user_input}'")
                                    if any(word in user_input for word in ["yes", "correct", "right", "yep", "yeah"]):
                                        mobile = state["confirmed_mobile"]
                                        if not mobile.startswith('+'):
                                            mobile = '+' + mobile
                                        
                                        # Format the number in a readable way
                                        formatted_mobile = ' '.join(mobile)
                                        
                                        logger.info(f"Sending SMS to confirmed number: {mobile}")
                                        success = await send_sms(mobile, state["last_result"])
                                        state["waiting_for"] = None
                                        
                                        if success:
                                            await inject_assistant_message(openai_ws, 
                                                f"Great! I've sent the information to your mobile number {formatted_mobile}. Is there anything else you need help with?")
                                        else:
                                            logger.error(f"Failed to send SMS to {mobile}")
                                            await inject_assistant_message(openai_ws, 
                                                f"I'm sorry, there was an issue sending the SMS to {formatted_mobile}. Let's continue with something else. What else can I help you with?")
                                    else:
                                        # Start over with mobile
                                        state["waiting_for"] = "mobile_collection"
                                        await inject_assistant_message(openai_ws, 
                                            "Let's try again. Please say your complete mobile number.")
                            
                            # Handle AI response text (TEXT TO VOICE) - delta events for streaming
                            elif response_type == "response.text.delta":
                                # Collect partial responses
                                delta_text = response.get("delta", "")
                                if delta_text:
                                    partial_text_buffer += delta_text
                                    
                                    # Show partial updates but not too frequently (every 100ms)
                                    current_time = time.time()
                                    if current_time - last_partial_update > 0.1:
                                        stream_conversation("system-partial", partial_text_buffer, call_sid)
                                        last_partial_update = current_time
                                        partial_text_buffer = ""  # Reset buffer after displaying
                                        
                                    # Also store in state for context
                                    if stream_sid and stream_sid in conversation_states:
                                        state = conversation_states[stream_sid]
                                        if "current_response" not in state:
                                            state["current_response"] = ""
                                        state["current_response"] += delta_text
                                        
                            # Handle AI response text
                            elif response_type == "response.text.done":
                                full_response = response.get("text", "")
                                logger.info(f"AI full response: {full_response}")
                                
                                # Stream the complete response
                                stream_conversation("system", full_response, call_sid)
                                # Add to complete transcript
                                add_to_conversation_transcript(call_sid, "system", full_response)

                                log_conversation_to_azure(call_sid, "system", full_response, {
                                    "flow_stage": state.get("flow_stage", "unknown"),
                                    "waiting_for": state.get("waiting_for", "unknown")
                                })
                                
                                if stream_sid and stream_sid in conversation_states:
                                    state = conversation_states[stream_sid]
                                    
                                    # Check and enforce proper SMS/Email selection flow
                                    corrected = await enforce_sms_email_state(openai_ws, response, state)
                                    if not corrected:
                                        # Only update last_response if we didn't correct a premature selection
                                        state["last_response"] = full_response
                                        
                                        # Save content for sending later via SMS or email
                                        if "would you like to receive the links by sms or email" in full_response.lower():
                                            state["last_result"] = full_response
                                            state["waiting_for"] = "sms_or_email"
                                            logger.info("Detected SMS/Email prompt - waiting for user selection")
                                        
                                        # Check for ending message and disconnect call
                                        if "thanks for calling alinta energy" in full_response.lower():
                                            logger.info("Detected call ending message, will terminate call")
                                            # Make sure to save the transcript before ending the call
                                            save_complete_transcript(call_sid)
                                            asyncio.create_task(end_call_with_delay(call_sid, 2))
                            
                            # Handle audio response
                            elif response_type == "response.audio.delta" and "delta" in response:
                                try:
                                    # Decode the base64 audio from OpenAI and re-encode it for Twilio
                                    audio_payload = base64.b64encode(
                                        base64.b64decode(response["delta"])
                                    ).decode("utf-8")
                                    
                                    # Create the message structure Twilio expects
                                    audio_delta = {
                                        "event": "media",
                                        "streamSid": stream_sid,
                                        "media": {"payload": audio_payload},
                                    }
                                    
                                    # Convert to JSON string before sending - use send_json() instead of send_text()
                                    await websocket.send_json(audio_delta)
                                    
                                    # Alternative if send_json is not available
                                    # await websocket.send_text(json.dumps(audio_delta))
                                except Exception as e:
                                    logger.error(f"Error sending audio to Twilio: {e}")
                            
                            # Handle function calls for RAG
                            elif response_type == "response.function_call_arguments.done":
                                function_name = response["name"]
                                if function_name == "get_additional_context":
                                    query = json.loads(response["arguments"]).get("query", "")
                                    search_results = azure_search_rag(query)
                                    logger.info(f"RAG Results: {search_results}")
                                    await send_function_output(openai_ws, response["call_id"], search_results)
                        except json.JSONDecodeError as e:
                            logger.error(f"JSON parse error for OpenAI message: {e}")
                        except Exception as e:
                            logger.error(f"Error processing OpenAI message: {e}")
                            import traceback
                            logger.error(traceback.format_exc())
                except Exception as e:
                    logger.error(f"Error in send_to_twilio: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                finally:
                    # Make sure we save the transcript before exiting
                    if call_sid:
                        save_complete_transcript(call_sid)

        except Exception as e:
            logger.error(f"Critical WebSocket error: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
    except Exception as e:
                logger.error(f"Critical WebSocket error: {str(e)}")
                import traceback
                logger.error(traceback.format_exc())
    finally:
        # Clean up WebSocket connection
        if not websocket.client_state == WebSocket.DISCONNECTED:
            await websocket.close()
        logger.info("WebSocket connection closed")
        
def log_conversation_to_azure(call_sid, direction, text, metadata=None):
    """Send detailed conversation logs to Azure Log Analytics"""
    if not tracer or not call_sid:
        return
        
    # Create a more detailed span with conversation-specific attributes
    with tracer.start_as_current_span("voice_conversation") as span:
        # Basic attributes
        span.set_attribute("call_sid", call_sid)
        span.set_attribute("direction", direction)
        span.set_attribute("message", text)
        span.set_attribute("timestamp", datetime.datetime.now().isoformat())
        
        # Add any additional metadata
        if metadata:
            for key, value in metadata.items():
                span.set_attribute(key, str(value))
        
        # Add conversation-specific events
        if direction == "user":
            span.add_event("user_speech", {"text": text})
        elif direction == "system":
            span.add_event("system_response", {"text": text})
        elif direction == "system-partial":
            span.add_event("system_typing", {"text": text})
        
        # Add the event to track full conversation flow
        conversation_event_name = f"{direction}_message"
        span.add_event(conversation_event_name, {"call_sid": call_sid, "text": text})

def track_complete_conversation(call_sid):
    """Start tracking a complete conversation for a call"""
    if not call_sid:
        return None
    
    # Initialize transcript
    initialize_conversation_transcript(call_sid)
    
    # Also create a span for the call if tracer is available
    if tracer:
        span = tracer.start_span(f"call_{call_sid}")
        span.set_attribute("call_sid", call_sid)
        span.set_attribute("start_time", datetime.datetime.now().isoformat())
        
        # Store the span so we can access it later
        if not hasattr(app, 'call_spans'):
            app.call_spans = {}
        app.call_spans[call_sid] = span
        
        return span
    
    return None

def end_conversation_tracking(call_sid):
    """End tracking of a complete conversation"""
    # Save the complete transcript
    save_complete_transcript(call_sid)
    
    # Also end the span if it exists
    if hasattr(app, 'call_spans') and call_sid in app.call_spans:
        span = app.call_spans.pop(call_sid)
        span.set_attribute("end_time", datetime.datetime.now().isoformat())
        span.end()
        
    logger.critical(f"CONVERSATION_TRACKING_ENDED: CALL_SID={call_sid}")

async def force_ai_interrupt(openai_ws, message):
    """Force the AI to stop its current train of thought and respond with a new direction"""
    try:
        logger.info("Forcing AI interruption")
        
        # First, try to cancel any ongoing response
        cancel_message = {
            "type": "response.cancel"
        }
        await openai_ws.send(json.dumps(cancel_message))
        
        # Wait a moment for cancellation to process
        await asyncio.sleep(0.2)
        
        # Insert a system message to guide the AI behavior
        system_guidance = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "system",
                "content": [{"type": "text", "text": 
                    "The user has indicated the previous understanding was incorrect. "
                    "Disregard previous context and start fresh with the user's new input."}]
            }
        }
        await openai_ws.send(json.dumps(system_guidance))
        
        # Wait a moment for the system message to be processed
        await asyncio.sleep(0.2)
        
        # Now send the actual message we want the AI to say
        new_message = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": message}]
            }
        }
        await openai_ws.send(json.dumps(new_message))
        
        # Prompt the AI to create a new response based on the updated context
        await openai_ws.send(json.dumps({"type": "response.create"}))
        
        logger.info("AI interruption complete")
        return True
    except Exception as e:
        logger.error(f"Error during AI interruption: {e}")
        return False
    
def extract_first_char(text):
    """Try to extract the first character from various input formats"""
    # Check for common phrases like "a as in apple"
    match = re.search(r'([a-z])\s+as\s+in', text)
    if match:
        return match.group(1)
    
    # Check for phrases like "capital B" or "uppercase B"
    match = re.search(r'(?:capital|uppercase)\s+([a-z])', text)
    if match:
        return match.group(1).upper()
    
    # For simple letter mentions (first letter mentioned)
    match = re.search(r'\b([a-z])\b', text)
    if match:
        return match.group(1)
    
    # For number mentions
    match = re.search(r'\b(zero|one|two|three|four|five|six|seven|eight|nine)\b', text)
    if match:
        number_words = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", 
                        "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
        return number_words.get(match.group(1))
    
    return None

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

# Helper functions
def spell_out_email(email):
    """Spell out an email address for clearer voice confirmation"""
    result = ""
    for char in email:
        if char == '@':
            result += " at "
        elif char == '.':
            result += " dot "
        elif char == '_':
            result += " underscore "
        elif char == '-':
            result += " dash "
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
async def end_call_with_delay(call_sid, delay_seconds=2):
    """End the call after a delay to allow for goodbye message"""
    if not call_sid:
        logger.error("No call_sid available for ending call")
        return
    
    logger.info(f"Will end call {call_sid} after {delay_seconds} seconds")
    await asyncio.sleep(delay_seconds)
    
    end_conversation_tracking(call_sid)

    try:
        # Use Twilio client to end the call
        logger.info(f"Attempting to end call {call_sid}")
        twilio_client.calls(call_sid).update(status="completed")
        logger.info(f"Call {call_sid} has been ended")
    except Exception as e:
        logger.error(f"Error ending call with Twilio client: {e}")
        
        # Fallback: Try direct API call
        try:
            import requests
            logger.info(f"Attempting fallback method to end call {call_sid}")
            url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{call_sid}.json"
            data = {"Status": "completed"}
            response = requests.post(url, data=data, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
            logger.info(f"Fallback call termination result: {response.status_code}, {response.text}")
        except Exception as e2:
            logger.error(f"Fallback call termination also failed: {e2}")
            
        # Last resort: Try webhook approach
        try:
            logger.info(f"Attempting last resort method to end call {call_sid}")
            # This creates a TwiML that hangs up the call
            hangup_response = VoiceResponse()
            hangup_response.hangup()
            
            # Send this to Twilio via their API
            url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{call_sid}.json"
            data = {"Twiml": str(hangup_response)}
            response = requests.post(url, data=data, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
            logger.info(f"Last resort call termination result: {response.status_code}, {response.text}")
        except Exception as e3:
            logger.error(f"All call termination methods failed: {e3}")
            

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
                "You are Jason from Alinta Energy providing customer support. Follow this exact conversation flow:\n\n"
                
                "1. ALWAYS START by saying exactly: 'Welcome to Alinta Energy. In a few words, please tell me the reason for your call.' Then WAIT for the customer to respond. DO NOT continue until they've told you their reason.\n\n"
                
                "2. AFTER the customer explains their reason, respond with: 'I understand you would like to [brief summary of their request]. Is that right?' NEVER assume what they want - only summarize what they actually said.\n\n"
                
                "3. After the customer confirms, provide a brief response using information from the search function.\n\n"
                
                "4. Then ask EXACTLY: 'To do this in a quick and easy way, would you like to receive the links by SMS or email?' and WAIT for their explicit choice.\n\n"
                
                "5. ONLY if the customer explicitly chooses email, say: 'Whilst you can do this online, I can email details. Please say your email.'\n\n"
                
                "6. ONLY if the customer explicitly chooses SMS, say: 'Whilst you can do this online, I can text details. Please say your mobile number.'\n\n"
                
                "7. When ending the call, say: 'Thanks for calling Alinta Energy.'\n\n"
                
                "Additional instructions:\n"
                "- Keep all responses concise and to the point.\n"
                "- Always WAIT for customer input after asking a question.\n"
                "- NEVER make assumptions about what the customer wants.\n"
                "- Pronounce 'noreply@alintaenergy.com.au' as 'no-reply at alinta energy dot com dot au'.\n"
                "- Speak at a measured pace, don't rush your words.\n"
                "- Respect the customer's explicit choice between SMS and email."
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

# Add this as a separate function that gets called from your WebSocket handler

async def enforce_sms_email_state(openai_ws, response, state):
    """Ensures proper handling of SMS/Email selection flow"""
    
    # Check if the response contains both the question and a premature selection
    full_response = response.get("text", "").lower()
    
    if ("would you like to receive the links by sms or email" in full_response and
        ("please be advised that you would receive an email" in full_response or 
         "please be advised that you would receive an sms" in full_response)):
        
        logger.warning("Detected premature SMS/Email selection in AI response")
        
        # Extract just the question part (up to the question mark)
        question_part = full_response.split("?")[0] + "?"
        
        # Force the AI to stop and wait for user selection
        try:
            # Override the current response with just the question
            corrected_message = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": question_part}]
                }
            }
            
            # Send the corrected message
            await openai_ws.send(json.dumps(corrected_message))
            
            # Set the state to wait for SMS/Email selection
            state["waiting_for"] = "sms_or_email"
            state["last_response"] = question_part
            
            logger.info("Corrected premature selection, now waiting for user choice")
            return True
        except Exception as e:
            logger.error(f"Error correcting premature selection: {e}")
    
    # If the response just contains the question (correct behavior)
    elif "would you like to receive the links by sms or email" in full_response:
        state["waiting_for"] = "sms_or_email"
        state["last_response"] = full_response
        logger.info("SMS/Email question detected, waiting for user selection")
    
    return False

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
    """Perform Azure Cognitive Search and return concise but complete results."""
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
            return "No relevant information found."
            
        # Create more concise results that complete full sentences
        concise_results = []
        for result in summarized_results:
            # Split into sentences
            import re
            sentences = re.split(r'(?<=[.!?])\s+', result)
            
            # Keep adding sentences until we reach ~25-30 words
            word_count = 0
            final_text = []
            
            for sentence in sentences:
                sentence_words = len(sentence.split())
                if word_count + sentence_words <= 30:
                    final_text.append(sentence)
                    word_count += sentence_words
                else:
                    # If first sentence is already too long, take part of it
                    if not final_text:
                        words = sentence.split()
                        final_text.append(" ".join(words[:25]) + "...")
                    break
                    
                # Stop if we have a complete thought and are in the 20-30 word range
                if 20 <= word_count <= 30:
                    break
                    
            concise_results.append(" ".join(final_text))
            
        return "\n".join(concise_results)
    except Exception as e:
        logger.error(f"Error in Azure Search: {e}")
        return "Error retrieving data."


async def inject_assistant_message(openai_ws, message, call_sid=None):
    """Inject a message into the conversation as if it came from the assistant."""
    # Stream the injected message
    stream_conversation("system-injected", message, call_sid)
    
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


# First, let's add a transcript management system to your code
def initialize_conversation_transcript(call_sid):
    """Initialize a new transcript record for a call"""
    if not hasattr(app, 'conversation_transcripts'):
        app.conversation_transcripts = {}
    
    # Create a new transcript entry with timestamp
    app.conversation_transcripts[call_sid] = {
        "start_time": datetime.datetime.now().isoformat(),
        "messages": [],
        "complete": False
    }
    logger.info(f"Initialized transcript tracking for call {call_sid}")

def add_to_conversation_transcript(call_sid, direction, text):
    """Add a message to the conversation transcript"""
    if not hasattr(app, 'conversation_transcripts') or call_sid not in app.conversation_transcripts:
        initialize_conversation_transcript(call_sid)
    
    # Add the message with timestamp
    timestamp = datetime.datetime.now().isoformat()
    app.conversation_transcripts[call_sid]["messages"].append({
        "timestamp": timestamp,
        "direction": direction,
        "text": text
    })

def save_complete_transcript(call_sid):
    """Save the complete transcript to Azure Monitor"""
    if not hasattr(app, 'conversation_transcripts') or call_sid not in app.conversation_transcripts:
        logger.warning(f"No transcript found for call {call_sid}")
        return
    
    if not tracer:
        logger.warning("Azure Monitor tracer not available")
        return
    
    transcript = app.conversation_transcripts[call_sid]
    
    # Mark transcript as complete
    transcript["complete"] = True
    transcript["end_time"] = datetime.datetime.now().isoformat()
    
    # Calculate duration
    start_time = datetime.datetime.fromisoformat(transcript["start_time"])
    end_time = datetime.datetime.fromisoformat(transcript["end_time"])
    duration_seconds = (end_time - start_time).total_seconds()
    
    # Create a formatted transcript for readability
    formatted_messages = []
    for msg in transcript["messages"]:
        time_str = datetime.datetime.fromisoformat(msg["timestamp"]).strftime("%H:%M:%S")
        if msg["direction"] == "user":
            formatted_messages.append(f"[{time_str}] USER: {msg['text']}")
        else:
            formatted_messages.append(f"[{time_str}] SYSTEM: {msg['text']}")
    
    full_transcript_text = "\n".join(formatted_messages)
    
    # Send to Azure Monitor as a single event
    with tracer.start_as_current_span("complete_conversation_transcript") as span:
        span.set_attribute("call_sid", call_sid)
        span.set_attribute("start_time", transcript["start_time"])
        span.set_attribute("end_time", transcript["end_time"])
        span.set_attribute("duration_seconds", duration_seconds)
        span.set_attribute("message_count", len(transcript["messages"]))
        span.set_attribute("complete_transcript", full_transcript_text)
        
        # Also include structured data for potential filtering/analysis
        for i, msg in enumerate(transcript["messages"]):
            span.set_attribute(f"message_{i}_direction", msg["direction"])
            span.set_attribute(f"message_{i}_text", msg["text"])
            span.set_attribute(f"message_{i}_timestamp", msg["timestamp"])
    
    logger.info(f"Saved complete transcript for call {call_sid} to Azure Monitor")
    
    # Clean up memory after saving
    del app.conversation_transcripts[call_sid]

if __name__ == "__main__":
    import uvicorn
    verify_twilio_setup()

    uvicorn.run(app, host="0.0.0.0", port=PORT)