import os
import json
import base64
import logging
import asyncio
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from twilio.twiml.voice_response import VoiceResponse, Connect
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from twilio.rest import Client
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv
import requests
import io
import time

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

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Twilio Client Setup
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Azure Search Client Setup
credential = AzureKeyCredential(AZURE_SEARCH_KEY)
search_client = SearchClient(
    endpoint=AZURE_SEARCH_ENDPOINT, index_name=AZURE_SEARCH_INDEX, credential=credential
)

# FastAPI App
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Active call tracking
active_calls = {}

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Streams REST API Application is running!"}

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming Twilio calls and set up the Media Stream"""
    call_sid = None
    try:
        # Try to parse form data
        form_data = await request.form()
        call_sid = form_data.get("CallSid")
    except RuntimeError as e:
        # Handle case where python-multipart is not installed
        logger.error(f"Form parsing error: {e}")
        # Continue without form data
    except Exception as e:
        logger.error(f"Unexpected error parsing form: {e}")
    
    logger.info(f"Incoming call received with SID: {call_sid or 'unknown'}")
    
    # Initialize call state if we have a call_sid
    if call_sid:
        active_calls[call_sid] = {
            "last_response": "",
            "waiting_for": None,
            "contact_info": None,
            "delivery_method": None,
            "transcript": "",
            "last_query": ""
        }
    
    response = VoiceResponse()
    response.say("Please wait while we connect your call.")
    response.pause(length=1)
    
    # Use MediaStream with a REST endpoint
    connect = Connect()
    media = connect.stream(name='Alinta Energy Voice Bot')
    
    # Set stream parameters
    media.parameter(name="contentType", value="audio/x-mulaw")
    
    # Set callback URLs
    host = request.headers.get("X-Forwarded-Host", request.url.hostname)
    protocol = request.headers.get("X-Forwarded-Proto", "https")
    base_url = f"{protocol}://{host}"
    
    media.parameter(name="statusCallback", value=f"{base_url}/stream-status")
    media.parameter(name="track", value="inbound_track")
    media.parameter(name="url", value=f"{base_url}/media-stream")
    
    response.append(connect)
    response.say("You can start talking now!")
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/stream-status")
async def stream_status(request: Request):
    """Handle Media Stream status callbacks"""
    form_data = await request.form()
    logger.info(f"Stream status callback: {form_data}")
    return {"status": "ok"}

@app.post("/media-stream")
async def handle_media_stream(request: Request, background_tasks: BackgroundTasks):
    """Handle incoming media chunks"""
    # Extract headers for call identification
    call_sid = request.headers.get("X-Twilio-CallSid")
    if not call_sid:
        logger.error("No Call SID provided in media stream request")
        return {"status": "error", "message": "No Call SID provided"}
    
    # Get the audio data
    audio_data = await request.body()
    
    # Process in background to avoid blocking
    background_tasks.add_task(
        process_audio_chunk, 
        call_sid=call_sid, 
        audio_data=audio_data
    )
    
    return {"status": "processing"}

async def process_audio_chunk(call_sid: str, audio_data: bytes):
    """Process audio chunks and interact with Azure OpenAI API"""
    if call_sid not in active_calls:
        logger.warning(f"Received audio for unknown call: {call_sid}")
        return
    
    try:
        # Send audio to Azure OpenAI for transcription
        # This is a simplified example - you'll need to adapt this to your Azure OpenAI API
        headers = {
            "api-key": AZURE_OPENAI_API_KEY,
            "Content-Type": "audio/mulaw"
        }
        
        files = {
            'file': ('audio.mulaw', io.BytesIO(audio_data), 'audio/mulaw')
        }
        
        # Transcribe the audio
        transcription_response = requests.post(
            f"{AZURE_OPENAI_API_ENDPOINT}/audio/transcriptions",
            headers=headers,
            files=files
        )
        
        if transcription_response.status_code == 200:
            transcription = transcription_response.json()
            user_input = transcription.get("text", "").strip()
            
            if user_input:
                # Update the transcript
                active_calls[call_sid]["transcript"] += f"User: {user_input}\n"
                active_calls[call_sid]["last_query"] = user_input
                
                # Process the user input based on conversation state
                await process_user_input(call_sid, user_input)
        else:
            logger.error(f"Transcription failed: {transcription_response.text}")
            
    except Exception as e:
        logger.error(f"Error processing audio chunk: {e}")

async def process_user_input(call_sid: str, user_input: str):
    """Process user input and generate appropriate response"""
    if call_sid not in active_calls:
        return
    
    state = active_calls[call_sid]
    user_input_lower = user_input.lower()
    
    # Check for special commands or conversation state
    if user_input_lower == "hello" or user_input_lower == "hi":
        response = "Hello, I am Jason from Alinta Energy. How can I help you today?"
        await send_tts_response(call_sid, response)
        return
        
    elif "thank you" in user_input_lower:
        response = "You are welcome. Is there anything else I can help you with?"
        await send_tts_response(call_sid, response)
        return
        
    elif state["waiting_for"] == "sms_or_email":
        if "sms" in user_input_lower:
            state["delivery_method"] = "sms"
            state["waiting_for"] = "mobile"
            response = "Please tell me your mobile number."
            await send_tts_response(call_sid, response)
            return
            
        elif "email" in user_input_lower:
            state["delivery_method"] = "email"
            state["waiting_for"] = "email"
            response = "Please tell me your email address."
            await send_tts_response(call_sid, response)
            return
            
    elif state["waiting_for"] == "mobile" and any(char.isdigit() for char in user_input):
        state["contact_info"] = user_input.replace(" ", "")
        success = await send_sms(state["contact_info"], state["last_response"])
        
        if success:
            response = "I've sent the information to your mobile number. Is there anything else you need help with?"
        else:
            response = "I'm sorry, I couldn't send the SMS. Would you like to try again or try email instead?"
            
        state["waiting_for"] = None
        await send_tts_response(call_sid, response)
        return
        
    elif state["waiting_for"] == "email" and "@" in user_input:
        state["contact_info"] = user_input.replace(" ", "")
        success = await send_email(state["contact_info"], "Information from Alinta Energy", state["last_response"])
        
        if success:
            response = "I've sent the information to your email address. Is there anything else you need help with?"
        else:
            response = "I'm sorry, I couldn't send the email. Would you like to try again or try SMS instead?"
            
        state["waiting_for"] = None
        await send_tts_response(call_sid, response)
        return
    
    # For general queries, perform RAG search
    search_results = azure_search_rag(user_input)
    
    # Prepare response with RAG results
    response = f"{search_results}"
    
    # Keep responses short
    if len(response) > 200:
        response = response[:197] + "..."
    
    # Store the response for potential SMS/email
    state["last_response"] = response
    
    # Add the SMS/email option
    response += " Would you like to receive this information via SMS or Email?"
    
    # Update state
    state["waiting_for"] = "sms_or_email"
    
    # Send response back to user
    await send_tts_response(call_sid, response)

async def send_tts_response(call_sid: str, text: str):
    """Send text-to-speech response to the caller"""
    try:
        # Update the active call transcript
        if call_sid in active_calls:
            active_calls[call_sid]["transcript"] += f"Assistant: {text}\n"
        
        # Use Twilio TwiML to speak the response
        twiml = VoiceResponse()
        twiml.say(text)
        
        # Send the TwiML update to Twilio
        twilio_client.calls(call_sid).update(twiml=str(twiml))
        logger.info(f"Sent response to call {call_sid}: {text}")
        
    except Exception as e:
        logger.error(f"Error sending TTS response: {e}")

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

async def send_email(email_address, subject, message):
    """Send email using SMTP."""
    try:
        msg = EmailMessage()
        msg.set_content(message)
        msg['Subject'] = subject
        msg['From'] = EMAIL_FROM
        msg['To'] = email_address
        
        server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT)
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        
        logger.info(f"Email sent to {email_address}")
        return True
    except Exception as e:
        logger.error(f"Error sending email: {e}")
        return False

@app.post("/tester")
async def test_endpoint(request: Request):
    """Test endpoint for debugging"""
    body = await request.body()
    headers = dict(request.headers)
    return {
        "message": "Test endpoint received request",
        "headers": headers,
        "body_size": len(body)
    }

@app.get("/active-calls")
async def get_active_calls():
    """Admin endpoint to see active calls (In a production environment, secure this)"""
    return {"active_calls": len(active_calls), "call_sids": list(active_calls.keys())}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)