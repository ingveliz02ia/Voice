import os
import httpx
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from twilio.twiml.voice_response import VoiceResponse
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

# Custom imports
from app.twilio_server import TwilioClient
from app.webhook import router as webhook_router
from app.analizer import router as analizer

# NEW RETELL SDK 2025
from retell import RetellClient

# Load envs
load_dotenv(override=True)

# ----------------------------
# APP INIT
# ----------------------------

app = FastAPI()
twilio_client = TwilioClient()

# NEW retell client
retell = RetellClient(api_key=os.getenv("RETELL_API_KEY"))

# Register inbound number
twilio_client.register_phone_agent(
    os.getenv("PHONE_NUMBER"),
    os.getenv("RETELL_AGENT_ID")
)

# Routers
app.include_router(webhook_router)
app.include_router(analizer)


# ----------------------------
# OUTBOUND CALL
# ----------------------------

@app.post("/outbound-call")
async def outbound_call(request: Request):
    body = await request.json()
    to_number = body.get("to_number")
    custom_variables = body.get("custom_variables", None)

    call = twilio_client.create_phone_call(
        os.getenv("PHONE_NUMBER"),          # FROM
        to_number,                           # TO
        os.getenv("RETELL_AGENT_ID"),
        custom_variables
    )

    return {"call_sid": call.sid, "msg": "done"}


# ----------------------------
# CALL STATUS
# ----------------------------

@app.post("/call-status")
async def call_status(request: Request):
    body = await request.json()
    call_sid = body.get("call_sid")

    call = twilio_client.get_call_status(call_sid)

    return {
        "sid": call.sid,
        "duration": call.duration,
        "status": call.status,
        "direction": call.direction,
        "from": call.from_formatted,
        "to": call.to_formatted,
        "start_time": call.start_time,
        "end_time": call.end_time,
    }


# ----------------------------
# Helper for async POST
# ----------------------------

class Item(BaseModel):
    phone: str

async def send_data(url: str, item: Item):
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=item.model_dump())
        if response.status_code not in range(200, 300):
            raise HTTPException(status_code=response.status_code, detail="Error calling external API")
        return response.json()


# ----------------------------
# INBOUND TWILIO CALL → RETELL
# ----------------------------

@app.post("/twilio-voice-webhook/{agent_id_path}")
async def twilio_voice_webhook(request: Request, agent_id_path: str):

    # Query params = dynamic variables for Retell LLM
    query_params = request.query_params
    custom_variables = {key: query_params[key] for key in query_params}

    try:
        post_data = await request.form()

        # ----------------------------
        # DETECT ANSWERING MACHINE
        ----------------------------

        if "AnsweredBy" in post_data and post_data["AnsweredBy"] == "machine_start":
            call = twilio_client.get_call_status(post_data["CallSid"])

            url = os.getenv("GHL_VOICE_MAIL_URL")
            if url:
                asyncio.create_task(send_data(url, Item(phone=call.to)))

            twilio_client.end_call(post_data["CallSid"])
            return PlainTextResponse("")

        # If other machine detection event
        elif "AnsweredBy" in post_data:
            return PlainTextResponse("")

        # Remove voicemail tag
        remove_vm_url = os.getenv("GHL_REMOVE_VOICE_MAIL_URL")
        if remove_vm_url:
            asyncio.create_task(send_data(remove_vm_url, Item(phone=post_data["To"])))

        # ----------------------------
        # NEW RETELL CALL REGISTER
        # ----------------------------

        call_response = retell.calls.create(
            agent_id=agent_id_path,
            audio_config={
                "protocol": "twilio",
                "encoding": "mulaw",
                "sample_rate": 8000
            },
            call_info={
                "from_number": post_data["From"],
                "to_number": post_data["To"]
            },
            llm_dynamic_variables=custom_variables,
            metadata={"twilio_call_sid": post_data["CallSid"]}
        )

        # ----------------------------
        # TWILIO STREAM → RETELL WS
        # ----------------------------

        response = VoiceResponse()
        start = response.connect()

        start.stream(
            url=f"wss://api.retellai.com/ws/audio/{call_response.call_id}"
        )

        return PlainTextResponse(str(response), media_type="text/xml")

    except Exception as err:
        print(f"Error in twilio voice webhook: {err}")
        return JSONResponse(
            status_code=500,
            content={"message": "Internal Server Error"}
        )
